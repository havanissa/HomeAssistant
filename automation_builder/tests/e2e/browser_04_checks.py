"""Browser checks of Phase 0.3 through Ingress: the template gallery, the Lighting Designer's matrices (select, edit
many cells, copy and paste, clear, defaults), presence sources with a drawn position zone, the Cover Designer's
sun-angle settings on the compass, the simulation and the YAML preview - at desktop (light and dark), tablet and
phone widths, without console errors and without sideways scrolling.

    python browser_04_checks.py <screenshot dir>

Run after ``e2e_04_checks.py`` (it creates its own devices and sources).
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import urllib.request

from playwright.sync_api import sync_playwright

from browser_checks import API, PANEL, SIZES, chromium

BED, OFFICE, HALL = "light.bed_light", "light.office_rgbw_lights", "cover.hall_window"


def api(path: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(f"{API}/{path}", data=json.dumps(body).encode() if body is not None else None,
                                     headers={"X-AB": "1", "Content-Type": "application/json"}, method="POST" if body is not None else "GET")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def change(path: str, body: dict) -> dict:
    return api(path, {**body, "expected_revision": api("overview")["revision"]})


def designer(device_id: str) -> dict:
    items = api(f"devices/{device_id}")["designers"]["items"]
    return items[0]["config"] if items else {}


def main(out: Path) -> int:  # noqa: PLR0915 - one linear scenario
    out.mkdir(parents=True, exist_ok=True)
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, bool(ok), detail))
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  -> {detail}"), flush=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium())
        for label, (width, height) in SIZES.items():
            for scheme in (("light", "dark") if label == "desktop" else ("light",)):
                tag = f"{label}-{scheme}"
                lights = change("devices/create", {"name": f"Browser lights {tag}", "targets": [BED, OFFICE]})["device_id"]
                blinds = change("devices/create", {"name": f"Browser blinds {tag}", "targets": [HALL]})["device_id"]
                page = browser.new_page(viewport={"width": width, "height": height}, color_scheme=scheme, has_touch=label == "phone")
                errors: list[str] = []
                page.on("console", lambda m, e=errors: e.append(m.text) if m.type == "error" else None)
                page.on("pageerror", lambda err, e=errors: e.append(str(err)))
                page.on("response", lambda r, e=errors: e.append(f"HTTP {r.status} {r.request.method} {r.url.split('/api/')[-1]}: {r.text()[:200]}")
                        if r.status >= 400 else None)
                page.goto(PANEL)
                frame = page.frame_locator("#ingress")
                frame.locator("h1").first.wait_for()
                inner = page.frames[1]

                def go(route: str, heading: str, inner=inner, frame=frame) -> None:
                    inner.evaluate(f"location.hash = {route!r}")
                    frame.locator("h1", has_text=heading).first.wait_for()
                    inner.wait_for_timeout(300)

                def saved(frame=frame, inner=inner) -> bool:
                    try:
                        frame.locator("#d-status.ok, #d-status:has-text('Saved')").first.wait_for(timeout=8000)
                    except Exception:  # noqa: BLE001
                        return False
                    inner.wait_for_timeout(150)
                    return True

                def overflow(inner=inner) -> bool:
                    return inner.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth + 1")

                def shot(name: str, frame=frame, tag=tag) -> None:
                    frame.locator("body").screenshot(path=str(out / f"{tag}-{name}.png"))

                def cell(r: str, c: str, frame=frame):
                    return frame.locator(f"button.mcell[data-r='{r}'][data-c='{c}']").first

                # ------------------------------------------------ the gallery
                go(f"#/gallery/{lights}", "New automation")
                cards = frame.locator(".card.tpl[data-template]")
                check(f"{tag}: the gallery shows the lighting templates for a light", cards.count() == 5
                      and frame.locator("[data-template=mood_lighting]").count() == 1 and frame.locator("text=Planned").count() == 1, str(cards.count()))
                check(f"{tag}: the gallery does not overflow", not overflow())
                shot("gallery")
                frame.locator("[data-template=mood_lighting] button[data-act=use]").click()
                frame.locator("nav.tabs").wait_for()
                check(f"{tag}: a template opens the designer with its controls on and nothing bound",
                      designer(lights).get("primary") == "mood" and designer(lights)["lights"] == [] and designer(lights)["mood"]["entity_id"] is None)
                check(f"{tag}: what is missing is shown", frame.locator("#d-problems").count() == 1)

                # ------------------------------------------------ rows and the mood source
                for light in (BED, OFFICE):
                    frame.locator(f"input[data-chg=row][data-id='{light}']").check()
                    saved()
                check(f"{tag}: lights are chosen as rows explicitly", designer(lights)["lights"] == [BED, OFFICE], designer(lights)["lights"])
                frame.locator("nav.tabs a", has_text="Mood").first.click()
                frame.locator("button.entity[data-path='mood/entity_id']").click()
                frame.locator("#pick-q").fill("Mood")
                frame.locator("button.result", has_text="input_select.mood").first.click()
                saved()
                for value in ("Dagur", "Hygge"):
                    frame.locator(f"input[data-chg=value][value='{value}']").check()
                    saved()
                check(f"{tag}: the mood source and the columns are chosen", designer(lights)["mood"]["entity_id"] == "input_select.mood"
                      and designer(lights)["mood"]["values"] == ["Dagur", "Hygge"], designer(lights)["mood"])
                check(f"{tag}: every cell starts UNSET", frame.locator("table.matrix button.mcell.st-unset").count() == 6)

                # ------------------------------------------------ cells
                cell(BED, "Hygge").click()
                frame.locator(".cell-editor label.kind:has(input[value='on'])").click()
                saved()
                frame.locator(".cell-editor input[type=number][data-key=brightness_pct]").fill("40")
                frame.locator(".cell-editor input[type=number][data-key=brightness_pct]").dispatch_event("change")
                saved()
                check(f"{tag}: a cell is set to ON 40 %", cell(BED, "Hygge").inner_text().startswith("40%")
                      and designer(lights)["mood"]["cells"][BED]["Hygge"]["brightness_pct"] == 40, cell(BED, "Hygge").inner_text())
                capable = designer(lights)["mood"]["cells"][BED]["Hygge"]["color_temp_kelvin"] is None
                check(f"{tag}: attributes nobody set stay unchanged (no invented value)", capable)
                frame.locator("button[data-act=mnone]").click()
                cell(BED, "Dagur").click()
                cell(OFFICE, "Dagur").click()
                frame.locator(".cell-editor label.kind:has(input[value='off'])").click()
                saved()
                cfg = designer(lights)["mood"]["cells"]
                check(f"{tag}: two cells are edited at once (OFF)", cfg[BED]["Dagur"]["kind"] == "off" and cfg[OFFICE]["Dagur"]["kind"] == "off", json.dumps(cfg))
                frame.locator("button[data-act=mnone]").click()
                frame.locator("button[data-act=mcol][data-c='Hygge']").click()
                frame.locator("button[data-act=mcopy]").click()
                frame.locator("button[data-act=mcol][data-c='Dagur']").click()
                frame.locator("button[data-act=mpaste]").click()
                saved()
                cfg = designer(lights)["mood"]["cells"]
                check(f"{tag}: a mood column is copied onto another (an unset cell copies as unset)",
                      cfg[BED]["Dagur"]["brightness_pct"] == 40 and "Dagur" not in cfg.get(OFFICE, {}), json.dumps(cfg))
                frame.locator("button[data-act=mnone]").click()
                cell(BED, "Dagur").click()
                frame.locator("button[data-act=mclear]").click()
                saved()
                check(f"{tag}: a cell is set back to Unset", "Dagur" not in designer(lights)["mood"]["cells"].get(BED, {}) and cell(BED, "Dagur").inner_text() == "UNSET")
                frame.locator("button[data-act=mnone]").click()
                cell(OFFICE, "__row").click()
                frame.locator(".cell-editor label.kind:has(input[value='on'])").click()
                saved()
                check(f"{tag}: a row default fills the light's unset cells, shown as DEFAULT with its source",
                      cell(OFFICE, "Hygge").inner_text().startswith("DEFAULT: ON") and "row default" in cell(OFFICE, "Hygge").inner_text()
                      and designer(lights)["mood"]["row_defaults"][OFFICE]["kind"] == "on", cell(OFFICE, "Hygge").inner_text())
                frame.locator("button[data-act=mnone]").click()
                cell("__matrix", "__matrix").click()
                check(f"{tag}: a default cannot be “Use default”", frame.locator(".cell-editor label.kind:has(input[value='default'])").count() == 0)
                frame.locator(".cell-editor label.kind:has(input[value='none'])").click()
                saved()
                check(f"{tag}: the matrix default is set explicitly", designer(lights)["mood"]["default"] == {"kind": "none"})
                check(f"{tag}: cells are large enough to tap", frame.locator("table.matrix button.mcell").first.bounding_box()["height"] >= 44)
                check(f"{tag}: the matrix page does not overflow", not overflow())
                shot("mood-matrix")

                # ------------------------------------------------ simulation and YAML
                frame.locator("nav.tabs a", has_text="Simulate").click()
                frame.locator("#sim-trigger").wait_for()
                frame.locator("#sim-inputs").wait_for()
                option = frame.locator("#sim-trigger option", has_text="“Hygge”").first.get_attribute("value")
                frame.locator("#sim-trigger").select_option(option)
                frame.locator("input[data-chg=simstate][data-id='input_select.mood']").fill("Hygge")
                frame.locator("input[data-chg=simstate][data-id='input_select.mood']").dispatch_event("change")
                frame.locator("button[data-act=simrun]").click()
                frame.locator("#sim-result").wait_for()
                text = frame.locator("#sim-result").inner_text()
                check(f"{tag}: the simulation shows the expected command and marks test values", "brightness_pct=40" in text
                      and frame.locator("#sim-inputs .badge.warn", has_text="test value").count() == 1, text[:300])
                shot("simulate")
                frame.locator("nav.tabs a", has_text="YAML").click()
                frame.locator("#d-yaml").wait_for()
                check(f"{tag}: the YAML preview is the generated automation", "brightness_pct: 40" in frame.locator("#d-yaml").inner_text()
                      and "Rule: Lighting" in frame.locator("#d-yaml").inner_text())
                check(f"{tag}: the YAML page does not overflow", not overflow())

                # ------------------------------------------------ presence sources: a drawn position zone
                go("#/presence", "Presence sources")
                frame.locator("button[data-act=new]").click()
                frame.locator("#z-name").fill(f"Desk {tag}")
                frame.locator("#z-name").dispatch_event("change")
                frame.locator("#z-kind").select_option("position")
                frame.locator("button.entity[data-path='position.targets.0.x.entity_id']").click()
                frame.locator("#pick-q").fill("radar target x")
                frame.locator("button.result", has_text="sensor.radar_target_x").first.click()
                inner.wait_for_timeout(600)
                frame.locator("button.entity[data-path='position.targets.0.y.entity_id']").click()
                frame.locator("#pick-q").fill("radar target y")
                frame.locator("button.result", has_text="sensor.radar_target_y").first.click()
                inner.wait_for_timeout(600)
                canvas = frame.locator("svg.zone-canvas")
                box = canvas.bounding_box()
                for fx, fy in ((0.3, 0.3), (0.7, 0.3), (0.7, 0.7)):
                    canvas.click(position={"x": box["width"] * fx, "y": box["height"] * fy})
                    inner.wait_for_timeout(500)
                found = [s for s in api("presence")["sources"] if s["name"] == f"Desk {tag}"]
                check(f"{tag}: one source is created, also when its kind is changed right after its name", len(found) == 1, str(len(found)))
                check(f"{tag}: a position zone is drawn by tapping the canvas", found and len(found[0]["position"]["shape"]["points"]) == 3
                      and found[0]["position"]["targets"][0]["x"]["entity_id"] == "sensor.radar_target_x", json.dumps(found)[:300])
                check(f"{tag}: the presence page does not overflow", not overflow())
                shot("zone")

                # ------------------------------------------------ the cover designer: sun angle on the compass
                go(f"#/gallery/{blinds}", "New automation")
                frame.locator("[data-template=sun_shading] button[data-act=use]").click()
                frame.locator("nav.tabs").wait_for()
                frame.locator(f"input[data-chg=row][data-id='{HALL}']").check()
                saved()
                frame.locator("nav.tabs a", has_text="Sun Position").click()
                frame.locator("button[data-act=winsetup]").click()
                saved()
                compass = frame.locator("svg.compass").first
                box = compass.bounding_box()
                compass.click(position={"x": box["width"] * 0.95, "y": box["height"] * 0.5})   # tap East
                saved()
                orientation = designer(blinds)["sun"]["windows"][HALL]["orientation"]
                check(f"{tag}: tapping East on the compass sets the window to face East (-90°, 0° = South)", orientation is not None and abs(orientation + 90) <= 3, str(orientation))
                for path, value in (("left", 50), ("right", 50), ("elevation_min", 5)):
                    field = frame.locator(f"input[data-path='sun/windows/{HALL}/{path}']")
                    field.fill(str(value))
                    field.dispatch_event("change")
                    saved()
                frame.locator(f"select[data-path='sun/windows/{HALL}/position']").select_option("fixed")
                saved()
                field = frame.locator(f"input[data-path='sun/windows/{HALL}/position/value']")
                field.fill("40")
                field.dispatch_event("change")
                saved()
                window = designer(blinds)["sun"]["windows"][HALL]
                check(f"{tag}: the sun-angle window is configured", (window["left"], window["right"], window["elevation_min"], window["position"]) ==
                      (50, 50, 5, {"kind": "fixed", "value": 40}), json.dumps(window))
                check(f"{tag}: the compass shows the sector", frame.locator("svg.compass path.wedge").count() == 1)
                check(f"{tag}: a preview of the shading is calculated", "Preview" in frame.locator(".preview").first.inner_text())
                check(f"{tag}: the sun page does not overflow", not overflow())
                shot("sun")
                frame.locator("nav.tabs a", has_text="Cover Moods").click()
                frame.locator("button[data-act=modeidea][data-name=Privacy]").click()
                saved()
                check(f"{tag}: cover modes are named by the user (suggestions are only offered)", designer(blinds)["modes"]["names"] == ["Privacy"])
                check(f"{tag}: no console errors", not errors, "; ".join(errors[:3]))
                page.close()
                change("devices/delete", {"device_id": lights, "overwrite": True})
                for source in found:
                    change("presence/delete", {"source_id": source["id"]})
                change("devices/delete", {"device_id": blinds, "overwrite": True})
        browser.close()
    passed = sum(1 for _n, ok, _d in results if ok)
    print(f"\n{passed} of {len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1] if len(sys.argv) > 1 else "shots")))
