"""In-process signals (``homeassistant.helpers.dispatcher`` subset)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..core import CALLBACK_TYPE, HomeAssistant, _run

_KEY = "_host_dispatcher"


def async_dispatcher_connect(hass: HomeAssistant, signal: str, target: Callable[..., Any]) -> CALLBACK_TYPE:
    targets: dict[str, list[Callable[..., Any]]] = hass.data.setdefault(_KEY, {})
    targets.setdefault(signal, []).append(target)

    def remove() -> None:
        items = targets.get(signal, [])
        if target in items:
            items.remove(target)

    return remove


def async_dispatcher_send(hass: HomeAssistant, signal: str, *args: Any) -> None:
    for target in list(hass.data.get(_KEY, {}).get(signal, ())):
        _run(hass, target, *args)


dispatcher_send = async_dispatcher_send
