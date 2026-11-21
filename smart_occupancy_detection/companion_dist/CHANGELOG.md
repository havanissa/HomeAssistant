# Changelog

## 0.2.0

- Published as its own HACS repository.
- Room entities count as fresh only after the App published in its current lease.
- A room missing from the App's snapshot keeps its entities (unavailable); only rooms deleted in the App lose them.
- Diagnostics are redacted (user and instance ids).
- The App recovers by itself after this integration is reloaded.

## 0.1.0

- First release: occupancy entities per room, pairing, lease and sequence fencing, last decision kept while the App
  is away.
