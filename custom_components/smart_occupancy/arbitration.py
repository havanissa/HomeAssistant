"""The central lighting rule engine (0.6.0): one winning rule per target, before any command is sent.

Pure logic. Every plan the controller builds (Occupied profile, Vacant
profile / vacancy action, possible occupancy, gradual updates) passes through
``arbitrate`` before it is executed or shown, so zones, moods and
preliminary lighting never send competing commands to the same light: each
controlled target gets exactly one plan item, from the winning rule.

Resolved policy, highest first:

1. **Protection** - Manual mood (everything paused), unavailable devices,
   excluded lights, members controlled through their group, unacknowledged
   automation conflicts. Nothing below overrides these.
2. **High-priority zone rules** - an active zone whose manual policy is
   ``override``: it wins over a manual override made *before* it became
   active (a change made while it is active is respected).
3. **Manual overrides** - a person's change is respected by every rule below.
4. **Zone rules** - active zones; among several zones claiming the same
   target the higher ``priority`` wins, then the zone listed first.
   ``Unchanged`` claims the target without changing it (lower rules do not
   act on it while the zone is active).
5. **Occupied profile** (the room's mood).
6. **Possible occupancy** preliminary lighting (only lights no zone claims).
7. **Vacant profile** / vacancy action (zones are never active in a vacant
   room).

Zone lights obey the daylight rule of their zone (the room's rules, always,
or the zone's own threshold) when they would switch an *off* light on; a
light that is already on follows the zone regardless. Shared spaces (rules
across rooms) are applied by the controller after this (``shared_*``).

Predicted occupancy never enters here: zones are active from sensor states
only.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.util import dt as dt_util

from .const import MANUAL_PAUSED_TEXT, LightAction
from .daylight import GATE_CODES, DaylightGate
from .planner import BLOCKED, CHANGE, SKIPPED, UNCHANGED, PlanItem, Target, build_command, matches
from .profiles import ResolvedSetting
from .zones import ZoneAction, ZoneConfig, ZoneManual

RULE_ZONE = "zone"
RULE_OCCUPIED = "occupied"
RULE_VACANT = "vacant"
RULE_PRELIMINARY = "preliminary"
RULE_BRIGHTNESS = "brightness"  # an enrolled light's Room occupied brightness rule (0.6.7)
RULE_NONE = "none"


@dataclass(slots=True, frozen=True)
class Claim:
    """One active zone's rule for one target."""

    zone: ZoneConfig
    order: int
    active_since: datetime | None
    gate: DaylightGate | None  # the zone's daylight decision (None: switched on whatever the daylight)

    @property
    def rank(self) -> tuple[int, int]:
        return -self.zone.priority, self.order

    def label(self) -> str:
        return f"{self.zone.name} zone"


def claims_by_target(active: Iterable[tuple[ZoneConfig, int, datetime | None, DaylightGate | None]],
                     ) -> dict[str, list[Claim]]:
    """entity -> the claims of every active zone that targets it, the winner first (deterministic)."""
    out: dict[str, list[Claim]] = {}
    for zone, order, since, gate in active:
        for target in zone.targets:
            out.setdefault(target.entity_id, []).append(Claim(zone, order, since, gate))
    for claims in out.values():
        claims.sort(key=lambda c: c.rank)
    return out


def rule_info(kind: str, name: str, **extra: Any) -> dict[str, Any]:
    return {"kind": kind, "name": name, **{k: v for k, v in extra.items() if v is not None}}


def _manual_wins(override: dict[str, Any] | None, claim: Claim) -> bool:
    """Is a manual override respected against this zone? (see the module docstring)"""
    if not override:
        return False
    if claim.zone.manual != ZoneManual.OVERRIDE:
        return True
    since = dt_util.parse_datetime(override.get("since") or "")
    # Made while the zone is active: a person's deliberate change in the zone; respected.
    return since is None or claim.active_since is None or since >= claim.active_since


