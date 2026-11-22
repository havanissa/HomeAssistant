"""Browser checks through Ingress (the fake Supervisor's sidebar panel) and in Home Assistant's own frontend.

    python browser_checks.py <tokens.json> <screenshot dir>

* Overview, Export (filters, grouping, selection), Archives, archive inspection, Import (a refused corrupted
  file) and a Restore preview at desktop (light and dark), tablet (landscape and portrait) and phone widths:
  no console errors, no failed requests, no sideways scrolling, touch targets of at least 44 px on the phone,
  the theme switch.
* On the desktop the whole Export & Delete workflow is done in the interface for two automations (selection,
  analysis, verification, download link, typed confirmation, report) and they are restored from the Restore page.
* Home Assistant's own automation dashboard shows the restored categories and labels.
"""

from __future__ import annotations

import glob
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright

STACK = "http://127.0.0.1:18410"
PANEL = f"{STACK}/hassio/ingress/aie"
API = f"{STACK}/api/hassio_ingress/aieIngressToken_Qw3rty/api"
HA = "http://127.0.0.1:18123"
SIZES = {"desktop": (1440, 900), "tablet": (1024, 768), "tablet-portrait": (768, 1024), "phone": (390, 844)}
PAIR = ["aie_plain", "aie_unicode"]


def chromium() -> str | None:
    found = glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell"))
    return sorted(found)[-1] if found else None


