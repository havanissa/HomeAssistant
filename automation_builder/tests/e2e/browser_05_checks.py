"""Browser checks of App 0.4 through Ingress: the Lighting Designer 2 - template, lights, Truth and Brightness
Matrices (columns of several kinds, cells, 0 % and its meaning), Colour & Mood, Switches & Remotes (room → device →
event, discovered interactions, a manual ZHA event, the action matrix, the override policy), Advanced Rules,
Priority & Resolution (order, who controls a light now), the simulation and the YAML - at desktop (light and dark),
tablet and phone widths, without console errors and without sideways scrolling.

    python browser_05_checks.py <screenshot dir>
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import urllib.request

from playwright.sync_api import sync_playwright

from browser_checks import API, PANEL, SIZES, chromium

BED, CEILING = "light.bed_light", "light.ceiling_lights"
BUTTON = "event.push_button_press"


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
                device = change("devices/create", {"name": f"L2 {tag}", "targets": [BED, CEILING]})["device_id"]
                sofa = change("presence/save", {"source": {"name": f"Sofa {tag}", "kind": "zone", "entity_id": "binary_sensor.sofa_zone"}})["source_id"]
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
                    inner.wait_for_timeout(200)
                    return True

                def overflow(inner=inner) -> bool:
                    return inner.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth + 1")

                def shot(name: str, frame=frame, tag=tag) -> None:
                    frame.locator("body").screenshot(path=str(out / f"{tag}-l2-{name}.png"))

                def tab(name: str, frame=frame, inner=inner) -> None:
                    frame.locator("nav.tabs a", has_text=name).first.click()
                    inner.wait_for_timeout(400)

                def cell(matrix: str, r: str, c: str, frame=frame):
                    return frame.locator(f"button.mcell[data-m='{matrix}'][data-r='{r}'][data-c='{c}']").first

                def fill(selector: str, value: str, frame=frame) -> None:
                    field = frame.locator(selector).first
                    field.fill(value)
                    field.dispatch_event("change")

                # ------------------------------------------------ a template, the lights
                go(f"#/gallery/{device}", "New automation")
                check(f"{tag}: the gallery offers the new lighting templates", frame.locator("[data-template=l2_presence]").count() == 1
                      and frame.locator("[data-template=l2_switch]").count() == 1)
                frame.locator("[data-template=l2_presence] button[data-act=use]").click()
                frame.locator("nav.tabs").wait_for()
                d = designer(device)
                check(f"{tag}: a template adds an empty Truth Matrix and binds nothing", d["version"] == 2 and len(d["truth"]["matrices"]) == 1
                      and d["truth"]["matrices"][0]["columns"] == [] and d["lights"] == [], json.dumps(d)[:300])
                check(f"{tag}: it opens on the Truth Matrix tab", frame.locator("nav.tabs a.active", has_text="Truth Matrix").count() == 1)
                tab("Lights")
                for light in (BED, CEILING):
                    frame.locator(f"input[data-chg=row][data-id='{light}']").check()
                    saved()
                check(f"{tag}: lights are chosen explicitly", designer(device)["lights"] == [BED, CEILING])
                check(f"{tag}: the priority is shown in the overview", frame.locator(".roles .role").count() == 5)
                shot("lights")

                # ------------------------------------------------ Truth Matrix
                tab("Truth Matrix")
                mid = designer(device)["truth"]["matrices"][0]["id"]
                for state in ("occupied", "vacant"):
                    frame.locator(f"#add-{mid}").select_option(f"{sofa}:{state}")
                    frame.locator(f"button[data-act=coladd][data-m='{mid}']").click()
                    saved()
                cols = [c["id"] for c in designer(device)["truth"]["matrices"][0]["columns"]]
                check(f"{tag}: presence columns are added", len(cols) == 2)
                check(f"{tag}: occupied and vacant of one zone never hold together: no policy is asked",
                      frame.locator(f"#pol-{mid}").count() == 0)
                cell(f"truth:{mid}", BED, cols[0]).click()
                frame.locator(".cell-editor label.kind:has(input[value='on'])").click()
                saved()
                frame.locator("button[data-act=mnone]").first.click()
                cell(f"truth:{mid}", CEILING, cols[0]).click()
                cell(f"truth:{mid}", BED, cols[1]).click()
                frame.locator(".cell-editor label.kind:has(input[value='off'])").click()
                saved()
                cells = designer(device)["truth"]["matrices"][0]["cells"]
                check(f"{tag}: Truth cells are ON / OFF (two edited at once)", cells[BED][cols[0]] == {"kind": "on"}
                      and cells[CEILING][cols[0]] == {"kind": "off"} and cells[BED][cols[1]] == {"kind": "off"}, json.dumps(cells))
                check(f"{tag}: a Truth cell offers no percentage", frame.locator(".cell-editor input[data-key=pct]").count() == 0)
                check(f"{tag}: cells are large enough to tap", cell(f"truth:{mid}", BED, cols[0]).bounding_box()["height"] >= 44)
                check(f"{tag}: the Truth tab does not overflow", not overflow())
                shot("truth")

                # ------------------------------------------------ Brightness Matrix with a light level, 0 %
                tab("Brightness Matrix")
                frame.locator("button[data-act=madd][data-layer=brightness]").click()
                saved()
                bid = designer(device)["brightness"]["matrices"][0]["id"]
                frame.locator(f"#add-{bid}").select_option("numeric")
                frame.locator(f"button[data-act=coladd][data-m='{bid}']").click()
                saved()
                frame.locator("button.entity[data-path='brightness/matrices/0/columns/0/entity_id']").click()
                frame.locator("#pick-q").fill("test lux")
                frame.locator("button.result", has_text="sensor.test_lux").first.click()
                saved()
                fill("input[data-path='brightness/matrices/0/columns/0/below']", "100")
                saved()
                bcol = designer(device)["brightness"]["matrices"][0]["columns"][0]
                check(f"{tag}: a light-level column (sensor and range)", bcol["entity_id"] == "sensor.test_lux" and bcol["below"] == 100, json.dumps(bcol))
                cell(f"brightness:{bid}", BED, bcol["id"]).click()
                frame.locator(".cell-editor label.kind:has(input[value='set'])").click()
                fill(".cell-editor input[type=number][data-key=pct]", "0")
                saved()
                check(f"{tag}: 0 % needs its meaning before deployment", "0 %" in frame.locator("#d-problems").inner_text())
                frame.locator("#zero").select_option("unchanged")
                saved()
                check(f"{tag}: with the meaning chosen the problem is gone", frame.locator("#d-problems", has_text="0 %").count() == 0
                      and designer(device)["brightness"]["zero"] == "unchanged")
                check(f"{tag}: the Brightness tab does not overflow", not overflow())
                shot("brightness")

                # ------------------------------------------------ Colour & Mood
                tab("Colour & Mood")
                frame.locator("input[data-chg=flag][data-path='mood/enabled']").check()
                saved()
                frame.locator("button.entity[data-path='mood/entity_id']").click()
                frame.locator("#pick-q").fill("Mood")
                frame.locator("button.result", has_text="input_select.mood").first.click()
                saved()
                frame.locator("input[data-chg=value][value='Hygge']").check()
                saved()
                cell("mood", BED, "Hygge").click()
                frame.locator(".cell-editor label.kind:has(input[value='set'])").click()
                saved()
                fill(".cell-editor input[data-key=color_temp_kelvin]", "2700")
                saved()
                mood = designer(device)["mood"]
                check(f"{tag}: a mood cell sets only what is typed (2700 K, brightness unchanged)",
                      mood["cells"][BED]["Hygge"]["color_temp_kelvin"] == 2700 and mood["cells"][BED]["Hygge"]["brightness_pct"] is None, json.dumps(mood["cells"]))
                check(f"{tag}: the mood cell shows its label", cell("mood", BED, "Hygge").inner_text().startswith("2700K"))
                shot("mood")

                # ------------------------------------------------ Switches & Remotes
                tab("Switches & Remotes")
                frame.locator("button[data-act=frooms]").click()
                frame.locator("select[data-chg=froom]").wait_for()
                frame.locator("select[data-chg=froom]").select_option("*")
                frame.locator(f"button[data-act=fpick][data-entity='{BUTTON}']").first.wait_for()
                frame.locator(f"button[data-act=fpick][data-entity='{BUTTON}']").first.click()
                frame.locator("input[data-chg=fint]").first.wait_for()
                check(f"{tag}: the button's discovered interactions are listed (its event types)",
                      frame.locator("input[data-chg=fint]").count() == 1 and "pressed" in frame.locator("#finder").inner_text())
                frame.locator("input[data-chg=fint]").first.check()
                frame.locator("button[data-act=fadd]").click()
                saved()
                w = designer(device)["switches"]
                check(f"{tag}: a momentary control is added with the chosen interaction", len(w) == 1 and w[0]["kind"] == "momentary"
                      and w[0]["interactions"][0]["trigger"] == {"type": "button", "entity_id": BUTTON, "event_types": ["pressed"]}, json.dumps(w)[:300])
                check(f"{tag}: nothing is assigned to it by itself", w[0]["mappings"] == [])
                interaction = w[0]["interactions"][0]["id"]
                frame.locator(f"select[data-chg=maction][data-i='{interaction}']").select_option("toggle")
                saved()
                frame.locator(f"tr[data-interaction='{interaction}'] input[data-chg=lightset][value='{BED}']").check()
                saved()
                frame.locator(f"tr[data-interaction='{interaction}'] input[data-chg=flag]").check()
                saved()
                check(f"{tag}: a press is mapped to toggle the bed light with an override", designer(device)["switches"][0]["mappings"][0]
                      == {"id": designer(device)["switches"][0]["mappings"][0]["id"], "interaction": interaction, "enabled": True, "lights": [BED],
                          "action": {"kind": "toggle"}, "override": True})
                check(f"{tag}: the override needs its policy", "how long" in frame.locator("#d-problems").inner_text())
                frame.locator("#opolicy").select_option("until_resumed")
                saved()
                check(f"{tag}: with a policy nothing is left to resolve", frame.locator("#d-problems").count() == 0,
                      frame.locator("#d-problems").inner_text() if frame.locator("#d-problems").count() else "")
                check(f"{tag}: the helpers to be created are listed", "created when you deploy" in frame.locator("#override-policy").inner_text())
                frame.locator("button[data-act=fmanual]").click()
                fill("input[data-chg=fman][data-key=ieee]", "00:11:22")
                fill("input[data-chg=fman][data-key=command]", "on")
                frame.locator("button[data-act=fmanadd]").click()
                saved()
                manual = [s for s in designer(device)["switches"] if s["name"].startswith("Remote")]
                check(f"{tag}: a ZHA event is mapped by hand", manual and manual[0]["interactions"][0]["trigger"]["event_data"] ==
                      {"device_ieee": "00:11:22", "command": "on"}, json.dumps(manual)[:300])
                mi = manual[0]["interactions"][0]["id"]
                frame.locator(f"select[data-chg=maction][data-i='{mi}']").select_option("none")
                saved()
                check(f"{tag}: NO ACTION is explicit and different from UNSET", [m["action"] for s in designer(device)["switches"]
                      for m in s["mappings"] if m["interaction"] == mi] == [{"kind": "none"}])
                frame.locator(f"select[data-chg=maction][data-i='{mi}']").select_option("select_mood")
                inner.wait_for_timeout(500)
                frame.locator(f"tr[data-interaction='{mi}'] select[aria-label=Mood]").select_option("Hygge")
                saved()
                check(f"{tag}: a press can select a mood", [m["action"] for s in designer(device)["switches"] for m in s["mappings"]
                      if m["interaction"] == mi] == [{"kind": "select_mood", "value": "Hygge"}])
                check(f"{tag}: the Switches tab does not overflow", not overflow())
                shot("switches")

                # ------------------------------------------------ Advanced Rules
                tab("Advanced Rules")
                frame.locator("button[data-act=radd][data-kind=while]").click()
                saved()
                fill("input[data-path='advanced/0/name']", "Guests")
                saved()
                frame.locator("input[data-chg=lightset][data-path='advanced/0/lights'][value='" + BED + "']").check()
                saved()
                frame.locator("#cnew-0").select_option("state")
                frame.locator("button[data-act=cadd][data-r='0']").click()
                frame.locator("#pick-q").fill("guest")
                frame.locator("button.result", has_text="input_boolean.guest_mode").first.click()
                fill("input[data-path='advanced/0/conditions/items/0/state']", "on")
                saved()
                frame.locator("select[data-chg=truthset][data-r='0']").select_option("off")
                saved()
                rule = designer(device)["advanced"][0]
                check(f"{tag}: a “while” rule: guests → bed light OFF", rule["name"] == "Guests" and rule["lights"] == [BED]
                      and rule["conditions"]["items"][0]["entity_id"] == "input_boolean.guest_mode" and rule["set"]["truth"] == "off", json.dumps(rule)[:300])
                check(f"{tag}: the generic rule editor is reachable", frame.locator(f"a[href='#/rules/{device}/new']").count() == 1)
                check(f"{tag}: the Advanced tab does not overflow", not overflow())
                shot("advanced")

                # ------------------------------------------------ Priority & Resolution
                tab("Priority & Resolution")
                frame.locator("#priority li").first.wait_for()
                check(f"{tag}: the default priority is listed, highest first", [li.get_attribute("data-layer") for li in frame.locator("#priority li").all()]
                      == ["advanced", "override", "mood", "brightness", "truth"])
                frame.locator("#priority li[data-layer=brightness] button[data-dir='-1']").click()
                saved()
                check(f"{tag}: the order is changed without YAML", designer(device)["priority"] == ["advanced", "override", "brightness", "mood", "truth"])
                check(f"{tag}: who controls each light now is shown", frame.locator(f"#control tr[data-light='{BED}']").count() == 1)
                check(f"{tag}: the Priority tab does not overflow", not overflow())
                shot("priority")

                # ------------------------------------------------ simulation and YAML
                tab("Simulate")
                frame.locator("#sim-inputs").wait_for()
                frame.locator("#sim-event").select_option(interaction)
                frame.locator("button[data-act=simrun]").click()
                frame.locator("#sim-result").wait_for()
                text = frame.locator("#sim-result").inner_text()
                check(f"{tag}: a simulated press starts the override and is shown as an event", "manual override starts" in text
                      and "an event, not a state" in text and "override active" in text, text[:400])
                shot("simulate")
                tab("YAML")
                frame.locator("#d-yaml").wait_for()
                yaml_text = frame.locator("#d-yaml").inner_text()
                check(f"{tag}: the YAML shows an automation per light", "automatic control" in yaml_text and "ab_cmd" in yaml_text)
                check(f"{tag}: the YAML page does not overflow", not overflow())
                check(f"{tag}: no console errors", not errors, "; ".join(errors[:3]))
                page.close()
                change("devices/delete", {"device_id": device, "overwrite": True})
                change("presence/delete", {"source_id": sofa})
        browser.close()
    passed = sum(1 for _n, ok, _d in results if ok)
    print(f"\n{passed} of {len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1] if len(sys.argv) > 1 else "shots")))