def zone_item(t: Target, claims: list[Claim], *, manual_mode: bool, control_enabled: bool,
              replaced: PlanItem | None = None, setting: ResolvedSetting | None = None, what: str | None = None,
              superseded: list[str] | None = None) -> PlanItem:
    """The plan item of the winning zone claim for one target (same protections as every plan item).

    Brightness rules (0.6.7): ``setting`` replaces the zone target's own values (the matrix brightness with
    the mood's appearance), ``what`` describes it and ``superseded`` names the rules it takes precedence over.
    """
    claim = claims[0]
    zt = claim.zone.target(t.entity_id)
    action = zt.action if zt else ZoneAction.UNCHANGED
    if setting is not None:
        resolved = setting
    elif action == ZoneAction.ON:
        resolved = ResolvedSetting(action=LightAction.ON, brightness_pct=zt.brightness_pct,
                                   color_temp_kelvin=zt.color_temp_kelvin, transition=zt.transition)
    elif action == ZoneAction.OFF:
        resolved = ResolvedSetting(action=LightAction.OFF, transition=zt.transition)
    else:
        resolved = ResolvedSetting()
    service, data, requested, unsupported = build_command(resolved, t.capabilities)
    if superseded is None:
        superseded = [c.label() for c in claims[1:]]
        lower = replaced.rule.get("name") if replaced is not None and replaced.rule else None
        if replaced is not None and replaced.service is not None and replaced.decision != SKIPPED and lower:
            superseded.append(lower)
    what = what or (zt.describe() if zt else "")
    item = PlanItem(
        entity_id=t.entity_id, name=t.name, category=t.category, category_name=t.category_name,
        is_group=t.is_group, current=t.current(), resolved=resolved, requested=requested, service=service,
        data=data, decision=SKIPPED, reason_code="", reason="", unsupported=unsupported, override=t.override,
        conflicts=t.conflicts,
        rule=rule_info(RULE_ZONE, claim.label(), zone=claim.zone.id, priority=claim.zone.priority,
                       superseded=superseded or None),
    )

    def set_(decision: str, code: str, reason: str) -> None:
        item.decision, item.reason_code, item.reason = decision, code, reason

    zone = claim.zone.name
    over = f"; takes precedence over {', '.join(superseded)}" if superseded else ""
    if manual_mode:
        set_(BLOCKED, "manual_mode", f"{MANUAL_PAUSED_TEXT}: nothing is sent")
    elif t.excluded:
        set_(SKIPPED, "excluded", f"Excluded from automatic management ({t.excluded})")
    elif t.covered_by:
        set_(SKIPPED, "covered_by_group", f"Member of managed group {t.covered_by}; controlled through the group")
    elif service is None and setting is not None:
        set_(UNCHANGED, "zone_unchanged", f"Left as it is: the {zone} zone's brightness rule sets nothing{over}")
    elif service is None:
        set_(UNCHANGED, "zone_unchanged", f"Left as it is: the {zone} zone says Unchanged{over}")
    elif t.offline:
        set_(BLOCKED, "offline", f"Light is {t.state or 'missing'}; nothing can be sent ({zone} zone)")
    elif _manual_wins(t.override, claim):
        source = (t.override or {}).get("source", "manual")
        set_(BLOCKED, "manual_override", f"Manual change ({source}) is respected by the {zone} zone")
    elif t.conflicts:
        names = ", ".join(c.get("name") or c["automation"] for c in t.conflicts)
        set_(BLOCKED, "conflict", f"Also controlled by enabled automation(s) {names}; acknowledge or resolve it first")
    elif service == "turn_on" and t.state != "on" and claim.gate is not None and (block := claim.gate.blocks()):
        set_(BLOCKED, block[0], f"{block[1]} ({zone} zone)")
    elif matches(t, service, data):
        set_(UNCHANGED, "already", f"Already as the {zone} zone sets it ({what}){over}")
    elif t.last_command == (service, data):
        set_(UNCHANGED, "already_sent", "The same command was already sent and the light was not changed since")
    else:
        verb = "Will" if control_enabled else "Would"
        what = "switch off" if service == "turn_off" else f"set {what or 'on'}"
        manual = " (its manual override predates the zone)" if t.override else ""
        set_(CHANGE, "change", f"{verb} {what} because the {zone} zone is occupied{over}{manual}.")
    return item


def zone_skip(item: PlanItem, claim: Claim) -> PlanItem:
    """A preliminary-lighting item for a light a zone controls: left to the zone."""
    item.decision, item.reason_code = SKIPPED, "zone"
    item.reason = f"Controlled by the {claim.zone.name} zone"
    item.rule = rule_info(RULE_ZONE, claim.label(), zone=claim.zone.id, priority=claim.zone.priority)
    return item


# Decisions after which nothing keeps a light a zone switched on: the zone that released it switches it off.
RELEASE_CODES = frozenset({"ignored", "no_mood", "vacant", "awaiting_confirmation", "occupancy_uncertain",
                           "off_needs_confirmation", *GATE_CODES})


def releases(item: PlanItem | None) -> bool:
    """After a zone ends, does the remaining rule leave a light it switched on without any setting?"""
    if item is None:
        return False
    return item.decision in (SKIPPED, BLOCKED, UNCHANGED) and item.reason_code in RELEASE_CODES
