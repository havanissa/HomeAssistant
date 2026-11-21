"""Runtime settings, read from the environment and the App options.

On Home Assistant OS everything comes from the Supervisor: ``SUPERVISOR_TOKEN``
(environment) and ``/data/options.json`` (the App's Configuration tab). The
``AB_*`` variables exist only for development and tests, for example to run
the backend against a local Home Assistant.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import ipaddress
import json
import logging
import os
from pathlib import Path

from .const import DATA_DIR, DEFAULT_HA_API_URL, DEFAULT_HA_WS_URL, DEFAULT_PORT, OPTIONS_PATH, SUPERVISOR_INGRESS_IP

_LOGGER = logging.getLogger(__name__)

LOG_LEVELS = ("debug", "info", "warning", "error")
IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


@dataclass(frozen=True)
class Settings:
    """Backend settings. The token is never part of repr() or any output."""

    token: str | None = field(repr=False)
    ha_ws_url: str
    ha_api_url: str
    host: str
    port: int
    allowed_networks: tuple[IpNetwork, ...]
    log_level: str
    static_dir: Path
    data_dir: Path
    dev_mode: bool

    def is_allowed(self, remote: str | None) -> bool:
        """Only the Supervisor's Ingress may reach the App (an empty allowlist allows everyone: development)."""
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
        return {
            "home_assistant_websocket": self.ha_ws_url,
            "home_assistant_api": self.ha_api_url,
            "listen": f"{self.host}:{self.port}",
            "allowed_clients": [str(n) for n in self.allowed_networks] or ["any"],
            "log_level": self.log_level,
            "token": "present (hidden)" if self.token else "missing",
            "development_mode": self.dev_mode,
            "data_dir": str(self.data_dir),
        }


def _networks(value: str) -> tuple[IpNetwork, ...]:
    if value.strip() in {"*", "any"}:
        return ()
    return tuple(ipaddress.ip_network(p.strip(), strict=False) for p in value.split(",") if p.strip())


def _read_options(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as err:
        _LOGGER.warning("Could not read App options %s: %s", path, err)
        return {}
    return data if isinstance(data, dict) else {}


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    options = _read_options(Path(env.get("AB_OPTIONS", OPTIONS_PATH)))
    log_level = str(env.get("AB_LOG_LEVEL") or options.get("log_level") or "info").lower()
    if log_level not in LOG_LEVELS:
        log_level = "info"
    return Settings(
        token=env.get("SUPERVISOR_TOKEN") or env.get("AB_HA_TOKEN") or None,
        ha_ws_url=env.get("AB_HA_WS_URL", DEFAULT_HA_WS_URL),
        ha_api_url=env.get("AB_HA_API_URL", DEFAULT_HA_API_URL).rstrip("/"),
        host=env.get("AB_HOST", "0.0.0.0"),
        port=int(env.get("AB_PORT", DEFAULT_PORT)),
        allowed_networks=_networks(env.get("AB_ALLOWED_CLIENTS", SUPERVISOR_INGRESS_IP)),
        log_level=log_level,
        static_dir=Path(env.get("AB_STATIC_DIR") or Path(__file__).parent / "static"),
        data_dir=Path(env.get("AB_DATA_DIR", DATA_DIR)),
        dev_mode=bool(env.get("AB_DEV")),
    )
