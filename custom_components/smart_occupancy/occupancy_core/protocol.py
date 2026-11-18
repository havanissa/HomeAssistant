"""The occupancy bridge protocol between the integration and the Smart Lighting App (shared constants).

The integration is the only authority over who publishes occupancy. Its
ownership record has an ``owner`` (``integration`` or ``app``) and an
``epoch`` that increases with every change of owner. The App may publish only
while it holds the current epoch's lease, on the bridge session it opened,
with strictly increasing sequence numbers and the current configuration
revision; everything else is refused with one of the ``REJECT_*`` codes.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Final

PROTOCOL_VERSION: Final = 1

OWNER_INTEGRATION: Final = "integration"
OWNER_APP: Final = "app"

#: An App lease lasts this long; every accepted publish or heartbeat renews it.
LEASE_SECONDS: Final = 30
#: The App sends a heartbeat at least this often while it owns occupancy.
HEARTBEAT_SECONDS: Final = 10
#: After Home Assistant restarts or the integration reloads while the App owned occupancy, the same App
#: process may resume its ownership within this time; otherwise the integration's engine takes over.
RESUME_GRACE_SECONDS: Final = 120
#: Ownership changes kept in the history.
HISTORY_SIZE: Final = 50

#: Refusal codes (websocket error codes).
REJECT_INVALID: Final = "invalid_format"
REJECT_UNKNOWN_SESSION: Final = "unknown_session"
REJECT_NOT_OWNER: Final = "not_owner"
REJECT_STALE_EPOCH: Final = "stale_epoch"
REJECT_LEASE: Final = "lease_invalid"
REJECT_SEQUENCE: Final = "out_of_order"
REJECT_CONFIG: Final = "config_mismatch"
REJECT_VERSION: Final = "version_mismatch"
REJECT_NOT_READY: Final = "not_ready"
REJECT_BUSY: Final = "ownership_changed"

#: Ownership change reasons.
REASON_HANDOVER: Final = "handover"
REASON_RELEASE: Final = "released"
REASON_LEASE_EXPIRED: Final = "lease_expired"
REASON_APP_RESTARTED: Final = "app_restarted"
REASON_NOT_RESUMED: Final = "not_resumed"
REASON_RESUMED: Final = "resumed"
REASON_REVOKED: Final = "revoked"
REASON_CONFIG: Final = "configuration_changed"
REASON_VERSION: Final = "version_changed"

_ID = re.compile(r"^[0-9a-f]{32}$")


def valid_id(value: Any) -> bool:
    """Instance ids, session ids and lease tokens are 32 lower-case hex characters."""
    return isinstance(value, str) and bool(_ID.match(value))


def token_hash(token: str) -> str:
    """Only a digest of a lease token is stored."""
    return hashlib.sha256(token.encode()).hexdigest()
