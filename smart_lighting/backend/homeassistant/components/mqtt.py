"""MQTT through Home Assistant (``homeassistant.components.mqtt`` subset).

The App never connects to a broker: it subscribes through Home Assistant's own
MQTT integration (websocket ``mqtt/subscribe``, administrators only). Without the
MQTT integration every function reports "not available", so installations that
use other inputs need no broker.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..core import CALLBACK_TYPE, HomeAssistant, _run
from ..exceptions import HomeAssistantError

DOMAIN = "mqtt"
_STATUS = "_host_mqtt_status"


@dataclass(slots=True, frozen=True)
class ReceiveMessage:
    topic: str
    payload: Any
    qos: int
    retain: bool
    subscribed_topic: str
    timestamp: datetime | None = None


def _available(hass: HomeAssistant) -> bool:
    return DOMAIN in hass.config.components and bool(hass.config_entries.async_entries(DOMAIN))


async def async_wait_for_mqtt_client(hass: HomeAssistant) -> bool:
    return _available(hass)


def is_connected(hass: HomeAssistant) -> bool:
    return _available(hass) and hass.backend is not None and hass.backend.mqtt_connected()


def async_subscribe_connection_status(hass: HomeAssistant, connection_status_callback: Callable[[bool], Any]) -> CALLBACK_TYPE:
    callbacks: list[Callable[[bool], Any]] = hass.data.setdefault(_STATUS, [])
    callbacks.append(connection_status_callback)

    def remove() -> None:
        if connection_status_callback in callbacks:
            callbacks.remove(connection_status_callback)

    return remove


def async_connection_changed(hass: HomeAssistant, connected: bool) -> None:
    """Host interface: the MQTT integration's connection changed."""
    for cb in list(hass.data.get(_STATUS, ())):
        _run(hass, cb, connected)


async def async_subscribe(hass: HomeAssistant, topic: str, msg_callback: Callable[[ReceiveMessage], Any],
                          qos: int = 0, encoding: str | None = "utf-8") -> CALLBACK_TYPE:
    if not _available(hass) or hass.backend is None:
        raise HomeAssistantError("MQTT integration is not set up")

    def handler(message: dict[str, Any]) -> None:
        payload = message.get("payload")
        if encoding is None and isinstance(payload, str):
            payload = payload.encode()
        _run(hass, msg_callback, ReceiveMessage(
            topic=str(message.get("topic") or topic), payload=payload, qos=int(message.get("qos") or 0),
            retain=bool(message.get("retain")), subscribed_topic=topic))

    return await hass.backend.async_mqtt_subscribe(topic, handler)
