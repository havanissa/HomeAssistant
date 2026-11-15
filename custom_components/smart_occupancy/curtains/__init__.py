"""Smart Window & Curtain Controller (0.6.1, the user's Phase 4.6.0).

A window belongs to one Home Assistant area and has one or more cover layers
(``cover.*`` entities: blackout, sheer, shade or custom). Every window has its
own preset library: named, coordinated target positions with one target (or
"Leave unchanged") per layer. Presets and windows have stable ids, so later
automatic rules can refer to them after a rename.

What this build does:

* stores windows, layers, presets, orientation and the future condition ->
  preset mapping in ``.storage/smart_occupancy.curtains`` (never in the config
  entry, never in the lighting store, never mixed with lighting moods);
* shows live positions, the recognised preset (or Custom), the sun's position
  against each window and competing automations;
* applies a preset **only** when an administrator explicitly asks for it and
  confirms the exact targets (``curtains/apply`` with ``confirm: true``). The
  integration then sends the cover commands, verifies the reported positions
  and records the result per cover.

Automatic (0.6.2, the user's Phase 4.6.1): a window an administrator
explicitly switched to Automatic decides on its own which preset applies
(``logic.py``: Sleep, Night, Evening privacy, Waiting for daylight, Sun
protection, Window lamp, Morning, Day, manual holds) and sends it through the
same executor after the safety limits of ``auto.py``. Settings are resolved
window → room → all windows (``rules.py``).

What it never does: move a cover of an Observing or Off window on its own,
open a window without measured daylight (or the explicitly allowed sun
fallback), open a sleep-protected window during sleep, undo a manual move,
replay an interrupted movement after a restart, read or write a lighting mood,
or change, disable or delete an existing automation.
"""
