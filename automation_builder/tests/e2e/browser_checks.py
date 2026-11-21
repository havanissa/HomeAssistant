"""Browser checks of the interface through Ingress (Playwright, headless Chromium).

Every page at desktop (light and dark), tablet (both orientations) and phone widths: no console errors, no
horizontal overflow, screenshots. On the desktop a device and a rule are created and deployed entirely in the
interface; on the phone an occupancy rule is added to it, deployed, and the device is deleted again.

    python browser_checks.py <screenshot dir>

Run after ``e2e_checks.py`` (it leaves four devices with rules behind).
"""

from __future__ import annotations

import glob
import json
import os
from pathlib import Path
import sys
import urllib.request

from playwright.sync_api import sync_playwright

STACK = "http://127.0.0.1:18400"
PANEL = f"{STACK}/hassio/ingress/ab"
API = f"{STACK}/api/hassio_ingress/abIngressToken_XyZ789/api"
SIZES = {"desktop": (1440, 900), "tablet": (1024, 768), "tablet-portrait": (768, 1024), "phone": (390, 844)}
CEILING = "light.ceiling_lights"


def chromium() -> str | None:
    found = glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell"))
    return sorted(found)[-1] if found else None


def overview() -> dict:
    with urllib.request.urlopen(f"{API}/overview", timeout=30) as response:
        return json.loads(response.read())


