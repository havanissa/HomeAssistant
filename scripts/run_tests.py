"""Run the test suite, also on Home Assistant releases newer than the test harness.

pytest-homeassistant-custom-component is released for each Home Assistant
version. When the newest harness is older than the Home Assistant release
under test (e.g. harness 0.13.375 = HA 2026.10.4, tested against 2026.11.1),
install that Home Assistant over the harness and run the tests through this
script. The only incompatibility is a helper that moved in 2026.11; it is
aliased here before the harness imports it. Nothing else is patched.
"""

import sys

import homeassistant  # noqa: F401
import homeassistant.components.logger as logger_pkg
from homeassistant.components.logger import helpers as logger_helpers

if not hasattr(logger_pkg, "_clear_logger_overwrites"):  # moved in 2026.11
    logger_pkg._clear_logger_overwrites = logger_helpers._clear_logger_overwrites

import pytest  # noqa: E402

sys.exit(pytest.main(sys.argv[1:]))
