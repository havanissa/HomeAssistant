"""Phase 4.6.2 (0.6.3): every time the frontend shows is 24-hour HH:mm, in Home Assistant's time zone.

The cards share one helper block (``// <time24>`` … ``// </time24>``). It is run here with Node under
different default locales (en-US, en-GB, Faroese, Danish) and browser time zones, at midnight, noon and
23:59, across date changes and daylight-saving changes, and for the 24-hour time fields. A static check
makes sure no card renders a time through the browser's locale (which shows AM/PM in en-US).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from custom_components.smart_occupancy.card import CARD_DIR

CARDS = sorted(CARD_DIR.glob("*.js"))
BLOCK = re.compile(r"// <time24>.*?// </time24>", re.S)
INTEGRATION = Path(__file__).parent.parent / "custom_components" / "smart_occupancy"

SCRIPT = r"""
const esc = (v) => String(v ?? "");
%BLOCK%
const out = {};
const cases = JSON.parse(process.argv[2]);
for (const c of cases) {
  setTimeZone(c.hass);
  const now = c.now ? new Date(c.now) : new Date(c.iso);
  out[c.id] = { fmt: fmtTime(c.iso, now), full: fullTime(c.iso) };
}
out.parse = Object.fromEntries(["00:00", "12:00", "23:59", "7:30", "0730", "730", "7.30", "07:05", "9", " 21:30 ",
  "24:00", "23:60", "7:3", "12:5x", "", "abc", "7:30 PM", "11:30am"].map((t) => [t, parseHHMM(t)]));