def main(out: Path) -> int:  # noqa: PLR0915 - one linear scenario
    out.mkdir(parents=True, exist_ok=True)
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, bool(ok), detail))
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  -> {detail}"), flush=True)

    before = overview()
    first = sorted(before["devices"], key=lambda d: d["name"])[0]
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
                    inner.wait_for_timeout(350)

                for section, heading in (("overview", "Overview"), ("devices", "Devices"), ("rules", "Rule Editor"),
                                         ("settings", "Settings")):
                    frame.locator(f'nav a[data-nav="{section}"]').click()
                    frame.locator("h1", has_text=heading).first.wait_for()
                    inner.wait_for_timeout(350)
                    check(f"{tag}: {heading} has no horizontal overflow", not overflow())
                    shot(section)
                nav = frame.locator("nav a").first.bounding_box()
                check(f"{tag}: navigation targets are at least 44 px high", nav is not None and nav["height"] >= 44, str(nav))
                background = inner.evaluate("getComputedStyle(document.body).backgroundColor")
                check(f"{tag}: the {scheme} theme is applied", (background == "rgb(17, 21, 24)") == (scheme == "dark"), background)

                # one device: rules, conflicts and notes, the automation with its diagnostics, the review
                go(f"#/devices/{first['id']}", first["name"])
                frame.locator("#deployment .plan-item").first.wait_for()
                check(f"{tag}: device page has no horizontal overflow", not overflow())
                check(f"{tag}: the device page shows rules as IF / THEN", frame.locator(".rule .ifthen .kw").count() >= 2)
                check(f"{tag}: the automation's identity and status are shown",
                      "automation_builder_" in (frame.locator("#deployment").text_content() or "")
                      and frame.locator("#deployment .status").count() >= 1)
                shot("device")
                frame.locator('button[data-act="plan"]').click()
                frame.locator("#plan").wait_for()
                inner.wait_for_timeout(300)
                check(f"{tag}: the review opens and fits", not overflow() and frame.locator("#plan .plan-item").count() >= 1)
                frame.locator("#plan details summary").last.click()
                inner.wait_for_timeout(200)
                check(f"{tag}: long YAML scrolls inside its box", not overflow())
                shot("review")
                frame.locator('button[data-act="plan-close"]').click()

                # the rule editor on an existing rule
                frame.locator(".rule a.button", has_text="Edit").first.click()
                frame.locator("h1", has_text="Edit rule").wait_for()
                frame.locator("#summary").wait_for()
                inner.wait_for_timeout(300)
                check(f"{tag}: rule editor has no horizontal overflow", not overflow())
                check(f"{tag}: the editor shows IF, AND and THEN blocks",
                      frame.locator(".block.if").count() == 1 and frame.locator(".block.and").count() == 1
                      and frame.locator(".block.then").count() == 1)
                check(f"{tag}: an unchanged rule is not re-saved", "Saved." in (frame.locator("#save-status").text_content() or ""))
                frame.locator("#side details summary").click()
                check(f"{tag}: the generated YAML is shown", "triggers:" in (frame.locator("#yaml").text_content() or ""))
                frame.locator("details[data-keep=advanced] summary").click()
                inner.wait_for_timeout(200)
                check(f"{tag}: editor with YAML and Advanced open still fits", not overflow())
                shot("editor")

                if label == "desktop" and scheme == "light":
                    # ---------------------------------------- create a device and a rule, deploy: all in the interface
                    go("#/devices/new", "Add a device")
                    frame.locator("#q").fill("ceiling")
                    box = frame.locator(f'input[data-id="{CEILING}"]')
                    box.wait_for()
                    check("flow: nothing is selected for the user", frame.locator("#found input:checked").count() == 0
                          and frame.locator("#create").is_disabled())
                    box.check()
                    check("flow: the name is suggested from the selection", frame.locator("#name").input_value() != "")
                    shot("new-device")
                    frame.locator("#create").click()
                    frame.locator("h2", has_text="Rules").wait_for()
                    check("flow: the new device has no rules and nothing deployed",
                          frame.locator('h1 .status[data-status="nothing"]').count() == 1)
                    frame.locator("a", has_text="Add rule").click()
                    frame.locator("h1", has_text="New rule").wait_for()
                    frame.locator("#rule-name").fill("GUI rule")
                    frame.locator('button[data-act="add-trigger"]').click()
                    check("flow: an incomplete rule is not saved", "Not saved yet" in (frame.locator("#save-status").text_content() or "")
                          or "new rule" in (frame.locator("#save-status").text_content() or "").lower())
                    frame.locator("button.entity.unset").first.click()
                    frame.locator("#pick-q").fill("Test motion")
                    frame.locator("button.result", has_text="binary_sensor.test_motion").click()
                    frame.locator('input[data-path="triggers.0.to"]').fill("on")
                    frame.locator('button[data-act="add-condition"]').first.click()
                    frame.locator('select[data-f="condition-type"]').select_option("numeric")
                    frame.locator(".block.and button.entity.unset").click()
                    frame.locator("#pick-q").fill("lux")
                    frame.locator("button.result", has_text="sensor.test_lux").click()
                    frame.locator('input[data-path="conditions.items.0.below"]').fill("100")
                    frame.locator('button[data-act="add-action"]').click()
                    frame.locator('input[data-path="actions.0.data.brightness_pct"]').fill("55")
                    frame.locator("#summary", has_text="brightness 55 %").wait_for(timeout=8000)
                    frame.locator("#save-status.ok").wait_for(timeout=8000)
                    summary = frame.locator("#summary").text_content() or ""
                    check("flow: the rule built in the editor reads as IF / AND / THEN",
                          "Test motion changes to “on”" in summary and "is below 100" in summary and "brightness 55 %" in summary, summary)
                    check("flow: it is saved as soon as it is complete", "Saved." in (frame.locator("#save-status").text_content() or ""))
                    shot("new-rule")
                    frame.locator("#side a", has_text="Review and deploy").click()
                    frame.locator("h2", has_text="Rules").wait_for()
                    check("flow: saved but not deployed", frame.locator('h1 .status[data-status="not_deployed"]').count() == 1)
                    frame.locator('button[data-act="plan"]').click()
                    frame.locator("#deploy").wait_for()
                    check("flow: the review offers the deployment", frame.locator("#deploy").is_enabled())
                    frame.locator("#deploy").click()
                    frame.locator('h1 .status[data-status="in_sync"]').wait_for(timeout=30000)
                    check("flow: deployed from the interface and confirmed", "accepted the deployment" in (frame.locator("#last-result").text_content() or ""))
                    shot("deployed")

                if label == "phone":
                    # ---------------------------------------- on the phone: an occupancy rule, deploy, delete the device
                    ceiling = next((d for d in overview()["devices"] if d["targets"][0]["entity_id"] == CEILING), None)
                    check("phone flow: the device created on the desktop is there", ceiling is not None)
                    if ceiling:
                        go(f"#/rules/{ceiling['id']}/new", "New rule")
                        frame.locator('button[data-act="add-trigger"]').click()
                        frame.locator('select[data-f="trigger-type"]').select_option("occupancy")
                        frame.locator("button.entity.unset").first.click()
                        wanted = frame.locator("button.result", has_text="binary_sensor.sod_kamar_occupied")
                        wanted.wait_for()
                        offered = frame.locator("button.result .mono").all_text_contents()
                        check("phone flow: the picker offers binary sensors only, occupancy-like ones first",
                              all(e.startswith("binary_sensor.") for e in offered) and "binary_sensor.sod_kamar_occupied" in offered[:8], str(offered[:8]))
                        shot("picker")
                        wanted.click()
                        frame.locator('button[data-act="add-action"]').click()
                        frame.locator("#save-status.ok").wait_for(timeout=8000)
                        frame.locator("details[data-keep=advanced] summary").click()
                        info = frame.locator("#recheck-info")
                        info.wait_for()
                        inner.wait_for_timeout(600)
                        check("phone flow: Advanced shows the generated occupancy checks",
                              "Generated checks: 2 s and 15 s" in (info.text_content() or ""), info.text_content() or "")
                        check("phone flow: editor with Advanced open fits the phone", not overflow())
                        shot("occupancy-rule")
                        go(f"#/devices/{ceiling['id']}", ceiling["name"])
                        frame.locator('button[data-act="plan"]').click()
                        frame.locator("#deploy").wait_for()
                        frame.locator("#deploy").click()
                        frame.locator('h1 .status[data-status="in_sync"]').wait_for(timeout=30000)
                        check("phone flow: deployed from the phone", True)
                        frame.locator('button[data-act="device-delete"]').click()
                        dialog = frame.locator("#dialog")
                        check("phone flow: deleting asks first and names the consequence",
                              "deleted from Home Assistant" in (dialog.text_content() or ""))
                        shot("delete-dialog")
                        dialog.locator('button[data-d="ok"]').click()
                        frame.locator("h1", has_text="Devices").wait_for()
                        check("phone flow: the device and its automation are gone",
                              all(d["targets"][0]["entity_id"] != CEILING for d in overview()["devices"]))
                check(f"{tag}: no console errors", not errors, "; ".join(errors[:3]))
                page.close()
        browser.close()
    after = overview()
    check("the devices of the e2e run are unchanged", sorted(d["id"] for d in after["devices"]) == sorted(d["id"] for d in before["devices"])
          and all(d["status"] == "in_sync" for d in after["devices"]), str([(d["name"], d["status"]) for d in after["devices"]]))
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)} of {len(results)} browser checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
