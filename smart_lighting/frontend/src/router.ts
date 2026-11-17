// Navigation items and hash routing. Hash routing keeps every URL inside
// the Ingress prefix, so a reload or a deep link never leaves the App.

export interface NavItem {
  key: string;
  title: string;
  icon: string;
}

export const NAV: NavItem[] = [
  { key: "dashboard", title: "Dashboard", icon: "view-dashboard" },
  { key: "rooms", title: "Rooms", icon: "floor-plan" },
  { key: "floorplan", title: "Floor plan", icon: "home-map-marker" },
  { key: "presence", title: "Presence", icon: "motion-sensor" },
  { key: "lighting", title: "Lighting", icon: "lightbulb-group" },
  { key: "moods", title: "Moods", icon: "palette" },
  { key: "switches", title: "Switches", icon: "light-switch" },
  { key: "curtains", title: "Curtains", icon: "curtains" },
  { key: "predictions", title: "Predictions", icon: "crystal-ball" },
  { key: "diagnostics", title: "Diagnostics", icon: "stethoscope" },
  { key: "settings", title: "Settings", icon: "cog" },
];

export const FOUNDATION = new Set(["dashboard", "floorplan", "diagnostics", "settings"]);

/** Phone bottom navigation; every module (these included) is under "More". */
export const BOTTOM_NAV = ["dashboard", "rooms", "lighting", "moods"];

export function routeFromHash(): string {
  const key = window.location.hash.replace(/^#\/?/, "").split(/[/?]/)[0];
  return NAV.some((item) => item.key === key) ? key : "dashboard";
}

/** Path segments after the module key, e.g. #/rooms/kamar_nordur/presence -> ["kamar_nordur", "presence"]. */
export function subPath(): string[] {
  return window.location.hash
    .replace(/^#\/?/, "")
    .split("?")[0]
    .split("/")
    .slice(1)
    .filter(Boolean)
    .map((p) => decodeURIComponent(p));
}

/** Query parameters after the path, e.g. #/rooms/kitchen/presence?adjacent=hall. */
export function hashQuery(): URLSearchParams {
  return new URLSearchParams(window.location.hash.split("?")[1] ?? "");
}

export function navigate(key: string): void {
  if (window.location.hash === `#/${key}`) return;
  window.location.hash = `/${key}`;
}

export function navigatePath(...parts: string[]): void {
  window.location.hash = "/" + parts.map((p) => encodeURIComponent(p)).join("/");
}

// Unsaved-changes guard: a page with unsaved edits registers a check; leaving asks first.
type Guard = () => string | null;
let guard: Guard | null = null;

export function setLeaveGuard(check: Guard | null): void {
  guard = check;
}

/** The message to confirm before leaving, or null when there is nothing unsaved. */
export function leaveWarning(): string | null {
  return guard ? guard() : null;
}