out.locale = new Intl.DateTimeFormat().resolvedOptions().locale;
out.naive = new Date("2026-11-15T15:45:00Z").toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", timeZone: "UTC" });
console.log(JSON.stringify(out));
"""

FAROE = {"config": {"time_zone": "Atlantic/Faroe"}, "locale": {"time_zone": "server"}}
CPH = {"config": {"time_zone": "Europe/Copenhagen"}, "locale": {"time_zone": "server"}}
LOCAL = {"config": {"time_zone": "Atlantic/Faroe"}, "locale": {"time_zone": "local"}}
NOON = "2026-11-15T12:00:00Z"
CASES = [
    # id, hass, iso, now -> expected fmtTime
    ("morning", FAROE, "2026-11-15T07:00:00Z", NOON, "07:00"),
    ("daylight", FAROE, "2026-11-15T08:30:00Z", NOON, "08:30"),
    ("sunset", FAROE, "2026-11-15T15:45:00Z", NOON, "15:45"),
    ("night", FAROE, "2026-11-15T21:30:00Z", NOON, "21:30"),
    ("late", FAROE, "2026-11-15T23:55:00Z", NOON, "23:55"),
    ("midnight", FAROE, "2026-11-15T00:00:00Z", NOON, "00:00"),
    ("after_midnight", FAROE, "2026-11-15T00:15:00Z", NOON, "00:15"),
    ("noon", FAROE, "2026-11-15T12:00:00Z", NOON, "12:00"),
    ("last_minute", FAROE, "2026-11-15T23:59:00Z", NOON, "23:59"),
    ("yesterday", FAROE, "2026-11-14T23:55:00Z", NOON, "14 Nov 23:55"),
    ("tomorrow", FAROE, "2026-11-16T00:05:00Z", "2026-11-15T23:50:00Z", "16 Nov 00:05"),
    # Home Assistant's zone, not the browser's (the browser runs in New York here).
    ("copenhagen", CPH, "2026-11-15T07:00:00Z", NOON, "08:00"),
    ("cph_date_change", CPH, "2026-11-15T23:30:00Z", "2026-11-15T22:00:00Z", "16 Nov 00:30"),
    # The person chose their own (browser) zone in their profile.
    ("local_profile", LOCAL, "2026-11-15T07:00:00Z", NOON, "02:00"),
    # Daylight saving: spring forward (29 March 2026) and fall back (25 October 2026).
    ("faroe_before_spring", FAROE, "2026-03-29T00:59:00Z", "2026-03-29T10:00:00Z", "00:59"),
    ("faroe_after_spring", FAROE, "2026-03-29T01:00:00Z", "2026-03-29T10:00:00Z", "02:00"),
    ("cph_before_spring", CPH, "2026-03-29T00:59:00Z", "2026-03-29T10:00:00Z", "01:59"),
    ("cph_after_spring", CPH, "2026-03-29T01:00:00Z", "2026-03-29T10:00:00Z", "03:00"),
    ("cph_before_fall", CPH, "2026-10-25T00:59:00Z", "2026-10-25T10:00:00Z", "02:59"),
    ("cph_after_fall", CPH, "2026-10-25T01:00:00Z", "2026-10-25T10:00:00Z", "02:00"),
    ("faroe_after_fall", FAROE, "2026-10-25T01:30:00Z", "2026-10-25T10:00:00Z", "01:30"),
    ("bad", FAROE, "not a time", NOON, "not a time"),
]
PARSE = {"00:00": "00:00", "12:00": "12:00", "23:59": "23:59", "7:30": "07:30", "0730": "07:30", "730": "07:30",
         "7.30": "07:30", "07:05": "07:05", "9": "09:00", " 21:30 ": "21:30", "24:00": None, "23:60": None,
         "7:3": None, "12:5x": None, "": None, "abc": None, "7:30 PM": None, "11:30am": None}


def test_every_card_uses_the_same_helper_and_no_locale_time() -> None:
    blocks = {path.name: BLOCK.search(path.read_text(encoding="utf-8")) for path in CARDS}
    with_times = {name: m.group(0) for name, m in blocks.items() if m}
    assert set(with_times) == {"smart-occupancy-curtains-card.js", "smart-occupancy-lighting-cards.js"}
    assert len(set(with_times.values())) == 1, "the 24-hour helper differs between the cards"
    for path in CARDS:
        source = path.read_text(encoding="utf-8")
        for bad in ("toLocaleTimeString", "toLocaleString(", "toLocaleDateString", "hour12", 'type="time"',
                    'hourCycle: "h12"'):
            assert bad not in source, (path.name, bad)
        code = re.sub(r"(?m)^\s*//.*$|/\*.*?\*/", "", source, flags=re.S)   # comments may say "AM/PM"
        assert not re.search(r"\b(AM|PM|a\.m\.|p\.m\.)\b", code), path.name
        if path.name in with_times:
            assert "setTimeZone(hass)" in source                       # Home Assistant's zone is followed
    lighting = (CARD_DIR / "smart-occupancy-lighting-cards.js").read_text(encoding="utf-8")
    assert '${String(h).padStart(2, "0")}:00' in lighting              # chart ticks 00:00, 06:00, 12:00, 18:00
    curtains = (CARD_DIR / "smart-occupancy-curtains-card.js").read_text(encoding="utf-8")
    assert 'placeholder="HH:mm"' in curtains and "parseHHMM(el.value)" in curtains


def test_python_times_are_24_hour() -> None:
    """Explanations made by the integration use %H:%M (never %I or %p)."""
    for path in INTEGRATION.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "%I" not in text and "%p" not in text and "%l" not in text, path


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("locale", ["en_US.UTF-8", "en_GB.UTF-8", "fo_FO.UTF-8", "da_DK.UTF-8"])
def test_times_are_24_hour_in_every_browser_locale(locale: str, tmp_path: Path) -> None:
    block = BLOCK.search((CARD_DIR / "smart-occupancy-curtains-card.js").read_text(encoding="utf-8")).group(0)
    script = tmp_path / "time24.js"
    script.write_text(SCRIPT.replace("%BLOCK%", block), encoding="utf-8")
    cases = [{"id": i, "hass": h, "iso": iso, "now": now} for i, h, iso, now, _ in CASES]
    env = {**os.environ, "LC_ALL": locale, "LANG": locale, "TZ": "America/New_York"}
    run = subprocess.run(["node", str(script), json.dumps(cases)], check=True, capture_output=True, text=True,
                         env=env, cwd=tmp_path)
    out = json.loads(run.stdout)
    for case_id, _h, _iso, _now, expected in CASES:
        assert out[case_id]["fmt"] == expected, (locale, case_id, out[case_id])
    assert out["midnight"]["full"] == "2026-11-15 00:00:00"
    assert out["faroe_after_spring"]["full"] == "2026-03-29 02:00:00"
    assert out["parse"] == PARSE
    for value in out.values():
        if isinstance(value, dict) and "fmt" in value:
            assert not re.search(r"\b(AM|PM|am|pm)\b", value["fmt"] + value["full"])
    assert out["locale"].split("-")[0] == locale[:2], out["locale"]     # Node really ran in that locale
    if locale.startswith("da_DK"):
        assert out["naive"] == "15.45"                                   # Danish uses a dot: not HH:mm either
    if locale.startswith("en_US"):
        # Proof that the test is meaningful: the browser's own en-US time rendering is 12-hour.
        assert out["locale"] == "en-US" and re.search(r"PM", out["naive"]), out
