"""Preset plans, position matching and operation results (0.6.1). Pure logic, no service calls.

A *plan* says, per layer of a window, what applying a preset would do:

* ``move`` with the service to use: ``cover.set_cover_position`` when the
  cover supports positions; ``cover.open_cover`` / ``cover.close_cover`` for
  open/close-only covers (only for 100 % / 0 % targets);
* ``already``: the cover is within the tolerance of its target and not moving,
  so it gets no command;
* ``unchanged``: the preset leaves this layer as it is;
* ``unavailable`` / ``missing`` / ``unsupported`` / ``busy``: it cannot be moved
  now, and why.

The same plan is the preview (nothing is sent) and, after an explicit
confirmation, the operation that the manager executes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant

from ..occupancy import friendly_name
from .config import Preset, Window
from .discovery import MOVING_STATES, available, capabilities, current_position, members

PLAN_WORDS = {
    "move": "Will move", "already": "Already there", "unchanged": "Leave unchanged", "unavailable": "Unavailable",
    "missing": "Not found", "unsupported": "Not supported", "busy": "Busy",
}
# Final per-cover results of an operation.
RESULT_WORDS = {
    "sent": "Command sent", "moving": "Moving", "reached": "Reached", "already": "Already there",
    "unchanged": "Left unchanged", "failed": "Failed", "timeout": "Timed out", "unavailable": "Unavailable",
    "missing": "Not found", "unsupported": "Not supported", "busy": "Busy", "interrupted": "Interrupted",
}
SUCCESS = frozenset({"reached", "already", "unchanged"})
PENDING = frozenset({"sent", "moving"})


@dataclass(frozen=True, slots=True)
class PlanItem:
    layer_id: str
    entity_id: str
    name: str
    kind: str
    target: int | None
    current: int | None
    state: str | None
    decision: str
    service: str | None
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"layer_id": self.layer_id, "entity_id": self.entity_id, "name": self.name, "kind": self.kind,
                "target": self.target, "current": self.current, "state": self.state, "decision": self.decision,
                "word": PLAN_WORDS[self.decision], "service": self.service, "reason": self.reason}


def at_target(state_value: str | None, position: int | None, target: int, tolerance: int, can_position: bool) -> bool:
    """Is a cover at a target? Not while it reports opening / closing."""
    if state_value in MOVING_STATES or position is None:
        return False
    if not can_position and target in (0, 100):
        return position == target
    return abs(position - target) <= tolerance


def plan_layer(hass: HomeAssistant, window: Window, layer_id: str, target: int | None, tolerance: int,
               busy: Mapping[str, str] | None = None) -> PlanItem:
    layer = window.layer(layer_id)
    assert layer is not None
    entity_id = layer.entity_id
    state = hass.states.get(entity_id)
    name = layer.label or friendly_name(hass, entity_id) or entity_id
    current = current_position(state)
    caps = capabilities(state)

    def item(decision: str, reason: str, service: str | None = None) -> PlanItem:
        return PlanItem(layer_id, entity_id, name, layer.kind, target, current, state.state if state else None,
                        decision, service, reason)

    if target is None:
        return item("unchanged", "The preset leaves this cover as it is")
    if state is None:
        return item("missing", f"{entity_id} does not exist in Home Assistant")
    if not available(state):
        return item("unavailable", f"{name} is {state.state}")
    if busy and (owner := next((busy[e] for e in members(hass, entity_id) | {entity_id} if e in busy), None)):
        return item("busy", f"{name} is still being moved by another preset ({owner})")
    if at_target(state.state, current, target, tolerance, caps["position"]):
        return item("already", f"{name} is at {current} %, within {tolerance} of {target} %: no command is sent")
    if caps["position"]:
        moving = " (it is moving now; the new target replaces that movement)" if state.state in MOVING_STATES else ""
        return item("move", f"Set {name} to {target} %{moving}", "set_cover_position")
    if target == 100 and caps["open"]:
        return item("move", f"Open {name} (it can only open or close fully)", "open_cover")
    if target == 0 and caps["close"]:
        return item("move", f"Close {name} (it can only open or close fully)", "close_cover")
    if caps["open"] or caps["close"]:
        return item("unsupported", f"{name} can only open or close fully: use 0 % or 100 % for it")
    return item("unsupported", f"{name} does not report support for moving")


def build_plan(hass: HomeAssistant, window: Window, preset: Preset, tolerance: int,
               busy: Mapping[str, str] | None = None) -> list[PlanItem]:
    return [plan_layer(hass, window, layer_id, preset.positions.get(layer_id), tolerance, busy)
            for layer_id in window.layer_ids]


def positions_now(hass: HomeAssistant, window: Window) -> dict[str, int | None]:
    return {x.layer_id: current_position(hass.states.get(x.entity_id)) for x in window.layers}


def preset_matches(hass: HomeAssistant, window: Window, preset: Preset, tolerance: int) -> bool:
    """Every layer the preset sets is at its target (layers left unchanged are ignored)."""
    targets = preset.targets
    if not targets:
        return False
    for layer_id, target in targets.items():
        layer = window.layer(layer_id)
        if layer is None:
            return False
        state = hass.states.get(layer.entity_id)
        if not at_target(state.state if state else None, current_position(state), target, tolerance,
                         capabilities(state)["position"]):
            return False
    return True


def recognise(hass: HomeAssistant, window: Window, tolerance: int) -> dict[str, Any]:
    """The preset the covers are at now, or Custom. The selected preset wins a tie, then the most specific one."""
    if not window.layers:
        return {"status": "no_covers", "preset_id": None, "text": "No covers"}
    states = [hass.states.get(x.entity_id) for x in window.layers]
    if any(s is not None and s.state in MOVING_STATES for s in states):
        return {"status": "moving", "preset_id": None, "text": "Moving"}
    if all(current_position(s) is None for s in states):
        return {"status": "unknown", "preset_id": None, "text": "Position unknown"}
    matching = [p for p in window.presets if preset_matches(hass, window, p, tolerance)]
    if not matching:
        return {"status": "custom", "preset_id": None, "text": "Custom position"}
    matching.sort(key=lambda p: (p.preset_id != window.selected_preset, -len(p.targets)))
    best = matching[0]
    return {"status": "preset", "preset_id": best.preset_id, "text": best.name,
            "also": [p.preset_id for p in matching[1:]]}


def overall(items: list[dict[str, Any]]) -> str:
    """Result of an operation from its per-cover results."""
    statuses = [i["status"] for i in items]
    if any(s in PENDING for s in statuses):
        return "running"
    moved = [s for s in statuses if s == "reached"]
    good = [s for s in statuses if s in SUCCESS]
    bad = [s for s in statuses if s not in SUCCESS]
    if not bad:
        return "succeeded" if moved else "nothing_to_do"
    if any(s == "reached" or s == "already" for s in good):
        return "partial"
    if all(s == "timeout" for s in bad):
        return "timeout"
    return "failed"


OVERALL_WORDS = {
    "running": "Moving", "succeeded": "Succeeded", "nothing_to_do": "Already there", "partial": "Partly succeeded",
    "timeout": "Timed out", "failed": "Failed", "interrupted": "Interrupted",
}


def explain(status: str, items: list[dict[str, Any]], preset_name: str) -> str:
    """One sentence for the window card's "Last action"."""
    counts: dict[str, int] = {}
    for item in items:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    if status == "running":
        return f"Applying {preset_name}: waiting for {counts.get('sent', 0) + counts.get('moving', 0)} cover(s) to report"
    if status == "nothing_to_do":
        return f"{preset_name}: every cover was already at its position; nothing was sent"
    if status == "succeeded":
        return f"{preset_name} applied: every cover reported its target position"
    if status == "interrupted":
        return f"{preset_name}: Home Assistant restarted before the positions were confirmed"
    parts = [f"{n} {RESULT_WORDS.get(s, s).lower()}" for s, n in sorted(counts.items())]
    return f"{preset_name}: {OVERALL_WORDS[status].lower()} ({', '.join(parts)})"
