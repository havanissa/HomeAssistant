"""Constants of the Smart Adaptive & Predictive Climate companion integration."""

from __future__ import annotations

from typing import Final

from .protocol import DOMAIN as _DOMAIN

DOMAIN: Final = _DOMAIN
NAME: Final = "Smart Adaptive & Predictive Climate"
VERSION: Final = "0.1.0"  # must match manifest.json (checked by the tests)

STORE_KEY: Final = f"{DOMAIN}.published"
STORE_VERSION: Final = 1
SAVE_DELAY_SECONDS: Final = 5
HISTORY_SIZE: Final = 30

#: Dispatcher signals: the published values changed / the freshness or the pairing changed / rooms or entities
#: appeared, went or were renamed.
SIGNAL_VIEWS: Final = f"{DOMAIN}_views"
SIGNAL_FRESHNESS: Final = f"{DOMAIN}_freshness"
SIGNAL_ROOMS: Final = f"{DOMAIN}_rooms"
#: A room's device was deleted by an administrator: the platforms forget its entities (Home Assistant removes them).
SIGNAL_FORGET: Final = f"{DOMAIN}_forget"
