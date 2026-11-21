"""The recorder's own browser, started headless (`record --headless`, TWINLAB_BROWSER_HEADLESS=1):
no window opens, and its pages are fitted to the standard 1280x720 viewport, so pictures compare
at a known size (rebuild proof, launched-web: a headless Edge started at --window-size=1280,720
shows 1250x625, because the window size includes its frame).

The attach test needs Playwright's Chromium and skips without it; Chromium runs with
--headless=new, so nothing opens on the desktop."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import web_harness as wh

EXE = wh.chromium_path()

PAGE = b"""<!DOCTYPE html><html><head><title>Size</title></head><body>
<button id="go" type="button" onclick="document.title='clicked'">Go</button>
<a id="tab" href="/second" target="_blank">Second</a>
</body></html>"""
SECOND = b"""<!DOCTYPE html><html><head><title>Second</title></head><body>
<button id="ok" type="button">OK</button></body></html>"""


class HeadlessSwitch(unittest.TestCase):
    def test_off_unless_set(self):
        from twinlab2_recorder.workstation import session

        with mock.patch.dict(os.environ, {session.HEADLESS_ENV: ""}):
            self.assertFalse(session.browser_headless())
        for value in ("1", "true", "YES"):
            with mock.patch.dict(os.environ, {session.HEADLESS_ENV: value}):
                self.assertTrue(session.browser_headless())

    def test_record_headless_refuses_an_attached_browser(self):
        from twinlab2_recorder import main

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}):
            with self.assertRaises(SystemExit) as got:
                main.main(["record", "--origin", "http://127.0.0.1:1", "--headless",
                           "--cdp", "http://127.0.0.1:9222"])  # fmt: skip
        self.assertIn("--headless", str(got.exception))


@unittest.skipUnless(EXE, "Playwright's Chromium is not installed")
class FittedViewport(unittest.TestCase):
    """attach(viewport=...) on a window smaller than 1280x720: every tab, the popup too, shows
    1280x720, every picture is 1280x720 and recording.json names the viewport."""

    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        from twinlab2_recorder.web import recorder

        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name) / "web"
        with wh.site({"/": PAGE, "/second": SECOND}) as base, wh.headless_chromium(EXE, "1000,600") as cdp:
            stop, ready, thread, errors = wh.attach_in_thread(cdp, cls.out, viewport=recorder.VIEWPORT)
            if not ready.wait(30):
                raise RuntimeError("attach() did not get ready")
            if errors:
                raise errors[0]
            with sync_playwright() as pw:
                context = pw.chromium.connect_over_cdp(cdp).contexts[0]
                page = context.pages[0]
                page.goto(base + "/")
                page.wait_for_timeout(600)
                cls.first = page.evaluate("() => [innerWidth, innerHeight]")
                page.click("#go")
                page.wait_for_timeout(800)
                with context.expect_page() as popup:
                    page.click("#tab")
                second = popup.value
                second.wait_for_load_state()
                second.wait_for_timeout(600)
                cls.second = second.evaluate("() => [innerWidth, innerHeight]")
                second.click("#ok")
                second.wait_for_timeout(1200)
            stop.set()
            thread.join(60)
        if errors:
            raise errors[0]
        cls.meta = json.loads((cls.out / "recording.json").read_text(encoding="utf-8"))
        cls.steps = [json.loads(line) for line in (cls.out / "steps.jsonl").read_text(encoding="utf-8").splitlines()]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_every_tab_shows_the_standard_viewport(self):
        self.assertEqual(self.first, [1280, 720])
        self.assertEqual(self.second, [1280, 720])

    def test_recording_json_names_it(self):
        self.assertEqual(self.meta["viewport"], {"width": 1280, "height": 720})
        self.assertEqual(self.meta["device_scale_factor"], 1)

    def test_every_picture_is_1280x720(self):
        from PIL import Image

        shots = [s["screenshot"] for s in self.steps if s.get("screenshot")]
        self.assertGreaterEqual(len(shots), 2)
        for rel in shots:
            with Image.open(self.out / rel) as img:
                self.assertEqual(img.size, (1280, 720), rel)
        self.assertTrue(any(s.get("tab") == "t2" and s.get("screenshot") for s in self.steps))


@unittest.skipUnless(EXE, "Playwright's Chromium is not installed")
class UnfittedViewport(unittest.TestCase):
    """Without `viewport` (a browser someone else started) nothing is resized: the window keeps
    its own size and recording.json says nothing about a viewport it did not set."""

    def test_window_keeps_its_size(self):
        from playwright.sync_api import sync_playwright

        with tempfile.TemporaryDirectory() as tmp, wh.site({"/": PAGE}) as base, \
                wh.headless_chromium(EXE, "1000,600") as cdp:  # fmt: skip
            out = Path(tmp) / "web"
            stop, ready, thread, errors = wh.attach_in_thread(cdp, out)
            self.assertTrue(ready.wait(30))
            with sync_playwright() as pw:
                page = pw.chromium.connect_over_cdp(cdp).contexts[0].pages[0]
                page.goto(base + "/")
                page.wait_for_timeout(400)
                size = page.evaluate("() => [innerWidth, innerHeight]")
            stop.set()
            thread.join(60)
            self.assertFalse(errors)
            self.assertNotEqual(size, [1280, 720])
            self.assertIsNone(json.loads((out / "recording.json").read_text(encoding="utf-8"))["viewport"])


if __name__ == "__main__":
    unittest.main()
