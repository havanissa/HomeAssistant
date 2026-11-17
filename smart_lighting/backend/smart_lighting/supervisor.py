"""Read-only access to the basic Supervisor endpoints.

Uses only endpoints every App may read without ``hassio_api``:
``/addons/self/info`` (this App) and ``/info`` (the system). Only a fixed set
of harmless fields is kept; the App's options and tokens are never stored.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from .ha import utcnow_iso

_LOGGER = logging.getLogger(__name__)

APP_FIELDS = (
    "name", "slug", "version", "version_latest", "update_available", "state",
    "arch", "repository", "ingress", "ingress_panel", "boot", "watchdog", "ip_address",
)
SYSTEM_FIELDS = (
    "supervisor", "homeassistant", "hassos", "docker", "hostname", "operating_system",
    "machine", "arch", "channel", "supported", "healthy", "timezone",
)
TIMEOUT = aiohttp.ClientTimeout(total=10)


class SupervisorClient:
    def __init__(self, session: aiohttp.ClientSession, base_url: str | None, token: str | None) -> None:
        self._session = session
        self._base = base_url
        self._token = token
        self.app: dict[str, Any] = {}
        self.system: dict[str, Any] = {}
        self.error: str | None = None
        self.checked_at: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self._base and self._token)

    @property
    def available(self) -> bool:
        return bool(self.app or self.system)

    async def _get(self, path: str) -> dict[str, Any]:
        async with self._session.get(
            f"{self._base}{path}", headers={"Authorization": f"Bearer {self._token}"}, timeout=TIMEOUT
        ) as resp:
            try:
                body = await resp.json(content_type=None)
            except ValueError:
                raise RuntimeError(f"{path}: HTTP {resp.status} (unexpected response)") from None
            if resp.status != 200 or not isinstance(body, dict) or body.get("result") != "ok":
                message = body.get("message") if isinstance(body, dict) else None
                raise RuntimeError(f"{path}: HTTP {resp.status} {message or ''}".strip())
            data = body.get("data")
            return data if isinstance(data, dict) else {}

    async def refresh(self) -> None:
        if not self.configured:
            self.error = "Supervisor API not configured (development mode)"
            return
        errors = []
        try:
            data = await self._get("/addons/self/info")
            self.app = {k: data.get(k) for k in APP_FIELDS if k in data}
        except Exception as err:  # noqa: BLE001 - shown in Diagnostics
            errors.append(str(err) or type(err).__name__)
        try:
            data = await self._get("/info")
            self.system = {k: data.get(k) for k in SYSTEM_FIELDS if k in data}
        except Exception as err:  # noqa: BLE001
            errors.append(str(err) or type(err).__name__)
        self.error = "; ".join(errors) or None
        self.checked_at = utcnow_iso()
        if errors:
            _LOGGER.warning("Supervisor information incomplete: %s", self.error)

    def as_dict(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "available": self.available,
            "app": self.app,
            "system": self.system,
            "error": self.error,
            "checked_at": self.checked_at,
        }
