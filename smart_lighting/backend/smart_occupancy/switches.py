"""Wall switches and remotes: configuration model and pure logic (Phase 0.3.5).

A *switch* is one physical wall switch or remote assigned to a room. Its
button events can be read from three input paths:

* ``zigbee2mqtt`` - the ``action`` of the device's Zigbee2MQTT topic
  (``zigbee2mqtt/avb_ksu``), read through Home Assistant's MQTT integration;
* ``device_trigger`` - Home Assistant MQTT device triggers of the device;
* ``event_entity`` - a Home Assistant event entity (``event.avb_ksu_action``).

Every path is normalised to one ``ButtonEvent`` (switch, source, raw action,
button, press type, time, reference). The raw action is the key of a learned
button and of its mapping; nothing about a device's buttons is assumed: they
are discovered from the device or learned by pressing them.

A switch starts in **Observing** (stored as ``listen``): its presses are
registered as manual intervention and logged, nothing is executed. **Active**
(stored as ``control``) executes the button mappings and is an explicit
activation per switch, independent of the room's automatic lighting mode.

This module has no Home Assistant runtime dependencies (models, parsing,
validation and deduplication only), so it is fully unit testable.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any

from .const import (
    BRIGHTNESS_MAX,
    BRIGHTNESS_MIN,
    DEDUPE_WINDOW_SECONDS,
    DEFAULT_DEBOUNCE_MS,
    DEFAULT_DIM_INTERVAL,
    DEFAULT_DIM_TIMEOUT,
    DEFAULT_HOLD_STEP_PCT,
    DEFAULT_MQTT_BASE_TOPIC,
    DEFAULT_STEP_PCT,
    DIM_INTERVAL_MAX,
    DIM_INTERVAL_MIN,
    DIM_TIMEOUT_MAX,
    KELVIN_MAX,
    KELVIN_MIN,
    MANUAL_OVERRIDE_MAX_MINUTES,
    MAX_BUTTONS_PER_SWITCH,
    MAX_DEBOUNCE_MS,
    MQTT_PAYLOAD_MAX_BYTES,
    ButtonAction,
    OnValues,
    SwitchMode,
    SwitchSource,
)

# Raw actions are short device-defined words ("on_right", "1_single", "brightness_move_up_left").
_ACTION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-.:]{0,63}$")
# MQTT topic levels: no wildcards, no control characters.
_TOPIC_RE = re.compile(r"^[^#+\x00-\x1f\x7f]{1,256}$")
ROOM_TARGET = "room"
# Actions that need a light target (the room's lights or one light/group).
LIGHT_ACTIONS = frozenset({
    ButtonAction.ON, ButtonAction.OFF, ButtonAction.TOGGLE, ButtonAction.BRIGHTNESS_UP,
    ButtonAction.BRIGHTNESS_DOWN, ButtonAction.DIM_UP, ButtonAction.DIM_DOWN, ButtonAction.DIM_STOP,
    ButtonAction.SET_BRIGHTNESS, ButtonAction.SET_COLOR_TEMP,
})
ACTION_WORDS: dict[ButtonAction, str] = {
    ButtonAction.NONE: "No action",
    ButtonAction.ON: "Turn lights on",
    ButtonAction.OFF: "Turn lights off",
    ButtonAction.TOGGLE: "Toggle lights",
    ButtonAction.BRIGHTNESS_UP: "Increase brightness",
    ButtonAction.BRIGHTNESS_DOWN: "Decrease brightness",
    ButtonAction.DIM_UP: "Start dimming up",
    ButtonAction.DIM_DOWN: "Start dimming down",
    ButtonAction.DIM_STOP: "Stop dimming",
    ButtonAction.SET_BRIGHTNESS: "Set brightness",
    ButtonAction.SET_COLOR_TEMP: "Set colour temperature",
    ButtonAction.MOOD: "Select mood",
    ButtonAction.MOOD_AUTO: "Return to Auto mood",
    ButtonAction.SCENE: "Activate scene",
    ButtonAction.SCRIPT: "Run script",
    ButtonAction.WLED_PRESET: "Activate WLED preset",
}


def _enum(enum_cls, value: Any, default):
    try:
        return enum_cls(value)
    except (ValueError, TypeError):
        return default


def _int(value: Any, default: int | None, low: int, high: int) -> int | None:
    if value is None or isinstance(value, bool):
        return default
    try:
        return min(max(int(value), low), high)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float, low: float, high: float) -> float:
    if value is None or isinstance(value, bool):
        return default
    try:
        return min(max(float(value), low), high)
    except (TypeError, ValueError):
        return default


def _str(value: Any, max_len: int = 255) -> str | None:
    return value.strip()[:max_len] or None if isinstance(value, str) and value.strip() else None


# ------------------------------------------------------------------ actions
_RELEASE = frozenset({"release", "stop", "released"})
_HOLD = frozenset({"hold", "long", "move", "held"})
_MULTI = {"double": "double", "triple": "triple", "quadruple": "quadruple", "many": "many"}
_SINGLE = frozenset({"single", "press", "click", "short", "pressed"})


def valid_action(raw: Any) -> bool:
    return isinstance(raw, str) and bool(_ACTION_RE.match(raw))


def parse_action(raw: str) -> tuple[str, str]:
    """(button, press type) of a raw action, for display and suggestions only.

    ``on_left`` -> ("on_left", "single"), ``brightness_move_up_left`` ->
    ("brightness_up_left", "hold"), ``brightness_stop_left`` -> ("brightness_left",
    "release"), ``1_double`` -> ("1", "double"). The raw action stays the key:
    press types are never synthesised.
    """
    tokens = [t for t in re.split(r"[_\-.:\s]+", raw.casefold()) if t]
    press = "single"
    if any(t in _RELEASE for t in tokens):
        press = "release"
    elif multi := next((_MULTI[t] for t in tokens if t in _MULTI), None):
        press = multi
    elif any(t in _HOLD for t in tokens):
        press = "hold"
    rest = [t for t in tokens if t not in _RELEASE | _HOLD | _SINGLE and t not in _MULTI]
    return ("_".join(rest) or raw.casefold()), press


def action_label(raw: str) -> str:
    """Readable name of a raw action: ``on_right`` -> ``ON right``."""
    words = re.split(r"[_\-]+", raw)
    return " ".join(w.upper() if w.casefold() in ("on", "off") else w for w in words if w) or raw


def suggest_mapping(raw: str) -> ButtonMapping | None:
    """A default mapping for recognisable actions (ON, OFF, toggle, brightness); None otherwise."""
    tokens = [t for t in re.split(r"[_\-.:\s]+", raw.casefold()) if t]
    if not tokens:
        return None
    if "brightness" in tokens or "dim" in tokens:
        if any(t in _RELEASE for t in tokens):
            return ButtonMapping(ButtonAction.DIM_STOP)
        up, down = "up" in tokens, "down" in tokens
        if "move" in tokens or "hold" in tokens:
            return ButtonMapping(ButtonAction.DIM_UP if up else ButtonAction.DIM_DOWN) if up != down else None
        if "step" in tokens:
            return ButtonMapping(ButtonAction.BRIGHTNESS_UP if up else ButtonAction.BRIGHTNESS_DOWN) if up != down else None
        return None
    first = tokens[0]
    if first == "on" and len(tokens) <= 2:
        return ButtonMapping(ButtonAction.ON)
    if first == "off" and len(tokens) <= 2:
        return ButtonMapping(ButtonAction.OFF)
    if first == "toggle" and len(tokens) <= 2:
        return ButtonMapping(ButtonAction.TOGGLE)
    return None


# ------------------------------------------------------------------ payloads
def parse_mqtt_payload(payload: Any) -> tuple[str | None, str]:
    """The action of a Zigbee2MQTT message, or None and why it is ignored.

    Device topics carry JSON (``{"action": "on_right", "battery": 100, ...}``);
    battery and link-quality updates have no (or an empty) action. A plain
    word is accepted for action topics (``zigbee2mqtt/avb_ksu/action``).
    """
    if isinstance(payload, (bytes, bytearray)):
        if len(payload) > MQTT_PAYLOAD_MAX_BYTES:
            return None, "payload_too_large"
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError:
            return None, "malformed"
    if not isinstance(payload, str):
        return None, "malformed"
    if len(payload) > MQTT_PAYLOAD_MAX_BYTES:
        return None, "payload_too_large"
    text = payload.strip()
    if not text:
        return None, "no_action"
    if text[0] in "{[\"":
        try:
            data = json.loads(text)
        except ValueError:
            return None, "malformed"
        if isinstance(data, str):
            action: Any = data
        elif isinstance(data, dict):
            action = data.get("action")
            if action is None or action == "":
                return None, "no_action"
        else:
            return None, "malformed"
    else:
        action = text
    if not isinstance(action, str) or not action.strip():
        return None, "no_action"
    action = action.strip()
    return (action, "ok") if valid_action(action) else (None, "invalid_action")


def event_entity_action(attributes: dict[str, Any]) -> str | None:
    """The raw action of an event entity's latest event.

    The state of an event entity is a timestamp, never the action. Zigbee2MQTT
    reports ``event_type: on_right``; other integrations split it into a
    ``button`` and an ``event_type`` (``button: left, event_type: double``).
    """
    event_type = attributes.get("event_type")
    if not isinstance(event_type, str) or not event_type.strip():
        return None
    event_type = event_type.strip()
    button = attributes.get("button")
    if isinstance(button, (str, int)) and str(button).strip():
        button = str(button).strip()
        if button.casefold() not in event_type.casefold():
            event_type = f"{button}_{event_type}"
    return event_type if valid_action(event_type) else None


def validate_topic(topic: Any) -> str | None:
    """Error code of a configured MQTT topic, or None. Wildcards are never accepted."""
    if not isinstance(topic, str) or not topic.strip():
        return "topic_empty"
    topic = topic.strip()
    if not _TOPIC_RE.match(topic):
        return "topic_wildcard" if ("#" in topic or "+" in topic) else "topic_invalid"
    if topic.startswith(("$", "/")) or topic.endswith("/") or "//" in topic:
        return "topic_invalid"
    if "/bridge/" in f"/{topic}/" or topic.startswith("homeassistant/"):
        return "topic_reserved"
    if "/" not in topic:
        return "topic_invalid"  # a device topic always has the base topic in front
    return None


def validate_base_topic(topic: Any) -> str | None:
    if not isinstance(topic, str) or not topic.strip():
        return "topic_empty"
    topic = topic.strip()
    if not _TOPIC_RE.match(topic):
        return "topic_wildcard" if ("#" in topic or "+" in topic) else "topic_invalid"
    if topic.startswith(("$", "/")) or topic.endswith("/") or "//" in topic or topic == "homeassistant":
        return "topic_invalid"
    return None


# --------------------------------------------------------------- the model
@dataclass(frozen=True, slots=True)
class ButtonMapping:
    """What one button event does."""

    action: ButtonAction = ButtonAction.NONE
    # "room" (every managed light of the room) or one light / light group.
    target: str = ROOM_TARGET
    on_values: OnValues = OnValues.MOOD
    brightness: int | None = None        # % (set brightness, fixed ON)
    color_temp: int | None = None        # K (set colour temperature, fixed ON)
    mood: str | None = None              # mood
    scope: str = "room"                  # mood: "room" (room mood) or "global" (the mood selector)
    entity: str | None = None            # scene / script / WLED preset select entity
    option: str | None = None            # WLED preset name
    minutes: int | None = None           # temporary room mood; None = until changed

    @classmethod
    def from_dict(cls, data: Any) -> ButtonMapping:
        data = data if isinstance(data, dict) else {}
        target = data.get("target")
        return cls(
            action=_enum(ButtonAction, data.get("action"), ButtonAction.NONE),
            target=target.strip() if isinstance(target, str) and target.strip() else ROOM_TARGET,
            on_values=_enum(OnValues, data.get("on_values"), OnValues.MOOD),
            brightness=_int(data.get("brightness"), None, BRIGHTNESS_MIN, BRIGHTNESS_MAX),
            color_temp=_int(data.get("color_temp"), None, KELVIN_MIN, KELVIN_MAX),
            mood=_str(data.get("mood"), 100),
            scope="global" if data.get("scope") == "global" else "room",
            entity=_str(data.get("entity")),
            option=_str(data.get("option")),
            minutes=_int(data.get("minutes"), None, 1, MANUAL_OVERRIDE_MAX_MINUTES),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value, "target": self.target, "on_values": self.on_values.value,
            "brightness": self.brightness, "color_temp": self.color_temp, "mood": self.mood,
            "scope": self.scope, "entity": self.entity, "option": self.option, "minutes": self.minutes,
        }

    def describe(self) -> str:
        """Plain words: "Turn lights on", "Select Hygge (room)", "Set brightness 40 %"."""
        word = ACTION_WORDS[self.action]
        if self.action == ButtonAction.MOOD:
            return f"Select {self.mood}" + (" (all rooms)" if self.scope == "global" else "")
        if self.action == ButtonAction.MOOD_AUTO:
            return word + (" (all rooms)" if self.scope == "global" else "")
        if self.action == ButtonAction.SET_BRIGHTNESS and self.brightness:
            word = f"{word} {self.brightness} %"
        if self.action == ButtonAction.SET_COLOR_TEMP and self.color_temp:
            word = f"{word} {self.color_temp} K"
        if self.action in (ButtonAction.SCENE, ButtonAction.SCRIPT) and self.entity:
            word = f"{word} {self.entity}"
        if self.action == ButtonAction.WLED_PRESET and self.option:
            word = f"{word} {self.option}"
        if self.action in LIGHT_ACTIONS and self.target != ROOM_TARGET:
            word = f"{word} ({self.target})"
        return word


@dataclass(frozen=True, slots=True)
class LearnedButton:
    """A button event the device was seen (or declared) to emit."""

    raw: str
    button: str
    press: str
    source: str | None = None      # input path that delivered it ("discovered": from the device's definition)
    learned_at: str | None = None

    @classmethod
    def create(cls, raw: str, source: str | None, at: str | None) -> LearnedButton:
        button, press = parse_action(raw)
        return cls(raw, button, press, source, at)

    @classmethod
    def from_dict(cls, raw: str, data: Any) -> LearnedButton:
        data = data if isinstance(data, dict) else {}
        button, press = parse_action(raw)
        return cls(raw, str(data.get("button") or button), str(data.get("press") or press),
                   data.get("source") or None, data.get("learned_at") or None)

    def as_dict(self) -> dict[str, Any]:
        return {"button": self.button, "press": self.press, "source": self.source, "learned_at": self.learned_at}


@dataclass(frozen=True, slots=True)
class DimmingConfig:
    step: int = DEFAULT_STEP_PCT              # % per press (increase / decrease brightness)
    hold_step: int = DEFAULT_HOLD_STEP_PCT    # % per hold-to-dim command
    interval: float = DEFAULT_DIM_INTERVAL    # s between hold-to-dim commands
    min_brightness: int = BRIGHTNESS_MIN
    max_brightness: int = BRIGHTNESS_MAX
    timeout: int = DEFAULT_DIM_TIMEOUT        # s: stop when no STOP / release arrives

    @classmethod
    def from_dict(cls, data: Any) -> DimmingConfig:
        data = data if isinstance(data, dict) else {}
        low = _int(data.get("min_brightness"), BRIGHTNESS_MIN, BRIGHTNESS_MIN, BRIGHTNESS_MAX) or BRIGHTNESS_MIN
        high = _int(data.get("max_brightness"), BRIGHTNESS_MAX, BRIGHTNESS_MIN, BRIGHTNESS_MAX) or BRIGHTNESS_MAX
        if high < low:
            low, high = high, low
        return cls(
            step=_int(data.get("step"), DEFAULT_STEP_PCT, 1, 50) or DEFAULT_STEP_PCT,
            hold_step=_int(data.get("hold_step"), DEFAULT_HOLD_STEP_PCT, 1, 50) or DEFAULT_HOLD_STEP_PCT,
            interval=_float(data.get("interval"), DEFAULT_DIM_INTERVAL, DIM_INTERVAL_MIN, DIM_INTERVAL_MAX),
            min_brightness=low,
            max_brightness=high,
            timeout=_int(data.get("timeout"), DEFAULT_DIM_TIMEOUT, 1, DIM_TIMEOUT_MAX) or DEFAULT_DIM_TIMEOUT,
        )

    def as_dict(self) -> dict[str, Any]:
        return {"step": self.step, "hold_step": self.hold_step, "interval": self.interval,
                "min_brightness": self.min_brightness, "max_brightness": self.max_brightness,
                "timeout": self.timeout}


@dataclass(frozen=True, slots=True)
class SwitchConfig:
    """One wall switch or remote assigned to a room."""

    id: str
    name: str
    device_id: str | None = None          # Home Assistant device
    ieee: str | None = None               # Zigbee IEEE address (follows Zigbee2MQTT renames)
    z2m_name: str | None = None           # last known Zigbee2MQTT friendly name
    topic: str | None = None              # Advanced: manual MQTT topic (replaces the derived one)
    event_entity: str | None = None
    source: SwitchSource = SwitchSource.AUTO
    mode: SwitchMode = SwitchMode.LISTEN
    activated_at: str | None = None
    activated_by: str | None = None
    acknowledged: tuple[str, ...] = ()    # automations accepted as also handling this switch
    buttons: dict[str, LearnedButton] = field(default_factory=dict)
    mappings: dict[str, ButtonMapping] = field(default_factory=dict)
    dimming: DimmingConfig = field(default_factory=DimmingConfig)
    debounce_ms: int = DEFAULT_DEBOUNCE_MS
    activity_evidence: bool = False       # a press refreshes room activity (occupancy engine)
    override_minutes: int | None = None   # None: the room's manual override time

    @classmethod
    def from_dict(cls, data: Any) -> SwitchConfig | None:
        if not isinstance(data, dict) or not isinstance(data.get("id"), str) or not data["id"]:
            return None
        buttons_raw = data.get("buttons") if isinstance(data.get("buttons"), dict) else {}
        mappings_raw = data.get("mappings") if isinstance(data.get("mappings"), dict) else {}
        buttons = {raw: LearnedButton.from_dict(raw, b) for raw, b in list(buttons_raw.items())[:MAX_BUTTONS_PER_SWITCH]
                   if valid_action(raw)}
        mappings = {raw: ButtonMapping.from_dict(m) for raw, m in mappings_raw.items() if valid_action(raw)}
        topic = _str(data.get("topic"), 256)
        acknowledged = data.get("acknowledged") if isinstance(data.get("acknowledged"), list) else []
        return cls(
            id=data["id"],
            name=_str(data.get("name"), 100) or data["id"],
            device_id=_str(data.get("device_id")),
            ieee=_str(data.get("ieee"), 32),
            z2m_name=_str(data.get("z2m_name"), 200),
            topic=topic if topic and validate_topic(topic) is None else None,
            event_entity=_str(data.get("event_entity")),
            source=_enum(SwitchSource, data.get("source"), SwitchSource.AUTO),
            mode=_enum(SwitchMode, data.get("mode"), SwitchMode.LISTEN),
            activated_at=data.get("activated_at") or None,
            activated_by=data.get("activated_by") or None,
            acknowledged=tuple(dict.fromkeys(a for a in acknowledged if isinstance(a, str) and a)),
            buttons=buttons,
            mappings=mappings,
            dimming=DimmingConfig.from_dict(data.get("dimming")),
            debounce_ms=_int(data.get("debounce_ms"), DEFAULT_DEBOUNCE_MS, 0, MAX_DEBOUNCE_MS) or 0,
            activity_evidence=bool(data.get("activity_evidence", False)),
            override_minutes=_int(data.get("override_minutes"), None, 0, MANUAL_OVERRIDE_MAX_MINUTES),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "device_id": self.device_id, "ieee": self.ieee,
            "z2m_name": self.z2m_name, "topic": self.topic, "event_entity": self.event_entity,
            "source": self.source.value, "mode": self.mode.value, "activated_at": self.activated_at,
            "activated_by": self.activated_by, "acknowledged": list(self.acknowledged),
            "buttons": {raw: b.as_dict() for raw, b in self.buttons.items()},
            "mappings": {raw: m.as_dict() for raw, m in self.mappings.items()},
            "dimming": self.dimming.as_dict(), "debounce_ms": self.debounce_ms,
            "activity_evidence": self.activity_evidence, "override_minutes": self.override_minutes,
        }

    @property
    def controls(self) -> bool:
        return self.mode == SwitchMode.CONTROL

    def mapping(self, raw: str) -> ButtonMapping | None:
        mapping = self.mappings.get(raw)
        return mapping if mapping is not None and mapping.action != ButtonAction.NONE else None

    def with_button(self, raw: str, source: str | None, at: str | None) -> SwitchConfig:
        """Add a learned button (and its suggested default mapping). Unchanged if known or full."""
        if raw in self.buttons or len(self.buttons) >= MAX_BUTTONS_PER_SWITCH or not valid_action(raw):
            return self
        mappings = self.mappings
        if raw not in mappings and (suggested := suggest_mapping(raw)) is not None:
            mappings = {**mappings, raw: suggested}
        return replace(self, buttons={**self.buttons, raw: LearnedButton.create(raw, source, at)}, mappings=mappings)


@dataclass(frozen=True, slots=True)
class SwitchSettings:
    """Global settings of the switch inputs."""

    mqtt_base_topic: str = DEFAULT_MQTT_BASE_TOPIC

    @classmethod
    def from_dict(cls, data: Any) -> SwitchSettings:
        data = data if isinstance(data, dict) else {}
        base = data.get("mqtt_base_topic")
        base = base.strip() if isinstance(base, str) else None
        return cls(mqtt_base_topic=base if base and validate_base_topic(base) is None else DEFAULT_MQTT_BASE_TOPIC)

    def as_dict(self) -> dict[str, Any]:
        return {"mqtt_base_topic": self.mqtt_base_topic}


def switches_from(data: Any) -> tuple[SwitchConfig, ...]:
    if not isinstance(data, list):
        return ()
    out: dict[str, SwitchConfig] = {}
    for item in data:
        if (switch := SwitchConfig.from_dict(item)) is not None and switch.id not in out:
            out[switch.id] = switch
    return tuple(out.values())


# ------------------------------------------------------------------ events
@dataclass(frozen=True, slots=True)
class ButtonEvent:
    """One button press, from whichever input path delivered it."""

    area_id: str
    switch_id: str
    source: str           # zigbee2mqtt | device_trigger | event_entity
    raw: str
    button: str
    press: str
    at: float             # monotonic
    when: str             # ISO time
    ref: str | None = None  # unique reference where the source has one (event entity timestamp)

    @classmethod
    def create(cls, area_id: str, switch_id: str, source: str, raw: str, at: float, when: str,
               ref: str | None = None) -> ButtonEvent:
        button, press = parse_action(raw)
        return cls(area_id, switch_id, source, raw, button, press, at, when, ref)

    def as_dict(self) -> dict[str, Any]:
        return {"source": self.source, "raw": self.raw, "label": action_label(self.raw), "button": self.button,
                "press": self.press, "at": self.when}


class Deduplicator:
    """Drops the same press seen through a second input path; keeps deliberate repeats.

    * the same action from a *different* source within ``DEDUPE_WINDOW_SECONDS``
      is the same physical press (raw MQTT + device trigger + event entity);
    * the same action from the *same* source is a new press, unless it repeats
      within the switch's debounce time (radio retransmissions);
    * a reference (event entity timestamp) seen before is a duplicate.
    """

    def __init__(self, window: float = DEDUPE_WINDOW_SECONDS) -> None:
        self.window = window
        self._last: dict[tuple[str, str, str], tuple[float, str]] = {}
        self._refs: dict[tuple[str, str], float] = {}

    def accept(self, event: ButtonEvent, debounce_ms: int) -> tuple[bool, str | None]:
        now = event.at
        self._prune(now)
        if event.ref is not None:
            ref_key = (event.switch_id, event.ref)
            if ref_key in self._refs:
                return False, "duplicate"
            self._refs[ref_key] = now
        key = (event.area_id, event.switch_id, event.raw)
        last = self._last.get(key)
        if last is not None:
            elapsed = now - last[0]
            if last[1] != event.source and elapsed < self.window:
                return False, "duplicate"  # same press, another input path: the first one counts
            if last[1] == event.source and elapsed < debounce_ms / 1000:
                return False, "debounced"
        self._last[key] = (now, event.source)
        return True, None

    def forget(self, switch_id: str) -> None:
        self._last = {k: v for k, v in self._last.items() if k[1] != switch_id}
        self._refs = {k: v for k, v in self._refs.items() if k[0] != switch_id}

    def _prune(self, now: float) -> None:
        horizon = now - max(self.window, MAX_DEBOUNCE_MS / 1000) - 1
        if len(self._last) > 64:
            self._last = {k: v for k, v in self._last.items() if v[0] >= horizon}
        if len(self._refs) > 64:
            self._refs = {k: t for k, t in self._refs.items() if t >= now - 60}


# Words for the reasons an event was ignored (diagnostics).
IGNORED_WORDS = {
    "no_action": "no button action in the message (battery or link-quality update)",
    "malformed": "malformed message",
    "payload_too_large": "message too large",
    "invalid_action": "the action is not a valid button name",
    "retained": "retained message (not a new press)",
    "stale": "stale event (restored state, not a new press)",
    "settling": "replayed while the trigger was attached (not a new press)",
    "duplicate": "the same press through another input path",
    "debounced": "repeated within the debounce time",
    "learning": "captured by Learn buttons (no command)",
    "not_mapped": "this button has no assigned action",
    "listen_only": "Observing: registered, no action is executed",
    "switched_to_observe": "the switch was set to Observing before the press ran: not executed",
    "busy": "too many presses queued; this one was dropped",
    "disabled": "blocked: the room is not enabled in Smart Occupancy",
}
