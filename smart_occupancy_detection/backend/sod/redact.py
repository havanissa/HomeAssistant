"""Redaction of the downloadable diagnostics package.

Removed: secrets and credentials (tokens, passwords, anything that looks like a token), Home Assistant user ids
(replaced by stable pseudonyms, so one user stays recognisable within one file), IP addresses, URLs and host
names, and personal entities (``person.*``, ``device_tracker.*``, ``notify.*``) with their names. Kept, because a
diagnosis needs them: room (area) names, sensor entity ids and names, states, times and decisions.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

SECRET_KEYS = frozenset({"token", "pairing_token", "lease_token", "access_token", "refresh_token", "password",
                         "authorization", "supervisor_token", "llat", "secret", "api_key"})
USER_KEYS = frozenset({"by", "user_id", "requested_by", "paired_by", "ingress_user", "user"})
PERSONAL_DOMAINS = ("person", "device_tracker", "notify")
_IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b|\b(?:[0-9a-f]{1,4}:){3,7}[0-9a-f]{1,4}\b", re.I)
_URL = re.compile(r"\b(?:https?|wss?)://[^\s\"'<>]+", re.I)
_TOKENISH = re.compile(r"\b(?:eyJ[\w-]{10,}\.[\w-]+\.[\w-]+|[0-9a-f]{32,}|gh[opsu]_[A-Za-z0-9]{20,})\b")
_PERSONAL_ID = re.compile(r"\b(?:" + "|".join(PERSONAL_DOMAINS) + r")\.[a-z0-9_]+\b")
REDACTED = "**REDACTED**"


def _pseudo(kind: str, value: str) -> str:
    return f"{kind}-{hashlib.sha256(value.encode()).hexdigest()[:6]}"


class Redactor:
    def __init__(self, personal_names: set[str] | None = None) -> None:
        self.names = sorted({n for n in (personal_names or set()) if len(n) >= 3}, key=len, reverse=True)

    def text(self, value: str) -> str:
        value = _TOKENISH.sub(REDACTED, value)
        value = _URL.sub("<url>", value)
        value = _IP.sub("<ip>", value)
        value = _PERSONAL_ID.sub(lambda m: f"{m.group(0).split('.')[0]}.{_pseudo('redacted', m.group(0))}", value)
        for name in self.names:
            value = value.replace(name, "<person>")
        return value

    def __call__(self, data: Any, key: str | None = None) -> Any:
        lowered = (key or "").lower()
        if lowered in SECRET_KEYS:
            return REDACTED if data else data
        if lowered in USER_KEYS and isinstance(data, str) and data:
            return _pseudo("user", data)
        if isinstance(data, dict):
            return {self.text(k) if isinstance(k, str) else k: self(v, k if isinstance(k, str) else None)
                    for k, v in data.items()}
        if isinstance(data, (list, tuple)):
            return [self(v) for v in data]
        if isinstance(data, str):
            return self.text(data)
        return data


def personal_names(states: dict[str, dict[str, Any]]) -> set[str]:
    """Friendly names of personal entities (people, phones, notification targets)."""
    return {str((s.get("attributes") or {}).get("friendly_name") or "")
            for e, s in states.items() if e.split(".", 1)[0] in PERSONAL_DOMAINS}
