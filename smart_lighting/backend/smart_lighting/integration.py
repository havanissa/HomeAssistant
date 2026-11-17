"""Detection of the existing Smart Occupancy custom integration (read-only)."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

# Statuses, from "nothing found" to "running".
NOT_INSTALLED = "not_installed"  # no files and no config entry
INSTALLED = "installed"  # files present, not set up (no config entry)
LOADED = "loaded"  # set up and running
NOT_RUNNING = "not_running"  # set up, but not loaded (error, disabled, ...)
UNKNOWN = "unknown"  # not checked yet (no Home Assistant connection)


@dataclass(frozen=True)
class IntegrationReport:
    """What the App knows about the existing integration."""

    domain: str
    status: str
    version: str | None = None
    name: str | None = None
    entries: tuple[dict[str, Any], ...] = ()
    entity_ids: tuple[str, ...] = ()
    entities_by_domain: tuple[tuple[str, int], ...] = ()
    checked: bool = False
    errors: tuple[str, ...] = ()
    entity_set: frozenset[str] = field(default=frozenset(), compare=False, repr=False)

    @classmethod
    def unknown(cls, domain: str = "smart_occupancy") -> IntegrationReport:
        return cls(domain=domain, status=UNKNOWN)

    @property
    def detected(self) -> bool:
        return self.status in {INSTALLED, LOADED, NOT_RUNNING}

    def as_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "status": self.status,
            "detected": self.detected,
            "version": self.version,
            "name": self.name,
            "entries": list(self.entries),
            "entity_count": len(self.entity_ids),
            "entities_by_domain": dict(self.entities_by_domain),
            "checked": self.checked,
            "errors": list(self.errors),
        }


def build_report(
    domain: str,
    entries: list[dict[str, Any]] | None,
    manifest: dict[str, Any] | None,
    registry: list[dict[str, Any]] | None,
) -> IntegrationReport:
    """Combine config entries, manifest and entity registry into one report.

    ``None`` means that source could not be read.
    """
    errors = []
    if entries is None:
        errors.append("config entries unavailable")
    if registry is None:
        errors.append("entity registry unavailable")

    own_entries = tuple(
        {
            "title": e.get("title"),
            "state": e.get("state"),
            "disabled_by": e.get("disabled_by"),
            "reason": e.get("reason"),
        }
        for e in (entries or [])
        if isinstance(e, dict) and e.get("domain", domain) == domain
    )
    entity_ids = tuple(
        sorted(
            e["entity_id"]
            for e in (registry or [])
            if isinstance(e, dict)
            and e.get("platform") == domain
            and e.get("entity_id")
            and not e.get("disabled_by")
        )
    )
    by_domain = Counter(eid.split(".", 1)[0] for eid in entity_ids)

    if any(e["state"] == "loaded" for e in own_entries):
        status = LOADED
    elif own_entries:
        status = NOT_RUNNING
    elif manifest:
        status = INSTALLED
    elif entries is None and manifest is None:
        status = UNKNOWN
    else:
        status = NOT_INSTALLED

    return IntegrationReport(
        domain=domain,
        status=status,
        version=(manifest or {}).get("version"),
        name=(manifest or {}).get("name"),
        entries=own_entries,
        entity_ids=entity_ids,
        entities_by_domain=tuple(sorted(by_domain.items())),
        checked=True,
        errors=tuple(errors),
        entity_set=frozenset(entity_ids),
    )
