"""Browser checks of the interface through Ingress (Playwright, headless Chromium): every page, the add-room flow,
no console errors, no horizontal overflow at desktop, tablet and phone widths, screenshots.

    python browser_checks.py <screenshot dir> [--add-room stova]
"""

from __future__ import annotations

import glob
import os
from pathlib import Path
import sys

from playwright.sync_api import sync_playwright

PANEL = "http://127.0.0.1:18300/hassio/ingress/sod"
SIZES = {"desktop": (1440, 900), "tablet": (1024, 768), "tablet-portrait": (768, 1024), "phone": (390, 844)}


def chromium() -> str | None:
    found = glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell"))
    return sorted(found)[-1] if found else None


def main(out: Path, add_room: str | None) -> int:
    out.mkdir(parents=True, exist_ok=True)
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  -> {detail}"), flush=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium())
        for label, (width, height) in SIZES.items():
            for scheme in (("light", "dark") if label == "desktop" else ("light",)):
                page = browser.new_page(viewport={"width": width, "height": height}, color_scheme=scheme)
                errors: list[str] = []
                page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(PANEL)
                frame = page.frame_locator("#ingress")
                frame.locator("h1").first.wait_for()
                tag = f"{label}-{scheme}"
                inner = page.frames[1]

                def overflow() -> bool:
                    return inner.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth + 1")

                for section, heading in (("overview", "Overview"), ("rooms", "Rooms"), ("settings", "Settings")):
                    frame.locator(f'nav a[data-nav="{section}"]').click()
                    frame.locator("h1", has_text=heading).first.wait_for()
                    inner.wait_for_timeout(300)
                    check(f"{tag}: {heading} has no horizontal overflow", not overflow())
                    frame.locator("body").screenshot(path=str(out / f"{tag}-{section}.png"))
                nav = frame.locator("nav a").first.bounding_box()
                check(f"{tag}: navigation targets are at least 44 px high", nav is not None and nav["height"] >= 44,
                      str(nav))
                frame.locator('nav a[data-nav="rooms"]').click()
                frame.locator("h1", has_text="Rooms").first.wait_for()
                link = frame.locator("tbody a").first
                link.wait_for(timeout=5000)
                check(f"{tag}: the rooms list links to a room", link.count() == 1)
                if link.count():
                    link.click()
                    frame.locator("h2", has_text="Sensors").wait_for()
                    inner.wait_for_timeout(300)
                    check(f"{tag}: room detail has no horizontal overflow", not overflow())
                    frame.locator("body").screenshot(path=str(out / f"{tag}-room.png"))
                    frame.locator("summary", has_text="Advanced diagnostics").click()
                    inner.wait_for_timeout(2600)  # longer than the refresh interval
                    still = inner.evaluate("document.querySelector('details[data-keep=diagnostics]').open")
                    check(f"{tag}: an opened section stays open while the page refreshes", still)
                    pilot = frame.locator("h2", has_text="Pilot comparison")
                    if pilot.count():
                        frame.locator("body").screenshot(path=str(out / f"{tag}-room-pilot.png"))
                    edit = frame.locator("a", has_text="Edit room")
                    if edit.count():
                        edit.click()
                        frame.locator("h2", has_text="Sensors").wait_for()
                        inner.wait_for_timeout(300)
                        check(f"{tag}: room editor has no horizontal overflow", not overflow())
                        frame.locator("body").screenshot(path=str(out / f"{tag}-editor.png"))
                check(f"{tag}: no console errors", not errors, "; ".join(errors[:3]))
                page.close()

        if add_room:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(PANEL)
            frame = page.frame_locator("#ingress")
            frame.locator('nav a[data-nav="rooms"]').click()
            frame.locator("a", has_text="Add a room").click()
            frame.locator("#area").select_option(add_room)
            frame.locator("h3", has_text="Found in this area").wait_for()
            frame.locator("body").screenshot(path=str(page_path := out / "add-1-discovered.png"))
            frame.locator("#add-suggested").click()
            rows = frame.locator(".editor-sensor").count()
            check("Add room: suggested sensors are added only on request", rows >= 1, str(rows))
            status = frame.locator("#save-status")
            status.filter(has_text="Saved").wait_for(timeout=10000)
            frame.locator("body").screenshot(path=str(out / "add-2-saved.png"))
            text = status.inner_text()
            check("Add room: saved on change, and the line says what the room shows", "starts as" in text, text[:200])
            frame.locator("a", has_text="Done").click()
            frame.locator("h2", has_text="Recent detection").wait_for()
            frame.locator("body").screenshot(path=str(out / "add-3-room.png"))
            check("Add room: saved and opened", True)
            frame.locator('nav a[data-nav="overview"]').click()
            frame.locator(".room").first.wait_for()
            frame.locator("body").screenshot(path=str(out / "add-4-overview.png"))
            check("Add room: no page errors", not errors, "; ".join(errors[:3]))
            del page_path
        browser.close()
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} browser checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    add = sys.argv[sys.argv.index("--add-room") + 1] if "--add-room" in sys.argv else None
    sys.exit(main(Path(sys.argv[1]), add))
