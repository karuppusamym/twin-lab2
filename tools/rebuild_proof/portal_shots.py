"""Pictures of portal pages in headless Chromium (no window opens).

    python tools/rebuild_proof/portal_shots.py <portal url> <out folder> <path>=<name> ...
"""

from __future__ import annotations

from pathlib import Path
import sys


def main(argv) -> int:
    from playwright.sync_api import sync_playwright

    base, out = argv[1].rstrip("/"), Path(argv[2])
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        for spec in argv[3:]:
            path, name = spec.split("=", 1)
            page.goto(base + "/" + path.lstrip("/"), wait_until="networkidle")
            page.wait_for_timeout(1500)
            page.screenshot(path=str(out / f"{name}.png"), full_page=True)
            print(out / f"{name}.png", page.title())
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
