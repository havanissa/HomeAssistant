"""Browser checks of the Phase 0.2 screens through Ingress (Playwright, headless Chromium).

The import list and analysis, the blueprint export, the rule diagnostics, an advanced (nested) rule in the editor
and the dropdown-helper shortcut, each at desktop (light and dark), tablet (both orientations) and phone widths:
no console errors, no horizontal overflow, screenshots. On the desktop a rule with a button trigger, a device
trigger and an if/else is built in the editor, a blueprint is configured and installed, mood rules are created
and an automation is imported - all in the interface. The phone deletes what the desktop created.

    python browser_02_checks.py <screenshot dir>

Run after ``e2e_02_checks.py`` (it leaves the devices Decor, Fan, Kitchen, Ceiling, Flaky and Office light).
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import urllib.request

from playwright.sync_api import sync_playwright

from browser_checks import API, PANEL, SIZES, chromium, overview

ENTRANCE = "light.entrance_color_white_lights"
IMPORT_ME = "Decor at the door"


def get(path: str) -> dict:
    with urllib.request.urlopen(f"{API}/{path}", timeout=30) as response:
        return json.loads(response.read())


def main(out: Path) -> int:  # noqa: PLR0915 - one linear scenario
    out.mkdir(parents=True, exist_ok=True)
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, bool(ok), detail))
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  -> {detail}"), flush=True)

    devices = {d["name"]: d for d in overview()["devices"]}
    fan, kitchen, office = devices["Fan"], devices["Kitchen"], devices["Office light"]
    door_rule = next(r["id"] for r in get(f"devices/{kitchen['id']}")["device"]["rules"] if r["name"] == "Door")
    automations = {a["name"]: a for a in get("automations")["automations"]}
    unsupported = automations["Everything off in the kitchen"]["entity_id"]
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium())
        for label, (width, height) in SIZES.items():
            for scheme in (("light", "dark") if label == "desktop" else ("light",)):
                page = browser.new_page(viewport={"width": width, "height": height}, color_scheme=scheme,
                                        has_touch=label == "phone")
                errors: list[str] = []
                page.on("console", lambda m, e=errors: e.append(m.text) if m.type == "error" else None)
                page.on("pageerror", lambda err, e=errors: e.append(str(err)))
                page.goto(PANEL)
                frame = page.frame_locator("#ingress")
                frame.locator("h1").first.wait_for()
                tag = f"{label}-{scheme}"
                inner = page.frames[1]

                def overflow(inner=inner) -> bool:
                    return inner.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth + 1")

                def shot(name: str, frame=frame, tag=tag) -> None:
                    frame.locator("body").screenshot(path=str(out / f"{tag}-{name}.png"))

                def go(route: str, heading: str, inner=inner, frame=frame) -> None:
                    inner.evaluate(f"location.hash = {route!r}")
                    frame.locator("h1", has_text=heading).first.wait_for()
                    inner.wait_for_timeout(400)

                # ------------------------------------------------ import: list and an automation that cannot be converted
                frame.locator('nav a[data-nav="import"]').click()
                frame.locator("#import-list").wait_for()
                inner.wait_for_timeout(300)
                listing = frame.locator("#import-list").text_content() or ""
                check(f"{tag}: the import list shows the unmanaged automations and no managed one",
                      "Everything off in the kitchen" in listing and "Hand-made automation" in listing and "automation_builder" not in listing, listing[:200])
                check(f"{tag}: an automation that was copied says so", "copied to" in listing)
                check(f"{tag}: import list has no horizontal overflow", not overflow())
                shot("import-list")
                go(f"#/import/{unsupported}", "Everything off in the kitchen")
                result = frame.locator("#import-result").text_content() or ""
                check(f"{tag}: unsupported parts are named and no conversion is offered",
                      "Cannot be converted" in result and "never a whole area" in result and "stop" in result
                      and frame.locator("#import-convert").count() == 0, result[:300])
                check(f"{tag}: the original's YAML is shown", "area_id: kitchen" in (frame.locator("#original-yaml").text_content() or ""))
                check(f"{tag}: import analysis has no horizontal overflow", not overflow())
                shot("import-unsupported")

                # ------------------------------------------------ blueprint export screen
                go(f"#/blueprint/{office['id']}", "Export as blueprint")
                frame.locator("#bp-yaml").wait_for()
                text = frame.locator("#bp-yaml").text_content() or ""
                check(f"{tag}: the blueprint screen shows name, inputs, validation and YAML",
                      frame.locator("#bp-name").input_value() == "Motion light" and frame.locator(".bp-input.on").count() == 6
                      and "!input" in text and frame.locator('#bp-validation [data-level="ok"]').count() >= 2
                      and frame.locator('#bp-validation [data-level="error"]').count() == 0)
                check(f"{tag}: it says where the blueprint is installed", "blueprints/automation/automation_builder/" in (frame.locator("#bp-side").text_content() or ""))
                check(f"{tag}: blueprint screen has no horizontal overflow", not overflow())
                shot("blueprint")

                # ------------------------------------------------ rule diagnostics on the device page
                go(f"#/devices/{fan['id']}", "Fan")
                frame.locator(".diag").first.wait_for()
                frame.locator(".diag details summary").first.click()
                inner.wait_for_timeout(200)
                diag = frame.locator(".diag").first.text_content() or ""
                check(f"{tag}: per rule: triggered, executed, blocked by which condition, outcome, entities",
                      "Last triggered" in diag and "Last executed" in diag and "Last blocked by conditions" in diag
                      and "Last outcome" in diag and "Test motion" in diag and frame.locator('.diag .status[data-status="in_sync"]').count() >= 1, diag[:300])
                check(f"{tag}: the trace in Home Assistant is linked", frame.locator('.diag a[href*="/config/automation/trace/automation_builder_"]').count() >= 1)
                check(f"{tag}: the separated rule says why it has its own automation", "its own automation because it waits" in (frame.locator("#checks").text_content() or ""))
                check(f"{tag}: device page with diagnostics has no horizontal overflow", not overflow())
                shot("diagnostics")

                # ------------------------------------------------ an advanced rule in the editor
                go(f"#/rules/{kitchen['id']}/{door_rule}", "Edit rule")
                frame.locator("#summary").wait_for()
                inner.wait_for_timeout(300)
                check(f"{tag}: branches and waits are shown nested", frame.locator(".part.nested").count() == 2 and frame.locator(".nest").count() >= 4)
                check(f"{tag}: the advanced rule is not re-saved by opening it", "Saved." in (frame.locator("#save-status").text_content() or ""))
                frame.locator("details[data-keep=advanced] summary").click()
                inner.wait_for_timeout(300)
                check(f"{tag}: Advanced offers the retrigger behaviour for a rule that waits", frame.locator("#retrigger").is_visible())
                check(f"{tag}: nested editor has no horizontal overflow", not overflow())
                shot("editor-advanced")
                go(f"#/mood/{fan['id']}", "Rules from a dropdown helper")
                check(f"{tag}: the dropdown-helper shortcut fits", not overflow())

                if label == "desktop" and scheme == "light":
                    # -------------------------------------------- build an advanced rule in the editor
                    go("#/devices/new", "Add a device")
                    frame.locator('button.choice[data-act="room"]', has_text="Hall").click()
                    frame.locator('button.choice[data-act="device"]', has_text="Entrance").click()
                    frame.locator(f'input[data-id="{ENTRANCE}"]').check()
                    frame.locator("#create").click()
                    # 0.3: a device with lights or covers opens the template gallery; "Write a rule" is the rule editor
                    frame.locator("h1", has_text="New automation").wait_for()
                    frame.locator("a.button", has_text="Write a rule").click()
                    frame.locator("h1", has_text="New rule").wait_for()
                    check("flow: a new rule starts with the simple choices (no nested blocks)", frame.locator(".nest").count() == 0)
                    frame.locator("#rule-name").fill("Remote")
                    frame.locator('button[data-act="add-trigger"]').click()
                    frame.locator('select[data-f="trigger-type"]').select_option("button")
                    frame.locator("button.entity.unset").first.click()
                    frame.locator("#pick-room").uncheck()
                    wanted = frame.locator("button.result", has_text="event.push_button_press")
                    wanted.wait_for()
                    offered = frame.locator("button.result .mono").all_text_contents()
                    check("flow: the button picker offers event entities only", offered and all(e.startswith("event.") for e in offered), str(offered[:5]))
                    wanted.click()
                    press = frame.locator('input[data-f="list-item"][value="pressed"]')
                    press.wait_for()
                    check("flow: the presses the button reports are offered, none invented", frame.locator('input[data-f="list-item"]').count() == 1)
                    press.check()
                    frame.locator('button[data-act="add-trigger"]').click()
                    frame.locator('select[data-f="trigger-type"]').nth(1).select_option("device")
                    frame.locator('button[data-act="pick-device"]').click()
                    frame.locator("#pick-q").fill("Bed Light")
                    frame.locator("button.result", has_text="Bed Light").first.click()
                    offer = frame.locator('select[data-f="device-offer"]')
                    offer.wait_for()
                    labels = offer.locator("option").all_text_contents()
                    check("flow: device triggers are the ones Home Assistant lists", any("turned on" in o for o in labels), str(labels))
                    offer.select_option(label=next(o for o in labels if "turned on" in o))
                    frame.locator('.block.then button[data-act="add-action"]').click()
                    frame.locator('select[data-f="action-kind"]').select_option("if")
                    frame.locator(".part.nested").wait_for()
                    frame.locator(".part.nested .nest button.entity.unset").first.click()
                    frame.locator("#pick-q").fill("guest")
                    frame.locator("button.result", has_text="input_boolean.guest_mode").click()
                    frame.locator('input[data-path="actions.0.conditions.items.0.state"]').fill("off")
                    frame.locator('button[data-act="add-action"][data-path="actions.0.then"]').click()
                    frame.locator('input[data-path="actions.0.then.0.data.brightness_pct"]').fill("35")
                    frame.locator('button[data-act="add-action"][data-path="actions.0.else"]').click()
                    frame.locator('select[data-f="action-kind"][data-path="actions.0.else"]').select_option("light.turn_off")
                    frame.locator("#summary", has_text="otherwise").wait_for(timeout=8000)
                    frame.locator("#save-status.ok").wait_for(timeout=8000)
                    summary = frame.locator("#summary").text_content() or ""
                    check("flow: button, device trigger and if/else read as one rule",
                          "Push Button press reports “pressed”" in summary and "turned on" in summary
                          and "if Guest mode is “off”" in summary and "brightness 35 %" in summary and "otherwise: turn" in summary, summary)
                    frame.locator("#side details summary").click()
                    yaml = frame.locator("#yaml").text_content() or ""
                    check("flow: the YAML is native (device trigger, if/then/else), nothing hand-written was needed",
                          "trigger: device" in yaml and "not_from:" in yaml and "else:" in yaml, yaml[:400])
                    shot("advanced-rule")
                    entrance = next(d for d in overview()["devices"] if d["targets"][0]["entity_id"] == ENTRANCE)

                    # -------------------------------------------- export it as a blueprint and install it
                    go(f"#/devices/{entrance['id']}", entrance["name"])
                    frame.locator("#export-blueprint").click()
                    frame.locator("h1", has_text="Export as blueprint").wait_for()
                    check("flow: nothing is an input until it is ticked", frame.locator(".bp-input.on").count() == 0
                          and "No input is selected" in (frame.locator("#bp-validation").text_content() or ""))
                    frame.locator("#bp-name").fill("Remote light")
                    frame.locator('.bp-input[data-input^="controlled_"] input[type=checkbox]').check()
                    frame.locator(".bp-input.on").first.wait_for()
                    frame.locator('.bp-input[data-input^="event_"] input[type=checkbox]').check()
                    frame.locator("#bp-yaml", has_text="!input 'event_push_button_press'").wait_for(timeout=8000)
                    frame.locator('.bp-input.on input[data-field="name"]').first.fill("The light")
                    frame.locator("#bp-yaml", has_text="name: The light").wait_for(timeout=8000)
                    text = frame.locator("#bp-yaml").text_content() or ""
                    check("flow: ticked values become inputs with the chosen name", "name: Remote light" in text
                          and "!input 'controlled_entrance_color_white_lights'" in text and "name: The light" in text, text[:300])
                    check("flow: what stays fixed is said (the device trigger)", "bound to the device" in (frame.locator("#bp-validation").text_content() or ""))
                    href = frame.locator("#bp-download").get_attribute("href") or ""
                    check("flow: the blueprint can be downloaded", href.endswith(f"/blueprint/{entrance['id']}/download") or "download" in href, href)
                    shot("blueprint-configured")
                    frame.locator("#bp-install").click()
                    frame.locator("#bp-side", has_text="Installed today").wait_for(timeout=15000)
                    check("flow: installed in Home Assistant from the interface", True)
                    reloaded = get(f"blueprint/{entrance['id']}")
                    check("flow: the choices were saved as they were made", reloaded["settings"]["name"] == "Remote light"
                          and len(reloaded["settings"]["inputs"]) == 2 and reloaded["installed"]["current"] is True, str(reloaded["settings"]))

                    # -------------------------------------------- rules from a dropdown helper
                    go(f"#/mood/{entrance['id']}", "Rules from a dropdown helper")
                    frame.locator('button[data-act="pick"]').click()
                    frame.locator("button.result", has_text="input_select.mood").click()
                    frame.locator("#mood-table").wait_for()
                    check("flow: the helper's own values are listed, whatever they are called", frame.locator("#mood-table tbody tr").count() == 3
                          and "Hygge" in (frame.locator("#mood-table").text_content() or ""))
                    frame.locator('select[data-option="Hygge"]').select_option("light.turn_on")
                    frame.locator('input[data-option="Hygge"][data-key="brightness_pct"]').fill("30")
                    frame.locator('input[data-option="Hygge"][data-key="color_temp_kelvin"]').fill("2400")
                    frame.locator('select[data-option="Nátt"]').select_option("light.turn_off")
                    shot("mood")
                    frame.locator('button[data-act="create"]').click()
                    frame.locator("h2", has_text="Rules").wait_for()
                    rules = get(f"devices/{entrance['id']}")
                    texts = [rules["summaries"][r["id"]]["text"] for r in rules["device"]["rules"]]
                    check("flow: two ordinary rules were created for this device only", len(texts) == 3
                          and any("Mood changes to “Hygge”" in t and "brightness 30 %" in t and "colour temperature 2400 K" in t for t in texts)
                          and any("Mood changes to “Nátt” THEN turn" in t for t in texts), str(texts))

                    # -------------------------------------------- import an automation in the interface
                    go("#/import", "Import an automation")
                    frame.locator("#import-list tr", has_text=IMPORT_ME).locator("a.button").click()
                    frame.locator("#import-convert").wait_for()
                    result = frame.locator("#import-result").text_content() or ""
                    check("flow: the analysis shows the rule the copy would be", "Can be converted faithfully" in result
                          and "Test door changes to “on”" in result and "Guest mode is “off”" in result and "Decorative Lights" in result, result[:300])
                    check("flow: an existing device with the same entity is offered, a new device is the default",
                          frame.locator("#destination").input_value() == "new" and frame.locator("#destination option").count() >= 2)
                    shot("import-convertible")
                    frame.locator('button[data-act="convert"]').click()
                    frame.locator("h2", has_text="Rules").wait_for()
                    inner.wait_for_timeout(400)
                    checks = frame.locator("#checks").text_content() or ""
                    check("flow: the copy is a device of its own, marked as imported, with the warning about its original",
                          frame.locator(".rule .badge", has_text="imported").count() == 1 and "is still turned on in Home Assistant" in checks, checks[:300])
                    frame.locator('button[data-act="plan"]').click()
                    frame.locator("#plan").wait_for()
                    check("flow: deploying is blocked while the original runs, and both ways out are offered",
                          frame.locator("#deploy").is_disabled() and frame.locator('[data-duplicate] button[data-act="allow-both"]').count() == 1
                          and "both would run" in (frame.locator("#plan-blocked").text_content() or ""))
                    shot("import-blocked")

                if label == "phone":
                    # -------------------------------------------- the phone removes what the desktop made
                    for name in ("Entrance Color + White Lights", IMPORT_ME):
                        device = next((d for d in overview()["devices"] if d["name"] == name), None)
                        check(f"phone flow: “{name}” from the desktop is there", device is not None)
                        if device is None:
                            continue
                        go(f"#/devices/{device['id']}", name)
                        check(f"phone flow: “{name}” fits the phone", not overflow())
                        if name != IMPORT_ME:
                            frame.locator(".rule a.button", has_text="Edit").first.click()
                            frame.locator("h1", has_text="Edit rule").wait_for()
                            frame.locator("#summary").wait_for()
                            inner.wait_for_timeout(300)
                            check("phone flow: the nested rule built on the desktop fits the phone", not overflow() and frame.locator(".part.nested").count() == 1)
                            shot("advanced-rule")
                            go(f"#/blueprint/{device['id']}", "Export as blueprint")
                            check("phone flow: its blueprint screen fits the phone", not overflow() and frame.locator(".bp-input.on").count() == 2)
                            shot("blueprint-configured")
                            go(f"#/devices/{device['id']}", name)
                        frame.locator('button[data-act="device-delete"]').click()
                        frame.locator('#dialog button[data-d="ok"]').click()
                        frame.locator("h1", has_text="Devices").wait_for()
                    left = [d["name"] for d in overview()["devices"]]
                    check("phone flow: both devices are gone again", "Entrance Color + White Lights" not in left and IMPORT_ME not in left, str(left))
                check(f"{tag}: no console errors", not errors, "; ".join(errors[:3]))
                page.close()
        browser.close()
    originals = {a["name"]: a for a in get("automations")["automations"]}
    check("the imported automation's original is still there, turned on and unmanaged", originals[IMPORT_ME]["state"] == "on" and originals[IMPORT_ME]["imported"] is None,
          str(originals[IMPORT_ME]))
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)} of {len(results)} browser checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
