// Client-side state and the connection to the App backend.
//
// All URLs are relative to document.baseURI. The backend sets <base href> to
// the Home Assistant Ingress prefix, so this works under
// /api/hassio_ingress/<token>/ as well as at "/" during development.
// The browser only ever talks to the App backend, never to Home Assistant.

import type { EntitySummary, LiveMessage, LiveState, Overview } from "./types";

export type LinkState = "connecting" | "live" | "polling" | "offline";

type Listener = () => void;

export function apiUrl(path: string): string {
  return new URL(path, document.baseURI).toString();
}

export async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(apiUrl(path), { cache: "no-store", credentials: "same-origin" });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return (await response.json()) as T;
}

class Store {
  overview: Overview | null = null;
  link: LinkState = "connecting";
  linkError: string | null = null;
  lastUpdate: Date | null = null;
  /** Live states of watched entities (Rooms editor). */
  liveStates = new Map<string, LiveState>();
  /** Bumped when the room configuration or Home Assistant's registries change. */
  roomsVersion = 0;
  registriesVersion = 0;
  roomsRevision: string | null = null;
  private listeners = new Set<Listener>();

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private emit(): void {
    for (const listener of this.listeners) listener();
  }

  setLink(link: LinkState, error: string | null = null): void {
    if (link === this.link && error === this.linkError) return;
    this.link = link;
    this.linkError = error;
    this.emit();
  }

  setOverview(overview: Overview): void {
    this.overview = overview;
    this.lastUpdate = new Date();
    this.emit();
  }

  apply(message: LiveMessage): void {
    switch (message.type) {
      case "overview":
        this.setOverview(message.overview);
        return;
      case "status":
        if (!this.overview) return;
        this.overview = {
          ...this.overview,
          app: message.app,
          checks: message.checks,
          home_assistant: { connection: message.connection, info: message.info },
        };
        break;
      case "states":
        for (const s of message.changed) this.liveStates.set(s.entity_id, s);
        break;
      case "rooms":
        this.roomsVersion += 1;
        this.roomsRevision = (message.status?.revision as string | undefined) ?? null;
        break;
      case "registries":
        this.registriesVersion += 1;
        break;
      case "entities": {
        if (!this.overview) return;
        const changed = new Map<string, EntitySummary>(message.changed.map((e) => [e.entity_id, e]));
        this.overview = {
          ...this.overview,
          integration_entities: this.overview.integration_entities.map((e) => changed.get(e.entity_id) ?? e),
        };
        break;
      }
      default:
        return;
    }
    this.lastUpdate = new Date();
    this.emit();
  }
}

export const store = new Store();

const RETRY_SECONDS = [1, 2, 5, 10, 30];
const POLL_MS = 10_000;

/** Websocket to the backend for live updates, with HTTP polling as fallback. */
export class LiveConnection {
  private ws: WebSocket | null = null;
  private watched: string[] = [];

  /** Ask the backend for live states of these entities (read-only; replaces the previous list). */
  watch(entityIds: string[]): void {
    this.watched = [...new Set(entityIds)].slice(0, 2000);
    this.sendWatch();
  }

  private sendWatch(): void {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify({ type: "watch", entities: this.watched }));
  }
  private failures = 0;
  private retryTimer: number | undefined;
  private pollTimer: number | undefined;

  start(): void {
    void this.refresh();
    this.open();
  }

  async refresh(): Promise<void> {
    try {
      store.setOverview(await getJson<Overview>("api/overview"));
      if (store.link === "offline") store.setLink(this.ws?.readyState === WebSocket.OPEN ? "live" : "polling");
    } catch (err) {
      if (store.link !== "live") store.setLink("offline", String(err));
    }
  }

  private open(): void {
    const url = new URL("api/live", document.baseURI);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    let ws: WebSocket;
    try {
      ws = new WebSocket(url);
    } catch {
      this.scheduleRetry();
      return;
    }
    this.ws = ws;
    ws.onopen = () => {
      this.failures = 0;
      this.stopPolling();
      store.setLink("live");
      if (this.watched.length) this.sendWatch();
    };
    ws.onmessage = (event: MessageEvent<string>) => {
      try {
        store.apply(JSON.parse(event.data) as LiveMessage);
      } catch {
        /* ignore malformed messages */
      }
    };
    ws.onclose = () => {
      if (this.ws === ws) this.ws = null;
      this.scheduleRetry();
    };
  }

  private scheduleRetry(): void {
    this.failures += 1;
    if (this.failures >= 2) this.startPolling();
    else store.setLink("connecting");
    const delay = RETRY_SECONDS[Math.min(this.failures - 1, RETRY_SECONDS.length - 1)] * 1000;
    window.clearTimeout(this.retryTimer);
    this.retryTimer = window.setTimeout(() => this.open(), delay);
  }

  private startPolling(): void {
    if (this.pollTimer !== undefined) return;
    if (store.link !== "offline") store.setLink("polling");
    this.pollTimer = window.setInterval(() => void this.refresh(), POLL_MS);
    void this.refresh();
  }

  private stopPolling(): void {
    window.clearInterval(this.pollTimer);
    this.pollTimer = undefined;
  }
}

export const live = new LiveConnection();
