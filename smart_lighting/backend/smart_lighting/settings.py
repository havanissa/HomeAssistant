"""Runtime settings, read from the environment and the App options.

On Home Assistant OS everything comes from the Supervisor:
``SUPERVISOR_TOKEN`` (environment) and ``/data/options.json`` (the App's
Configuration tab). The ``SL_*`` variables exist only for development and
tests, for example to run the backend against a local Home Assistant.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import ipaddress
import json
import logging
import os
from pathlib import Path

from .const import (
    DEFAULT_HA_WS_URL,
    DEFAULT_PORT,
    DATA_DIR,
    DEFAULT_SUPERVISOR_URL,
    OPTIONS_PATH,
    SUPERVISOR_INGRESS_IP,
)

_LOGGER = logging.getLogger(__name__)

LOG_LEVELS = ("debug", "info", "warning", "error")
IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


@dataclass(frozen=True)
class Settings:
    """Backend settings. The token is never part of repr() or any output."""

    token: str | None = field(repr=False)
    ha_ws_url: str
    supervisor_url: str | None
    host: str
    port: int
    allowed_networks: tuple[IpNetwork, ...]
    log_level: str
    static_dir: Path
    dev_mode: bool
    data_dir: Path | None = None
    # Phase A3.2: shadow comparison required before a handover. Only tests and development lower them
    # (SL_ENGINE_MIN_SHADOW_SECONDS, SL_ENGINE_MIN_TRANSITIONS); they are not App options.
    engine_min_shadow_seconds: int = 7200
    engine_min_transitions: int = 20

    @property
    def token_present(self) -> bool:
        return bool(self.token)

    def is_allowed(self, remote: str | None) -> bool:
        """Return True when a client address may use the App."""
        if not self.allowed_networks:
            return True
        if not remote:
            return False
        try:
            address = ipaddress.ip_address(remote)
        except ValueError:
            return False
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return any(address in network for network in self.allowed_networks)

    def public(self) -> dict[str, object]:
        """Settings that are safe to show in the interface and diagnostics."""
        return {
            "home_assistant_websocket": self.ha_ws_url,
            "supervisor_api": self.supervisor_url,
            "listen": f"{self.host}:{self.port}",
            "allowed_clients": [str(n) for n in self.allowed_networks] or ["any"],
            "log_level": self.log_level,
            "token": "present (hidden)" if self.token else "missing",
            "development_mode": self.dev_mode,
            "data_dir": str(self.data_dir) if self.data_dir else None,
            "engine_min_shadow_seconds": self.engine_min_shadow_seconds,
            "engine_min_transitions": self.engine_min_transitions,
        }


def _networks(value: str) -> tuple[IpNetwork, ...]:
    if value.strip() in {"*", "any"}:
        return ()
    networks = []
    for part in value.split(","):
        if part := part.strip():
            networks.append(ipaddress.ip_network(part, strict=False))
    return tuple(networks)


def _read_options(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as err:
        _LOGGER.warning("Could not read App options %s: %s", path, err)
        return {}
    return data if isinstance(data, dict) else {}


def load_settings(
    env: Mapping[str, str] | None = None, options_path: Path | None = None
) -> Settings:
    """Build the settings from the environment and the App options."""
    env = os.environ if env is None else env
    options = _read_options(options_path or Path(env.get("SL_OPTIONS", OPTIONS_PATH)))

    log_level = str(env.get("SL_LOG_LEVEL") or options.get("log_level") or "info").lower()
    if log_level not in LOG_LEVELS:
        log_level = "info"

    supervisor_url = env.get("SL_SUPERVISOR_URL", DEFAULT_SUPERVISOR_URL).strip() or None
    static_dir = Path(env.get("SL_STATIC_DIR") or Path(__file__).parent / "static")

    return Settings(
        token=env.get("SUPERVISOR_TOKEN") or env.get("SL_HA_TOKEN") or None,
        ha_ws_url=env.get("SL_HA_WS_URL", DEFAULT_HA_WS_URL),
        supervisor_url=supervisor_url.rstrip("/") if supervisor_url else None,
        host=env.get("SL_HOST", "0.0.0.0"),
        port=int(env.get("SL_PORT", DEFAULT_PORT)),
        allowed_networks=_networks(env.get("SL_ALLOWED_CLIENTS", SUPERVISOR_INGRESS_IP)),
        log_level=log_level,
        static_dir=static_dir,
        dev_mode=bool(env.get("SL_DEV")),
        data_dir=Path(env.get("SL_DATA_DIR", DATA_DIR)) if env.get("SL_DATA_DIR", DATA_DIR) else None,
        engine_min_shadow_seconds=_positive(env.get("SL_ENGINE_MIN_SHADOW_SECONDS"), 7200),
        engine_min_transitions=_positive(env.get("SL_ENGINE_MIN_TRANSITIONS"), 20),
    )


def _positive(value: str | None, default: int) -> int:
    try:
        number = int(value) if value is not None else default
    except ValueError:
        return default
    return number if number >= 0 else default
