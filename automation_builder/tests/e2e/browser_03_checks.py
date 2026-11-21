"""Browser checks of the room-first screens and the Default Rule (0.2.1) through Ingress.

The Overview's room groups and the three-step "Add a device" at desktop (light and dark), tablet and phone
widths; on the desktop a device is added from a room group, entities of two devices are combined by going back,
a rule is built with the room filter on and off, and a default is configured, deployed and removed.

    python browser_03_checks.py <screenshot dir>

Run after ``e2e_03_checks.py`` (any earlier state works as long as some devices exist).
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import urllib.request

from playwright.sync_api import sync_playwright

from browser_checks import API, PANEL, SIZES, chromium, overview

RGBWW = "light.living_room_rgbww_lights"
FAN = "fan.living_room_fan"


def get(path: str) -> dict:
    with urllib.request.urlopen(f"{API}/{path}", timeout=30) as response:
        return json.loads(response.read())


def main(out: Path) -> int:  # noqa: PLR0915 - one linear scenario
    out.mkdir(parents=True, exist_ok=True)
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, bool(ok), detail))
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  -> {detail}"), flush=True)

    before = overview()
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium())
        for label, (width, height) in SIZES.items():
            for scheme in (("light", "dark") if label == "desktop" else ("light",)):
                page = browser.new_page(viewport={"width": width, "height": height}, color_scheme=scheme, has_touch=label == "phone")
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

                # ------------------------------------------------ Overview by room
                go("#/", "Overview")
                frame.locator("details.room").first.wait_for()
                groups = frame.locator("details.room")
                names = frame.locator("details.room .room-name").all_text_contents()
                now = overview()
                listed = sum(len(g["device_ids"]) for g in now["rooms"])
                check(f"{tag}: the Overview groups the devices by room, each device once",
                      groups.count() == len(now["rooms"]) and frame.locator("details.room .card.device").count() == listed == len(now["devices"]), str(names))
                first = groups.first
                summary = first.locator("summary").text_content() or ""
                check(f"{tag}: a room shows its devices, rules and an Add device button", "device" in summary and "rule" in summary
                      and first.locator("a[data-add-room]").count() == 1 and first.locator(".card.device .status").count() >= 1, summary)
                first.locator("summary .room-name").click()
                check(f"{tag}: a room group can be collapsed", not first.locator(".grid").is_visible())
                first.locator("summary .room-name").click()
                check(f"{tag}: Overview has no horizontal overflow", not overflow())
                target = first.locator("summary").bounding_box()
                check(f"{tag}: the group header is a comfortable target", target is not None and target["height"] >= 44, str(target))
                shot("overview-rooms")

                # ------------------------------------------------ the three steps
                go("#/devices/new", "Add a device")
                frame.locator("#rooms-list").wait_for()
                rooms = frame.locator('button.choice[data-act="room"]')
                check(f"{tag}: step 1 lists rooms only (with Unassigned last), no entities",
                      rooms.count() >= 6 and "Unassigned" in (rooms.last.text_content() or "") and frame.locator("input[type=checkbox]").count() == 0)
                check(f"{tag}: step 1 fits", not overflow())
                shot("step1-rooms")
                frame.locator("#q").fill("liv")
                frame.locator('button.choice[data-room="living_room"]').wait_for()
                inner.wait_for_timeout(400)
                check(f"{tag}: rooms can be searched; the search box keeps the focus", frame.locator('button.choice[data-act="room"]').count() == 1
                      and inner.evaluate("document.activeElement && document.activeElement.id") == "q")
                frame.locator('button.choice[data-act="room"]').first.click()
                frame.locator("#devices-list").wait_for()
                devices = frame.locator('button.choice[data-act="device"]').all_text_contents()
                check(f"{tag}: step 2 lists only the devices of that room, still no entities to tick",
                      len(devices) == 4 and all("Kitchen" not in d for d in devices) and frame.locator("input[type=checkbox]").count() == 0, str(devices))
                check(f"{tag}: the selection path shows where you are", "Living Room" in (frame.locator(".crumbs").text_content() or ""))
                check(f"{tag}: step 2 fits", not overflow())
                shot("step2-devices")
                frame.locator('button.choice[data-act="device"]', has_text="Sonos").click()
                frame.locator("#entities-list").wait_for()
                check(f"{tag}: step 3 lists that device's controllable entities (a device with several), none ticked",
                      frame.locator("#entities-list input[type=checkbox]").count() == 9
                      and frame.locator("#entities-list input:checked").count() == 0 and frame.locator("#create").is_disabled()
                      and "media_player.living_room_living_room" in (frame.locator("#entities-list").text_content() or ""))
                check(f"{tag}: step 3 fits", not overflow())
                shot("step3-entities")
                frame.locator('#wizard button[data-act="to-devices"]').click()
                frame.locator('button.choice[data-device="loose"]').click()
                frame.locator(f'input[data-id="{FAN}"]').wait_for()

                if label == "desktop" and scheme == "light":
                    # -------------------------------------------- combine two devices by going back; selections stay
                    frame.locator(f'input[data-id="{FAN}"]').check()
                    frame.locator('#wizard button[data-act="to-devices"]').click()
                    frame.locator("#devices-list").wait_for()
                    check("flow: going back keeps the selection and marks the device", "1 selected" in (frame.locator('button.choice[data-device="loose"]').text_content() or "")
                          and "fan.living_room_fan" in (frame.locator("#chosen").text_content() or ""))
                    frame.locator('button.choice[data-act="device"]', has_text="Living Room RGBWW Lights").click()
                    frame.locator(f'input[data-id="{RGBWW}"]').check()
                    check("flow: entities of two devices are selected together, on purpose", frame.locator("#chosen li").count() == 2)
                    frame.locator('#chosen button[data-act="drop"]').first.click()
                    check("flow: a selection can be removed again", frame.locator("#chosen li").count() == 1 and frame.locator("#entities-list input:checked").count() == 1)
                    frame.locator("#create").click()
                    frame.locator("h1", has_text="New rule").wait_for()
                    made = next(d for d in overview()["devices"] if d["targets"][0]["entity_id"] == RGBWW and d["rules"] == 0)
                    check("flow: after Add device an empty rule is open, nothing saved or deployed yet", made["status"] == "nothing"
                          and "saved as soon as it is complete" in (frame.locator("#save-status").text_content() or ""), made["status"])

                    # -------------------------------------------- a rule with the room filter
                    frame.locator("#rule-name").fill("Fan runs")
                    frame.locator('button[data-act="add-trigger"]').click()
                    frame.locator("button.entity.unset").first.click()
                    frame.locator("button.result").first.wait_for()
                    offered = frame.locator("button.result .mono").all_text_contents()
                    check("flow: “only this room” is on and the picker shows the living room only", frame.locator("#pick-room").is_checked()
                          and FAN in offered and "sensor.test_lux" not in offered and "light.kitchen_lights" not in offered, str(offered))
                    frame.locator("button.result", has_text=FAN).click()
                    frame.locator('input[data-path="triggers.0.to"]').fill("on")
                    frame.locator('button[data-act="add-condition"]').first.click()
                    frame.locator('select[data-f="condition-type"]').select_option("numeric")
                    frame.locator(".block.and button.entity.unset").click()
                    frame.locator("button.result").first.wait_for()
                    check("flow: nothing of the kitchen is offered while the filter is on", "sensor.test_lux" not in frame.locator("button.result .mono").all_text_contents())
                    frame.locator("#pick-room").uncheck()
                    frame.locator("#pick-q").fill("lux")
                    frame.locator("button.result", has_text="sensor.test_lux").click()   # a sensor of another room
                    frame.locator('input[data-path="conditions.items.0.below"]').fill("100")
                    frame.locator('button[data-act="add-action"]').click()
                    frame.locator("#save-status.ok").wait_for(timeout=8000)
                    check("flow: a rule across rooms is saved", "Test lux is below 100" in (frame.locator("#summary").text_content() or ""))
                    frame.locator('button.entity[data-path="conditions.items.0.entity_id"]').click()
                    frame.locator("#pick-room").check()
                    frame.locator("button.result[data-current]").wait_for()
                    check("flow: with the filter on again, the chosen entity of the other room is still listed", "sensor.test_lux" in (frame.locator("button.result[data-current]").text_content() or ""))
                    shot("picker-room-filter")
                    frame.locator('#dialog button[data-d="cancel"]').click()

                    # -------------------------------------------- the default rule
                    go(f"#/devices/{made['id']}", made["name"])
                    section = frame.locator("#default-rule")
                    check("flow: the device page offers an optional default below the rules, nothing configured",
                          "optional" in (section.text_content() or "") and section.locator("input").count() == 0
                          and "Living Room" in (frame.locator("#dev-room").text_content() or ""))
                    section.locator("a", has_text="Add a default").click()
                    frame.locator("h1", has_text="Default rule").wait_for()
                    frame.locator("#default-when li").first.wait_for()
                    check("flow: the default has no trigger or condition editor, and shows the rules it follows",
                          frame.locator('button[data-act="add-trigger"]').count() == 0 and frame.locator(".block.and").count() == 0
                          and "IF Living Room Fan changes to “on”" in (frame.locator("#default-when").text_content() or ""))
                    check("flow: nothing is configured by default", frame.locator('[data-part="action"]').count() == 0)
                    frame.locator('button[data-act="add-action"]').click()
                    frame.locator('select[data-f="action-kind"]').select_option("light.turn_off")
                    frame.locator("#summary", has_text="turn Living Room RGBWW Lights off").wait_for(timeout=8000)
                    frame.locator("#save-status.ok").wait_for(timeout=8000)
                    frame.locator("#side details summary").click()
                    check("flow: the default becomes native logic of the device's automation", "alias: Default" in (frame.locator("#yaml").text_content() or "")
                          and "else:" in (frame.locator("#yaml").text_content() or ""))
                    shot("default-editor")
                    go(f"#/devices/{made['id']}", made["name"])
                    frame.locator("#default-rule .ifthen").wait_for()
                    text = frame.locator("#default-rule").text_content() or ""
                    check("flow: the device page shows the default below the rules with its switch", "no rule matched" in text and "turn Living Room RGBWW Lights off" in text
                          and frame.locator('#default-rule input[data-chg="default-enabled"]').is_checked())
                    shot("device-with-default")
                    frame.locator('button[data-act="plan"]').click()
                    frame.locator("#deploy").wait_for()
                    frame.locator("#deploy").click()
                    frame.locator('h1 .status[data-status="in_sync"]').wait_for(timeout=30000)
                    check("flow: device, rule and default deployed as one automation", len(get(f"devices/{made['id']}")["live"]["automations"]) == 1)
                    frame.locator('#default-rule input[data-chg="default-enabled"]').uncheck()
                    frame.locator('h1 .status[data-status="changes_pending"]').wait_for(timeout=10000)
                    check("flow: the default can be switched off", get(f"devices/{made['id']}")["device"]["default_rule"]["enabled"] is False)

                    # -------------------------------------------- Add device from a room group
                    go("#/", "Overview")
                    group = frame.locator("details.room", has=frame.locator(".room-name", has_text="Living Room"))
                    group.locator("a[data-add-room]").click()
                    frame.locator("#devices-list").wait_for()
                    check("flow: “+ Add device” in a room opens the device step with that room chosen",
                          frame.locator("#wizard").get_attribute("data-step") == "2" and "Living Room" in (frame.locator(".crumbs").text_content() or "")
                          and frame.locator('button.choice[data-act="device"]').count() == 4)
                    frame.locator('.crumbs button[data-act="to-rooms"]').click()
                    frame.locator("#rooms-list").wait_for()
                    check("flow: the room can still be changed from there", frame.locator('button.choice[data-act="room"]').count() >= 6)
                    frame.locator('button.choice[data-act="room"]', has_text="Unassigned").click()
                    frame.locator('button.choice[data-device="loose"]').click()
                    frame.locator("#entities-list").wait_for()
                    check("flow: Unassigned offers the entities without a device", "light.flaky_lamp" in (frame.locator("#entities-list").text_content() or ""))

                if label == "phone":
                    made = next((d for d in overview()["devices"] if d["targets"][0]["entity_id"] == RGBWW and d["name"].startswith("Living Room RGBWW")), None)
                    check("phone flow: the device from the desktop is there, under its room", made is not None and made["room"]["name"] == "Living Room")
                    if made:
                        go(f"#/default/{made['id']}", "Default rule")
                        frame.locator("#summary").wait_for()
                        check("phone flow: the default editor fits the phone", not overflow())
                        shot("default-editor")
                        go(f"#/devices/{made['id']}", made["name"])
                        frame.locator("#default-rule").wait_for()   # (the editor's heading names the device too)
                        inner.wait_for_timeout(300)
                        check("phone flow: the device page with a default fits", not overflow() and frame.locator("#default-rule").count() == 1)
                        frame.locator('button[data-act="device-delete"]').click()
                        frame.locator('#dialog button[data-d="ok"]').click()
                        frame.locator("h1", has_text="Devices").wait_for()
                        check("phone flow: the device is removed again", all(d["id"] != made["id"] for d in overview()["devices"]))
                check(f"{tag}: no console errors", not errors, "; ".join(errors[:3]))
                page.close()
        browser.close()
    after = overview()
    check("the devices that were there before are unchanged", sorted(d["id"] for d in after["devices"]) == sorted(d["id"] for d in before["devices"]),
          str([d["name"] for d in after["devices"]]))
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)} of {len(results)} browser checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
