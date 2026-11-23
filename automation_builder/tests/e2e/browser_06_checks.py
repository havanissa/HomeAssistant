"""Browser checks of App 0.5 through Ingress, at desktop (light and dark), tablet (landscape and portrait) and phone:

* Switches & Remotes: "+ Add Interaction" twice for the same switch and the same light (single press ON, double press
  OFF), both saved; reorder, disable and delete one interaction without touching the other;
* Light Level: add a room, choose its template, override a limit (shown as "Room override"), reset it (back to
  "Global"); the templates, default matrices, sources and diagnostics pages;
* Heating: a template and a room profile with a room value; the Heating Designer on a thermostat (rows, profile,
  the Day / night template, the preview);

without console errors, failed requests or sideways scrolling.

    python browser_06_checks.py <screenshot dir>
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import urllib.request

from playwright.sync_api import sync_playwright

from browser_checks import API, PANEL, SIZES, chromium

PUMP = "climate.heatpump"
LIGHT_OF = {"desktop-light": "light.bed_light", "desktop-dark": "light.ceiling_lights", "tablet-light": "light.kitchen_lights",
            "tablet-portrait-light": "light.office_rgbw_lights", "phone-light": "light.living_room_rgbww_lights"}
AREA_OF = {"desktop-light": "bedroom", "desktop-dark": "living_room", "tablet-light": "kitchen", "tablet-portrait-light": "office",
           "phone-light": "hall"}


def api(path: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(f"{API}/{path}", data=json.dumps(body).encode() if body is not None else None,
                                     headers={"X-AB": "1", "Content-Type": "application/json"}, method="POST" if body is not None else "GET")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def change(path: str, body: dict) -> dict:
    return api(path, {**body, "expected_revision": api("overview")["revision"]})


def main(out: Path) -> int:  # noqa: PLR0915 - one linear scenario
    out.mkdir(parents=True, exist_ok=True)
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, bool(ok), detail))
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  -> {detail}"), flush=True)

    # a repeated run starts from a clean slate: its own devices, room light levels and room profiles go first
    for dev in api("overview")["devices"]:
        if dev["name"].startswith(("Switch ", "Pump ")):
            change("devices/delete", {"device_id": dev["id"], "overwrite": True})
    for room in api("light-levels")["rooms"]:
        if room["area_id"] in AREA_OF.values():
            change("light-levels/room/delete", {"area_id": room["area_id"], "overwrite": True})
    for room in api("heating")["rooms"]:
        if room["area_id"] in AREA_OF.values() and not room["used_by"]:
            change("heating/room/delete", {"area_id": room["area_id"]})
    if not any(t["name"] == "Standard light levels" for t in api("light-levels")["templates"]):
        change("light-levels/template/preset", {})
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium())
        for label, (width, height) in SIZES.items():
            for scheme in (("light", "dark") if label == "desktop" else ("light",)):
                tag = f"{label}-{scheme}"
                light, area = LIGHT_OF[tag], AREA_OF[tag]
                page = browser.new_page(viewport={"width": width, "height": height}, color_scheme=scheme, has_touch=label == "phone")
                errors: list[str] = []
                page.on("console", lambda m, e=errors: e.append(m.text) if m.type == "error" else None)
                page.on("pageerror", lambda err, e=errors: e.append(str(err)))
                page.on("response", lambda r, e=errors: e.append(f"HTTP {r.status} {r.url.split('/api/')[-1]}") if r.status >= 400 else None)
                page.goto(PANEL)
                frame = page.frame_locator("#ingress")
                frame.locator("h1").first.wait_for()
                inner = page.frames[1]

                def go(route: str, inner=inner) -> None:
                    inner.evaluate(f"location.hash = {route!r}")
                    inner.wait_for_timeout(700)

                def saved(which: str, frame=frame, inner=inner) -> bool:
                    try:
                        frame.locator(f"#{which}.ok").first.wait_for(timeout=8000)
                    except Exception:  # noqa: BLE001
                        return False
                    inner.wait_for_timeout(250)
                    return True

                def overflow(inner=inner) -> bool:
                    return inner.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth + 1")

                def shot(name: str, frame=frame, tag=tag) -> None:
                    frame.locator("#view").screenshot(path=str(out / f"{tag}-06-{name}.png"))

                def fill(selector: str, value: str, frame=frame) -> None:
                    field = frame.locator(selector).first
                    field.fill(value)
                    field.dispatch_event("change")

                if scheme == "dark":
                    bg = inner.evaluate("getComputedStyle(document.body).backgroundColor")
                    check(f"{tag}: dark theme", bg not in ("rgb(244, 246, 248)", "rgba(0, 0, 0, 0)"), bg)

                # ------------------------------------------------ switches: two interactions, one switch, one light
                device = change("devices/create", {"name": f"Switch {tag}", "targets": [light]})["device_id"]
                designer_id = change("designers/create", {"device_id": device, "kind": "lighting"})["designer_id"]
                d = api(f"designers/{device}/{designer_id}")["designer"]["config"]
                ev = lambda c: {"type": "event", "event_type": "zha_event", "event_data": {"device_ieee": f"b6:{tag}", "command": c}}  # noqa: E731
                d.update(lights=[light], switches=[{"id": "w_0b06", "name": "Wall switch", "kind": "momentary", "entity_id": None, "ha_device_id": None,
                                                    "manual": True, "mappings": [],
                                                    "interactions": [{"id": "i_0b01", "kind": "single", "label": "Single press", "trigger": ev("single")},
                                                                     {"id": "i_0b02", "kind": "double", "label": "Double press", "trigger": ev("double")}]}])
                change("designers/save", {"device_id": device, "designer": d})
                go(f"#/design/{device}/{designer_id}/switches")
                for event, action in (("i_0b01", "on"), ("i_0b02", "off")):
                    frame.locator("[data-act=iadd][data-w=w_0b06]").click()
                    frame.locator("select[data-chg=xnew][data-key=interaction]").select_option(event)
                    frame.locator("select[data-chg=xnew][data-key=kind]").select_option(action)
                    frame.locator("[data-act=xcreate]").click()
                    check(f"{tag}: + Add Interaction ({action}) is saved", saved("d-status"))
                    row = frame.locator("li.mapping-row").last
                    if row.locator(f"input[data-chg=lightset][value='{light}']").count() and not row.locator(f"input[data-chg=lightset][value='{light}']").is_checked():
                        row.locator(f"input[data-chg=lightset][value='{light}']").check()
                        saved("d-status")
                maps = api(f"designers/{device}/{designer_id}")["designer"]["config"]["switches"][0]["mappings"]
                check(f"{tag}: the same switch turns the same light ON and OFF", [(m["interaction"], m["action"]["kind"], m["lights"]) for m in maps]
                      == [("i_0b01", "on", [light]), ("i_0b02", "off", [light])], str(maps))
                check(f"{tag}: no conflict blocks it", api(f"designers/{device}/{designer_id}")["blocking"] == 0)
                shot("switches")
                frame.locator("li.mapping-row").first.locator("[data-act=xmove][data-dir='1']").click()
                saved("d-status")
                maps = api(f"designers/{device}/{designer_id}")["designer"]["config"]["switches"][0]["mappings"]
                check(f"{tag}: reordering moves the line", [m["action"]["kind"] for m in maps] == ["off", "on"], str(maps))
                frame.locator("li.mapping-row").first.locator("input[data-chg=flag]").first.uncheck()
                saved("d-status")
                frame.locator("li.mapping-row").last.locator("[data-act=xdrop]").click()
                saved("d-status")
                maps = api(f"designers/{device}/{designer_id}")["designer"]["config"]["switches"][0]["mappings"]
                check(f"{tag}: disabling and deleting one interaction leaves the other", [(m["action"]["kind"], m["enabled"]) for m in maps] == [("off", False)], str(maps))
                check(f"{tag}: switches - no sideways scrolling", not overflow())

                # ------------------------------------------------ light level: a room, its template, an override
                go("#/light-level")
                frame.locator("#l-new-room").select_option(area)
                frame.locator("[data-act=room-add]").click()
                frame.locator("#rtpl").wait_for()
                inner.wait_for_timeout(500)
                check(f"{tag}: a room is added with the standard template", frame.locator("#rtpl").input_value() != "")
                fill("input[data-chg=rband][data-band=dark]", "95")
                check(f"{tag}: a room limit is saved", saved("l-status"))
                row = frame.locator("tr[data-band=dark]")
                check(f"{tag}: it is shown as a room override", "95 lx" in row.inner_text() and row.locator(".badge.src-room").count() == 1, row.inner_text())
                room = next(r for r in api("light-levels")["rooms"] if r["area_id"] == area)
                check(f"{tag}: the override is stored for this room only", room["overrides"]["bands"] == {"dark": {"below": 95}}
                      and all(r["overrides"]["bands"].get("dark") != {"below": 95} for r in api("light-levels")["rooms"] if r["area_id"] != area), str(room["overrides"]))
                shot("room")
                frame.locator("[data-act=rband-reset][data-band=dark]").click()
                saved("l-status")
                check(f"{tag}: reset returns to the global value", frame.locator("tr[data-band=dark] .badge.src-global").count() == 1)
                check(f"{tag}: room editor - no sideways scrolling", not overflow())
                for sub in ("templates", "matrices", "sources", "diagnostics", "rooms"):
                    go(f"#/light-level/{sub}")
                    check(f"{tag}: Light Level {sub} - renders without sideways scrolling", frame.locator("nav.tabs a.active").count() == 1 and not overflow())
                shot("rooms")

                # ------------------------------------------------ heating: profile and designer
                go("#/heating")
                if not api("heating")["templates"]:
                    frame.locator("[data-act=tpreset]").click()
                    saved("h-status")
                frame.locator("#h-new-room").select_option(area)
                frame.locator("#h-new-tpl").select_option(index=1)
                frame.locator("[data-act=radd]").click()
                saved("h-status")
                frame.locator(f"select[data-chg=rcell][data-area={area}][data-m=comfort]").select_option("set")
                saved("h-status")
                fill(f"input[data-chg=rtemp][data-area={area}][data-m=comfort]", "22.5")
                check(f"{tag}: a room profile value is saved", saved("h-status"))
                cells = next(r for r in api("heating")["rooms"] if r["area_id"] == area)["cells"]
                check(f"{tag}: room value and global value are told apart", (cells["comfort"]["source"], cells["comfort"]["cell"]["temp"], cells["setback"]["source"])
                      == ("room", 22.5, "global"), str(cells))
                shot("heating")
                check(f"{tag}: heating page - no sideways scrolling", not overflow())
                pump = change("devices/create", {"name": f"Pump {tag}", "targets": [PUMP]})["device_id"]
                hid = change("designers/create", {"device_id": pump, "kind": "heating"})["designer_id"]
                go(f"#/design/{pump}/{hid}/thermostats")
                frame.locator(f"input[data-chg=row][data-id='{PUMP}']").check()
                saved("d-status")
                frame.locator("#hroom").select_option(area)
                saved("d-status")
                frame.locator("[data-act=quick][data-kind=day_night]").click()
                check(f"{tag}: the Day / night template is applied", saved("d-status"))
                cfg = api(f"designers/{pump}/{hid}")
                check(f"{tag}: the heating designer is complete", cfg["blocking"] == 0 and cfg["designer"]["config"]["modes"]["source"] == "schedule",
                      str([p["message"] for p in cfg["designer"]["problems"] if p["level"] == "blocking"]))
                check(f"{tag}: the thermostats table shows the inherited values", frame.locator(".badge.src-room").count() >= 1)
                shot("heating-designer")
                frame.locator("nav.tabs a", has_text="Schedule").first.click()
                inner.wait_for_timeout(500)
                check(f"{tag}: the schedule lists two entries", frame.locator("li.slot-row").count() == 2)
                frame.locator("nav.tabs a", has_text="Preview").first.click()
                frame.locator(".heat-now").first.wait_for(timeout=8000)
                check(f"{tag}: the preview says what decides now", frame.locator(".heat-now").first.inner_text().strip() != "")
                shot("heating-preview")
                check(f"{tag}: heating designer - no sideways scrolling", not overflow())
                check(f"{tag}: no console errors or failed requests", not errors, "; ".join(errors[:5]))
                page.close()
        browser.close()
    passed = sum(1 for _n, ok, _d in results if ok)
    print(f"\n{passed} of {len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1])))
