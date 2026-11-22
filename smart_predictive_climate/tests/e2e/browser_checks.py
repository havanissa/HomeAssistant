"""Browser checks of the interface through Ingress (Playwright, headless Chromium; never a production instance).

Every page - the overview, Add a room, the six tabs of a room, thermal zones and Settings - at desktop, tablet
(both orientations) and phone widths, light and dark: no console errors, no horizontal overflow, screenshots.
On the desktop a simulation is run in the interface (its chart appears; nothing is sent to a device - see
``e2e_checks.py commands``), the window editor is opened, and the main navigation is used with the keyboard.

    python browser_checks.py <screenshot dir>

Run after ``e2e_checks.py`` (it leaves the room Kamar suður configured and trained).
"""

from __future__ import annotations

import glob
import os
from pathlib import Path
import sys

from playwright.sync_api import sync_playwright

PANEL = "http://127.0.0.1:18500/hassio/ingress/spc"
SIZES = {"desktop": (1440, 900), "tablet": (1024, 768), "tablet-portrait": (768, 1024), "phone": (390, 844)}
ROOM = "kamar_sudur"
TABS = ("overview", "history", "model", "prediction", "simulate", "settings")


def chromium() -> str | None:
    found = glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell"))
    return sorted(found)[-1] if found else None


def main(out: Path) -> int:  # noqa: PLR0915 - one linear scenario
    out.mkdir(parents=True, exist_ok=True)
    fails = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal fails
        fails += 0 if ok else 1
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  -> {detail}"), flush=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium())
        for label, (width, height) in SIZES.items():
            for scheme in ("light", "dark"):
                page = browser.new_page(viewport={"width": width, "height": height}, color_scheme=scheme,
                                        has_touch=label == "phone")
                errors: list[str] = []
                page.on("console", lambda m, e=errors: e.append(m.text) if m.type == "error" else None)
                page.on("pageerror", lambda err, e=errors: e.append(str(err)))
                page.goto(PANEL)
                frame = page.frame_locator("#ingress")
                frame.locator("h1").first.wait_for()
                inner = page.frames[1]
                tag = f"{label}-{scheme}"
                wide: list[str] = []

                def go(route: str, name: str, heading: str | None = None, inner=inner, frame=frame, tag=tag,
                       wide=wide) -> None:
                    inner.evaluate(f"location.hash = {route!r}")
                    if heading:
                        frame.locator("h1", has_text=heading).first.wait_for()
                    else:
                        frame.locator("h1").first.wait_for()
                    inner.wait_for_timeout(500)
                    if inner.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth + 1"):
                        wide.append(name)
                    frame.locator("body").screenshot(path=str(out / f"{tag}-{name}.png"))

                go("#/", "overview", "Overview")
                check(f"{tag}: the overview shows the room card",
                      frame.locator(f'a.card.room[data-room="{ROOM}"]').count() == 1)
                go("#/add", "add", "Add a room")
                for t in TABS:
                    go(f"#/rooms/{ROOM}/{t}", f"room-{t}", "Kamar")
                    if t in ("history", "prediction"):
                        frame.locator("svg.chart").first.wait_for(timeout=15000)
                go("#/zones", "zones", "Thermal zones")
                go("#/settings", "settings", "Settings")
                check(f"{tag}: no horizontal overflow on any page", not wide, ", ".join(wide))
                bg = inner.evaluate("getComputedStyle(document.body).backgroundColor")
                dark = sum(int(x) for x in bg[bg.index("(") + 1:bg.index(")")].split(",")[:3]) < 300
                check(f"{tag}: the {scheme} colour scheme is applied", dark == (scheme == "dark"), bg)

                if label == "desktop":
                    go(f"#/rooms/{ROOM}/simulate", "simulate-before", "Kamar")
                    frame.locator('button[data-act="simulate"]').click()
                    frame.locator("#sim-result svg.chart").wait_for(timeout=20000)
                    frame.locator("#sim-result").screenshot(path=str(out / f"{tag}-simulation.png"))
                    text = frame.locator("#sim-result").inner_text()
                    check(f"{tag}: a simulation draws its chart, labelled hypothetical", "hypothetical" in text, text[:200])
                    go(f"#/rooms/{ROOM}/settings", "room-settings", "Kamar")
                    before = frame.locator('[data-act="orient"]').count()
                    frame.locator('button[data-act="add-window"]').click()
                    frame.locator('[data-act="orient"]').first.wait_for()
                    check(f"{tag}: the window editor opens a window with direction buttons",
                          frame.locator('[data-act="orient"]').count() > before)
                    frame.locator("body").screenshot(path=str(out / f"{tag}-window-editor.png"))
                    body = frame.locator("body").inner_text()
                    check(f"{tag}: the editor states the 0° = South convention", "0° = South" in body or "0° = south" in body)
                    page.reload()
                    frame.locator("h1").first.wait_for()
                    inner = page.frames[1]
                    inner.evaluate("location.hash = '#/'")
                    frame.locator("h1", has_text="Overview").first.wait_for()
                    reached = None
                    for _ in range(25):
                        page.keyboard.press("Tab")
                        href = inner.evaluate("document.activeElement && document.activeElement.getAttribute('href')")
                        if href == "#/settings":
                            reached = href
                            break
                    check(f"{tag}: the navigation is reachable with the keyboard", reached == "#/settings")
                    if reached:
                        page.keyboard.press("Enter")
                        frame.locator("h1", has_text="Settings").first.wait_for()
                        outline = inner.evaluate("getComputedStyle(document.activeElement).outlineStyle")
                        check(f"{tag}: Enter opens the focused page, focus is visible", outline not in ("none", ""), outline)
                check(f"{tag}: no console errors", not errors, "; ".join(errors[:5]))
                page.close()
        browser.close()
    print(f"{fails} failed")
    return fails


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