def api(path: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(f"{API}/{path}", data=json.dumps(body).encode() if body is not None else None,
                                     headers={"X-AIE": "1", "Content-Type": "application/json"},
                                     method="POST" if body is not None else "GET")
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def ha_get(token: str, path: str) -> tuple[int, dict | None]:
    request = urllib.request.Request(f"{HA}/api/{path}", headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as err:
        return err.code, None


def main(tokens_path: Path, out: Path) -> int:  # noqa: PLR0915 - one linear scenario
    out.mkdir(parents=True, exist_ok=True)
    tokens = json.loads(tokens_path.read_text())
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, bool(ok), detail))
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  -> {detail}"), flush=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium())
        archive_id = api("archives")["archives"][0]["archive_id"]
        for label, (width, height) in SIZES.items():
            for scheme in (("light", "dark") if label == "desktop" else ("light",)):
                tag = f"{label}-{scheme}"
                page = browser.new_page(viewport={"width": width, "height": height}, color_scheme=scheme,
                                        has_touch=label == "phone")
                errors: list[str] = []
                # failed requests are recorded below (the refused upload is expected); the browser's own
                # "Failed to load resource" console line for them is not a page error
                page.on("console", lambda m, e=errors: e.append(m.text) if m.type == "error"
                        and not m.text.startswith("Failed to load resource") else None)
                page.on("pageerror", lambda err, e=errors: e.append(str(err)))
                page.on("response", lambda r, e=errors: e.append(f"HTTP {r.status} {r.url}") if r.status >= 400
                        and "/upload" not in r.url else None)
                page.goto(PANEL)
                frame = page.frame_locator("#ingress")
                inner = next(f for f in page.frames if "hassio_ingress" in f.url)

                def overflow(inner=inner) -> bool:
                    return inner.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth + 1")

                def go(hash_: str, ready: str, inner=inner, frame=frame) -> None:
                    inner.evaluate(f"location.hash = {json.dumps(hash_)}")
                    frame.locator(ready).first.wait_for(timeout=30000)

                def shot(name: str, page=page, tag=tag) -> None:
                    page.screenshot(path=str(out / f"{tag}-{name}.png"), full_page=False)

                frame.locator(".tiles .tile").first.wait_for(timeout=30000)
                check(f"{tag}: overview shows the counts", frame.locator(".tiles .tile").count() == 6)
                check(f"{tag}: overview offers Export & Delete", frame.locator("#start-cleanup").is_visible())
                check(f"{tag}: overview fits the width", not overflow())
                bg = inner.evaluate("getComputedStyle(document.body).backgroundColor")
                check(f"{tag}: {scheme} theme applied", (bg == "rgb(16, 21, 23)") == (scheme == "dark"), bg)
                shot("overview")

                go("#/export", ".auto")
                rows = frame.locator("#list .auto").count()
                inv = api("inventory")
                check(f"{tag}: export lists every automation", rows == inv["summary"]["total"], f"{rows} vs {inv['summary']['total']}")
                check(f"{tag}: export fits the width", not overflow())
                night = next(x["id"] for x in inv["labels"] if x["name"] == "AIE Night")
                frame.locator("#f-label").select_option(night)
                want = sum(1 for i in inv["items"] if any(x["id"] == night for x in i["labels"]))
                check(f"{tag}: label filter", frame.locator("#list .auto").count() == want, str(want))
                frame.locator("#f-label").select_option("")
                frame.locator("#f-group").select_option("category")
                headers = frame.locator("#list .group h3").all_inner_texts()
                check(f"{tag}: grouping by category", any("AIE Lights" in h for h in headers) and any("No category" in h for h in headers), str(headers))
                frame.locator("#f-group").select_option("source")
                headers = frame.locator("#list .group h3").all_inner_texts()
                check(f"{tag}: grouping by source separates protected and external",
                      any("Automation Builder" in h for h in headers) and any("External YAML" in h for h in headers), str(headers))
                frame.locator("#f-group").select_option("none")
                frame.locator("#f-q").fill("Kvøld")
                check(f"{tag}: search", frame.locator("#list .auto").count() == 1)
                frame.locator("#f-q").fill("")
                frame.locator('#list input[data-key="aie_plain"]').check()
                check(f"{tag}: selection count", "1 selected" in frame.locator("#sel-count").inner_text())
                check(f"{tag}: protected rows are marked", frame.locator(".chip.prot").count() >= 3)
                frame.locator('#list input[data-key="aie_plain"]').uncheck()
                if label == "phone":
                    small = inner.evaluate("""[...document.querySelectorAll('button, .button, nav a, select, input[type=checkbox]')]
                        .filter(e => e.offsetParent && e.type !== 'checkbox').map(e => e.getBoundingClientRect())
                        .filter(r => r.height < 43.5).length""")
                    check(f"{tag}: touch targets are at least 44 px", small == 0, str(small))
                shot("export")

                go("#/archives", "#archives")
                check(f"{tag}: archives listed", frame.locator("#archives tbody tr").count() >= 1)
                check(f"{tag}: archives page fits the width", not overflow())
                shot("archives")
                go(f"#/archive/{archive_id}", ".kv")
                check(f"{tag}: archive inspection", frame.locator(".auto").count() >= 1 and not overflow())
                frame.locator("#verify").click()
                frame.locator("#verify-result .checks").wait_for(timeout=30000)
                check(f"{tag}: verification from the interface", frame.locator("#verify-result .checks li").count() >= 6)
                shot("archive")

                go("#/import", "#zip-drop")
                frame.locator("#zip").set_input_files({"name": "broken.zip", "mimeType": "application/zip",
                                                       "buffer": b"PK\x03\x04 this is broken"})
                frame.locator("#upload-error").wait_for(timeout=15000)
                check(f"{tag}: a corrupted archive is refused with a message", "Refused" in frame.locator("#upload-error").inner_text())
                check(f"{tag}: import fits the width", not overflow())
                shot("import")

                go(f"#/restore/{archive_id}", "#items")
                check(f"{tag}: restore preview shows the existing automations as 'metadata only'",
                      "restore its metadata" in frame.locator("#items").inner_text())
                check(f"{tag}: restore preview fits the width", not overflow())
                shot("restore")

                if tag == "desktop-light":
                    inner.evaluate("document.getElementById('theme').value = 'dark'; document.getElementById('theme').dispatchEvent(new Event('change'))")
                    check(f"{tag}: theme switch to dark", inner.evaluate("document.documentElement.dataset.theme") == "dark")
                    inner.evaluate("document.getElementById('theme').value = 'auto'; document.getElementById('theme').dispatchEvent(new Event('change'))")

                    # ---- the whole Export & Delete workflow in the interface
                    go("#/cleanup/new", "#eligible")
                    frame.locator("#all").uncheck()
                    for key in PAIR:
                        frame.locator(f'#eligible input[data-key="{key}"]').check()
                    check(f"{tag}: wizard selection", "2 of" in frame.locator("#n-chosen").inner_text())
                    frame.locator("#go").click()
                    frame.locator("#delete").wait_for(timeout=60000)
                    check(f"{tag}: wizard shows the verification and a download link",
                          frame.locator("#download").is_visible() and "Verification checks" in frame.locator("body").inner_text())
                    check(f"{tag}: delete button disabled until confirmed", frame.locator("#delete").is_disabled())
                    shot("cleanup-preview")
                    frame.locator("#ack").check()
                    frame.locator("#count").fill("1")
                    check(f"{tag}: wrong count keeps the button disabled", frame.locator("#delete").is_disabled())
                    frame.locator("#count").fill("2")
                    frame.locator("#delete").click()
                    frame.locator("h2", has_text="Report").wait_for(timeout=90000)
                    body = frame.locator("body").inner_text()
                    check(f"{tag}: report shows the deletion and unchanged protected automations",
                          "all unchanged" in body and "Deleted" in body, body[:400])
                    check(f"{tag}: deleted in Home Assistant", all(ha_get(tokens["llat"], f"config/automation/config/{k}")[0] == 404 for k in PAIR))
                    shot("cleanup-report")
                    frame.locator("text=Restore from this archive…").click()
                    frame.locator("#run").wait_for(timeout=30000)
                    frame.locator("#run").click()
                    frame.locator("dialog [data-d=ok]").click()
                    for _ in range(120):  # (wait_for_function would need 'unsafe-eval', which the App's CSP forbids)
                        if inner.evaluate("location.hash").startswith("#/operation/"):
                            break
                        time.sleep(0.25)
                    frame.locator("h1 [data-status=completed]").wait_for(timeout=90000)
                    check(f"{tag}: restored from the interface", all(ha_get(tokens["llat"], f"config/automation/config/{k}")[0] == 200 for k in PAIR))
                    shot("restore-done")
                check(f"{tag}: no console errors or failed requests", not errors, "; ".join(errors[:5]))
                page.close()

        # ---- Home Assistant's own automation dashboard shows the categories and labels
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(HA + "/")
        page.evaluate("""t => localStorage.setItem('hassTokens', JSON.stringify({access_token: t, token_type: 'Bearer',
            expires_in: 1800, hassUrl: location.origin, clientId: location.origin + '/', expires: Date.now() + 3600e3 * 24,
            refresh_token: ''}))""", tokens["llat"])
        page.goto(HA + "/config/automation/dashboard")
        deadline = time.time() + 60
        text = ""
        while time.time() < deadline:
            text = page.evaluate("""() => {
                const out = []; const walk = (n) => { if (n.shadowRoot) walk(n.shadowRoot);
                  for (const c of n.children || []) walk(c); if (n.nodeType === 1 && n.childNodes) for (const t of n.childNodes)
                  if (t.nodeType === 3) out.push(t.textContent); }; walk(document.body); return out.join(' '); }""")
            if "AIE Kitchen motion" in text and "AIE Lights" in text:
                break
            time.sleep(1)
        for word in ("AIE Lights", "AIE Safety", "AIE Comfort", "AIE Lighting", "AIE Night", "AIE Security",
                     "AIE Kitchen motion (custom name)", "AIE Kvøld ljós"):
            check(f"Home Assistant's automation dashboard shows {word}", word in text)
        page.screenshot(path=str(out / "ha-automation-dashboard.png"))
        page.close()
        browser.close()
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} browser checks passed")
    (out / "results.json").write_text(json.dumps(results, indent=1))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]), Path(sys.argv[2])))
