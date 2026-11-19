"""Run the Home Assistant test suites, also on Home Assistant releases newer than the test harness.

pytest-homeassistant-custom-component is released for each Home Assistant
version. When the newest harness is older than the Home Assistant release
under test (e.g. harness 0.13.375 = HA 2026.10.4, tested against 2026.11.1),
install that Home Assistant over the harness and run the tests through this
script. The only incompatibility is a helper that moved in 2026.11; it is
aliased here before the harness imports it. Nothing else is patched.

Two suites exist and must run in separate processes, because both import
``custom_components.smart_occupancy`` (the companion integration, and the 0.9.0
engine harness in ``tests/engine/custom_components``):

    python scripts/run_tests.py tests/companion   # the minimal companion (Phase A6)
    python scripts/run_tests.py tests/engine      # the App's engines in a real Home Assistant
    python scripts/run_tests.py                   # both, one after the other
"""

import subprocess
import sys

import homeassistant  # noqa: F401
import homeassistant.components.logger as logger_pkg
from homeassistant.components.logger import helpers as logger_helpers

if not hasattr(logger_pkg, "_clear_logger_overwrites"):  # moved in 2026.11
    logger_pkg._clear_logger_overwrites = logger_helpers._clear_logger_overwrites

import pytest  # noqa: E402

SUITES = ("tests/companion", "tests/engine")


def main(argv: list[str]) -> int:
    if any(a.startswith("tests/") for a in argv):
        return pytest.main(argv)
    code = 0
    for suite in SUITES:
        result = subprocess.run([sys.executable, __file__, suite, *argv], check=False)
        code = code or result.returncode
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
