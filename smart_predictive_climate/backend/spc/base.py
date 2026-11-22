"""The App's base core: settings, the configuration store, the Home Assistant client and the companion link.

``core.AppCore`` derives from :class:`BaseCore` and adds everything about climate in ``on_start`` / ``on_stop``.
Nothing here knows what a room or a forecast is.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from . import config
from .const import APP_NAME, APP_VERSION
from .ha import HomeAssistantClient
from .logbuffer import BufferHandler
from .publisher import CompanionLink
from .settings import Settings
from .store import ConfigError, ConfigStore

_LOGGER = logging.getLogger(__name__)

ADMIN_CACHE_SECONDS = 60


class BaseCore:
    """Attributes: ``settings``, ``buffer``, ``store`` (loaded), and after ``start()``: ``session``, ``ha``,
    ``link`` (None before)."""

    def __init__(self, settings: Settings, buffer: BufferHandler | None = None) -> None:
        self.settings = settings
        self.buffer = buffer
        self.store = ConfigStore(settings.data_dir, validate=config.validate_config, empty=config.empty_config)
        self.store.load()
        self.session: aiohttp.ClientSession | None = None
        self.ha: HomeAssistantClient | None = None
        self.link: CompanionLink | None = None
        self._admins: tuple[float, dict[str, bool]] | None = None

    # -------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        """Create the session, the Home Assistant client and the companion link, call ``on_start()`` (listeners
        are registered there, before the first connection), then start the link and the connection."""
        self.session = aiohttp.ClientSession()
        self.ha = HomeAssistantClient(self.session, self.settings.ha_ws_url, self.settings.token)
        self.link = CompanionLink(self.ha, self.settings.data_dir, app_version=APP_VERSION,
                                  can_publish=self.can_publish)
        await self.on_start()
        await self.link.start()
        await self.ha.start()
        _LOGGER.info("%s %s started", APP_NAME, APP_VERSION)

    async def stop(self) -> None:
        """``on_stop()`` first (while the connection still works), then the link (releases the lease), the
        connection and the session."""
        try:
            await self.on_stop()
        except Exception:  # noqa: BLE001 - the rest must still be closed
            _LOGGER.exception("Stopping failed")
        if self.link:
            await self.link.stop()
        if self.ha:
            await self.ha.stop()
        if self.session:
            await self.session.close()

    async def on_start(self) -> None:
        """For the subclass: ``self.session``, ``self.ha`` and ``self.link`` exist; nothing is connected yet."""

    async def on_stop(self) -> None:
        """For the subclass: called first when the App stops."""

    def can_publish(self) -> str | None:
        """For the subclass: a reason not to claim the publishing lease yet (None = publish)."""
        return None

    # ----------------------------------------------------------------- users
    async def is_admin(self, user_id: str | None) -> bool:
        """Is this Home Assistant user an active administrator (Ingress passes the user id)?"""
        if not user_id or self.ha is None:
            return False
        now = asyncio.get_running_loop().time()
        if self._admins is None or now - self._admins[0] > ADMIN_CACHE_SECONDS or user_id not in self._admins[1]:
            users = await self.ha.command("config/auth/list") or []
            self._admins = (now, {
                u.get("id"): bool(u.get("is_active", True)) and not u.get("system_generated")
                and (bool(u.get("is_owner")) or "system-admin" in (u.get("group_ids") or []))
                for u in users if isinstance(u, dict)
            })
        return bool(self._admins[1].get(user_id))

    # --------------------------------------------------------------- reading
    def _require_ha(self) -> HomeAssistantClient:
        if self.ha is None or not self.ha.connected:
            raise ConfigError("not_connected", "Home Assistant is not connected; try again in a moment.", 503)
        return self.ha

    def connection(self) -> dict[str, Any]:
        link = self.link.status() if self.link else None
        return {"home_assistant": self.ha.status() if self.ha else None, "companion": link,
                "versions": {"app": APP_VERSION, "home_assistant": self.ha.ha_version if self.ha else None,
                             "companion": (link or {}).get("companion_version")}}
