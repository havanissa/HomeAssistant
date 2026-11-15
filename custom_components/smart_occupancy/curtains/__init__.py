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

What it never does: move a cover automatically (Automatic mode is reserved
for a later phase and refused by the safety gate), move anything at discovery,
installation, migration or restart, read or write a lighting mood, or change,
disable or delete an existing automation.
"""
