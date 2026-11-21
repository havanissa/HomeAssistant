"""The occupancy engine of Smart Occupancy Detection (pure Python, standard library only).

A standalone copy of Smart Comfort's ``occupancy_core`` 1.0.0, never imported
from there, so this App keeps working when Smart Comfort is removed. It never
reads a clock and does no I/O apart from reading its own golden fixtures.

* ``types`` - sensor kinds and roles, states, timing parameters;
* ``engine`` - the per-room state machine (evidence fusion, vacancy rules);
* ``house`` - one engine per room, the routing of entities and adjacent-room movement;
* ``golden`` - the recorded regression scenarios and the start-up self-test.
"""

from __future__ import annotations

#: Version of this engine copy. Bumped with every change of a rule.
ENGINE_VERSION = "1.0.0"
#: The Smart Comfort engine it was copied from (``occupancy_core.CORE_VERSION``).
DERIVED_FROM = "occupancy_core 1.0.0"
