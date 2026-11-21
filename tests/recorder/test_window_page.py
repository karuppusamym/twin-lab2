"""The recorder's window page (twinlab2_recorder/ui) in headless Chromium, with the real window
logic behind it: the page's `window.pywebview.api` calls go to webwindow.Bridge, as in WebView2.
No window opens.

- the page draws each state and its buttons act (pair, choose, start, stop, send, exclude);
- accessibility: axe-core (axe-playwright-python) finds no violation, in light and dark, and
  every page can be used at the minimum window size (600x460) without horizontal scrolling;
- with TWINLAB_UI_SHOTS=<folder>, a screenshot of each state is written there for review.

The portal's tokens are the page's: tokens.css must match web/src/styles.css.
"""

import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
import unittest
from unittest import mock

from twinlab2_recorder import settings as st
from twinlab2_recorder.app import Loop, RecorderApp
from twinlab2_recorder.webwindow import UI, Bridge

from test_window_headless import FIXTURES, S1, S2, SESSION, FakeHost, SlowPortal

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None
try:
    from axe_playwright_python.sync_playwright import Axe
except ImportError:  # pragma: no cover
    Axe = None

ROOT = Path(__file__).resolve().parents[2]
SHOTS = os.environ.get("TWINLAB_UI_SHOTS")
FAKE_API = """
window.pywebview = {api: new Proxy({}, {get: (_, name) => (...args) => window.__tl(name, args)})};
"""


def _chromium(p):
    try:
        return p.chromium.launch(headless=True)
    except Exception as exc:  # noqa: BLE001 - no browser installed
        raise unittest.SkipTest(f"headless Chromium is not installed: {exc}") from exc


class TokensTest(unittest.TestCase):
    def test_the_page_uses_the_portals_tokens(self):
        def props(text):
            return dict(re.findall(r"(--[\w-]+):\s*([^;]+);", text))

        portal = (ROOT / "web" / "src" / "styles.css").read_text(encoding="utf-8")
        light = props(re.search(r"^:root \{(.*?)^\}", portal, re.S | re.M).group(1))
        dark = props(
            re.search(
                r"@media \(prefers-color-scheme: dark\) \{\s*:root \{(.*?)\}\s*\}",
                portal,
                re.S,
            ).group(1)
        )
        ours = (UI / "tokens.css").read_text(encoding="utf-8")
        our_light = props(re.search(r"^:root \{(.*?)^\}", ours, re.S | re.M).group(1))
        our_dark = props(
            re.search(
                r':root:not\(\[data-theme="light"\]\) \{(.*?)\}', ours, re.S
            ).group(1)
        )
        self.assertEqual(our_light, light)
        self.assertEqual(our_dark, dark)



class BuildTest(unittest.TestCase):
    def test_the_build_keeps_what_the_window_needs(self):
        """pywebview, pythonnet and its loader (with cffi, which opens .NET) are in the build; a
        frozen recorder that lacks one has no window ('TwinLabRecorder version' says so)."""
        import sys

        sys.path.insert(0, str(ROOT / "recorder"))
        import build

        for name in ("cffi", "clr_loader", "pythonnet", "clr", "webview", "webview.platforms.edgechromium"):
            self.assertFalse(any(name == x or name.startswith(x + ".") for x in build.EXCLUDES), name)
        self.assertIn("tkinter", build.EXCLUDES)

@unittest.skipIf(sync_playwright is None, "playwright is not installed")
class WindowPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        try:
            cls.browser = _chromium(cls.pw)
        except unittest.SkipTest:
            cls.pw.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        policy = mock.patch.dict(
            os.environ, {"TWINLAB_RECORDER_POLICY": str(self.tmp / "policy.json")}
        )
        policy.start()
        self.addCleanup(policy.stop)
        self.cfg = st.Settings(self.tmp)
        self.cfg.portable = False
        self.host = FakeHost()
        self.loop = Loop().start()
        no_poll = mock.patch.object(
            RecorderApp, "_poll_loop", lambda _self: None
        )  # the tests feed apply()
        no_poll.start()
        self.addCleanup(no_poll.stop)
        self.app = self.loop.call(
            lambda: RecorderApp(self.cfg, None, self.loop, self.host).start()
        )
        self.bridge = Bridge(self.app, self.loop, self.host)
        self.addCleanup(self._close)
        self.page = None

    def _close(self):
        if self.page is not None:
            self.page.context.close()
        if not self.app.state.get("destroyed"):
            self.loop.call(self.app.destroy)
        self.loop.stop()

    def open(self, width=1040, height=700, dark=False):
        context = self.browser.new_context(viewport={"width": width, "height": height},
                                           color_scheme="dark" if dark else "light", reduced_motion="reduce")  # fmt: skip
        page = self.page = context.new_page()
        errors = self.errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        # axe-core reads the stylesheets with XHR, which a file:// page refuses: not the page's error
        page.on("console", lambda m: m.type == "error" and not re.search(r"XMLHttpRequest|net::ERR_FAILED", m.text)
                and errors.append(m.text))  # fmt: skip

        def call(name, args):
            fn = getattr(self.bridge, name, None)
            if fn is None or name.startswith("_"):
                raise AttributeError(name)
            return fn(*args)

        page.expose_function("__tl", call)
        page.add_init_script(FAKE_API)
        page.goto((UI / "index.html").as_uri())
        page.wait_for_selector(".pill")
        return page

    def do(self, fn, *args):
        return self.loop.call(fn, *args)

    def settle(self):
        """Wait until the page has drawn the app's latest state."""
        want = self.do(lambda: self.app.version)
        self.page.wait_for_function(
            f"() => typeof version !== 'undefined' && version >= {want}"
        )
        self.page.wait_for_timeout(50)

    def shot(self, name):
        if SHOTS:
            Path(SHOTS).mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(Path(SHOTS) / f"{name}.png"))

    def check_a11y(self, what):
        if Axe is None:
            self.skipTest("axe-playwright-python is not installed")
        results = Axe().run(self.page)
        self.assertEqual(
            results.violations_count, 0, f"{what}: {results.generate_report()}"
        )

    def check_fits(self, what):
        """Nothing scrolls sideways; every enabled button is reachable inside its scroller."""
        over = self.page.evaluate("""() => {
            const out = [];
            if (document.documentElement.scrollWidth > innerWidth) out.push('page ' + document.documentElement.scrollWidth);
            const main = document.getElementById('main');
            if (main.scrollWidth > main.clientWidth + 1) out.push('main ' + main.scrollWidth + '>' + main.clientWidth);
            for (const b of document.querySelectorAll('button')) {
              const r = b.getBoundingClientRect();
              if (r.width && (r.right > innerWidth + 1 || r.left < -1)) out.push('button ' + b.textContent.trim());
            }
            return out;
        }""")
        self.assertEqual(over, [], what)

    def _recorded(self):
        folder = self.cfg.recordings / SESSION
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copy(
            FIXTURES / "references-ten-apps.json", folder / "portal-outbox.json"
        )
        (folder / "session.json").write_text(
            json.dumps({"name": "Month-end close", "state": "stopped"}),
            encoding="utf-8",
        )
        self.do(self.app.refresh_done)

    # -- the states ---------------------------------------------------------------------------------

    def test_unpaired_the_page_offers_pairing_first(self):
        page = self.open()
        self.settle()
        self.assertIn("Not paired", page.inner_text("#pairing"))
        self.assertEqual(page.inner_text(".pill").strip(), "Not recording")
        self.assertTrue(page.is_visible("text=Connect this PC to your portal"))
        self.check_a11y("unpaired")
        self.shot("01-unpaired")
        page.click("#main >> text=Pair this PC")
        page.wait_for_selector("#pair-form")
        self.shot("02-pair")
        self.check_a11y("pair form")
        self.assertEqual(self.errors, [])

    def test_pairing_through_the_page(self):
        portal = SlowPortal(delay=0.2)
        self.addCleanup(portal.stop)
        page = self.open()
        page.click("#pairing button")
        page.wait_for_selector("#pair-url")
        page.fill("#pair-url", portal.url)
        page.fill("#pair-code", "123-456")
        page.fill("#pair-name", "Workstation 7")
        page.click("#pair-form button[type=submit]")
        page.wait_for_function(
            "() => document.querySelector('#pairing').innerText.includes('Paired as Workstation 7')"
        )
        self.assertEqual(self.cfg.values.get("machine"), "Workstation 7")
        page.wait_for_selector(".toast >> text=Paired")
        self.assertEqual(self.errors, [])

    def test_choosing_an_offered_session(self):
        self.cfg.paired("https://portal.example.test", 3, "Workstation 7", "tok")
        self.do(self.app.refresh_status)
        page = self.open()
        apps = {
            "apps": [
                {"app_id": 4, "name": "Billing Desk", "target": {"exe": "Billing.exe"}}
            ],
            "deny_keys": [],
        }
        self.do(self.app.apply, {"sessions": [S1, S2], "deny_keys": []}, apps, None)
        self.settle()
        start = page.locator("button[data-act=start-offered]")
        self.assertTrue(start.is_disabled())
        page.click("text=Orders")
        page.wait_for_function(
            "() => !document.querySelector('button[data-act=start-offered]').disabled"
        )
        self.assertIn("Orders", start.inner_text())
        self.assertEqual(self.app.offered_selected, 2)
        self.check_a11y("offered")
        self.shot("03-offered")
        page.click("#tab-own")
        page.wait_for_selector("text=Billing Desk")
        self.check_a11y("own session")
        self.shot("04-own")

    def _recording(self, seconds=30):
        import subprocess
        import sys

        folder = self.cfg.recordings / SESSION
        folder.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(
            [sys.executable, "-c", f"import time; time.sleep({seconds})"]
        )
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        targets = [{"name": "Ledger", "origin": "https://ledger.example.test"}, {"name": "CaseDesk", "exe": "CaseDesk.exe"},
                   {"name": "GL3270", "tn3270": "mainframe.example.test:23"}]  # fmt: skip
        conf = {
            "id": SESSION,
            "name": "Month-end close",
            "targets": targets,
            "preflight": {"ready": True},
        }
        self.do(
            lambda: (self.app.state.update(proc=proc), self.app.watch_recording(conf))
        )
        return folder

    def test_the_recording_view(self):
        page = self.open()
        folder = self._recording()
        rows = [{"kind": "channel", "channel": "web", "event": "ready"}, {"kind": "channel", "channel": "desktop", "event": "ready"},
                {"kind": "browser"}]  # fmt: skip
        (folder / "journal.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
        )
        page.wait_for_selector("text=The browser is ready")
        self.assertIn("RECORDING", page.inner_text(".pill"))
        self.assertEqual(page.locator("tbody tr").count(), 3)
        self.assertIn("Recording", page.inner_text("tbody tr:nth-child(1)"))
        self.check_a11y("recording")
        box = page.locator("button[data-act=stop]").bounding_box()
        self.assertGreater(
            box["width"], box["height"] * 2
        )  # a button on one line, not an icon
        self.shot("05-recording")
        for w, h, dark in ((600, 460, False), (1040, 700, True)):
            page.set_viewport_size({"width": w, "height": h})
            page.emulate_media(color_scheme="dark" if dark else "light")
            page.wait_for_timeout(100)
            self.check_fits(f"recording {w}x{h}")
            self.shot(f"05-recording-{w}x{h}{'-dark' if dark else ''}")
            if dark:
                self.check_a11y("recording, dark")
        with open(folder / "journal.jsonl", "a", encoding="utf-8") as f:
            f.write(
                json.dumps({"kind": "channel", "channel": "desktop", "event": "gap"})
                + "\n"
            )
        page.wait_for_selector("text=An application's recorder had trouble")
        self.shot("06-trouble-dark")
        page.wait_for_selector("button[data-act=pause]")
        page.click("button[data-act=pause]")
        page.wait_for_selector(".pill.paused")
        self.assertTrue((folder / "paused").is_file())
        page.wait_for_selector("button[data-act=resume]")
        self.assertIn("Paused", page.inner_text("tbody tr:nth-child(1)"))
        self.check_a11y("paused, dark")
        self.shot("06-paused-dark")
        page.emulate_media(color_scheme="light")
        page.wait_for_timeout(100)
        self.check_a11y("paused")
        self.shot("06-paused")
        page.click("button[data-act=resume]")
        page.wait_for_selector(".pill.recording")
        self.assertFalse((folder / "paused").exists())
        page.click("button[data-act=stop]")
        page.wait_for_selector(".pill.stopping")
        self.assertTrue((folder / "stop").is_file())
        self.assertEqual(self.errors, [])

    def test_reviewing_and_sending_a_recording(self):
        portal = SlowPortal(delay=0.2)
        self.addCleanup(portal.stop)
        self.cfg.paired(portal.url, 3, "Workstation 7", "tok")
        self._recorded()
        page = self.open()
        page.click("nav >> text=Recordings")
        page.click("text=Month-end close")
        page.wait_for_selector("text=Sent to the portal")
        self.assertTrue(page.is_visible("text=Stays on this PC"))
        self.check_a11y("review")
        self.shot("07-review")
        page.click("button[data-act=details]")
        page.wait_for_selector("pre.json")
        page.click("button[data-act=details]")
        page.click("button[data-act=send]")
        page.wait_for_selector("[role=dialog]")
        self.check_a11y("send dialog")
        self.shot("08-send-dialog")
        page.click("#dlg-yes")
        page.wait_for_selector(".toast >> text=Sent", timeout=15000)
        self.assertIn("/api/agent/references", portal.got)
        page.click("button[data-act=upload]")
        page.wait_for_selector("[role=dialog] >> text=Upload the whole recording?")
        page.keyboard.press("Escape")
        page.wait_for_selector("[role=dialog]", state="detached")
        self.assertEqual(self.errors, [])

    def test_uploads_show_progress(self):
        self._recorded()
        page = self.open()
        rows = [{"id": "j1", "session_id": SESSION, "state": "uploading", "size": 210_000_000, "received_bytes": 142_000_000},
                {"id": "j2", "session_id": "other", "state": "queued", "size": 10_000, "received_bytes": 3_000,
                 "last_error": "the portal cannot be reached: timed out", "next_try": time.time() + 25},
                {"id": "j3", "session_id": "old", "state": "confirmed", "size": 5, "confirmed_at": "yesterday"}]  # fmt: skip
        self.do(self.app.show_uploads, rows, "j1", None)
        self.do(self.app.nav, "uploads")
        page.wait_for_selector("[role=progressbar]")
        self.assertEqual(page.locator("[role=progressbar]").count(), 3)
        self.assertEqual(
            page.get_attribute("[role=progressbar] >> nth=0", "aria-valuenow"), "67"
        )
        self.assertIn("Month-end close", page.inner_text("#main"))
        self.check_a11y("uploads")
        self.shot("09-uploads")

    def test_the_privacy_page_edits_the_exclude_list(self):
        from twinlab2_recorder.workstation import exclude

        page = self.open()
        page.click("nav >> text=Privacy")
        page.wait_for_selector("#ex-pattern")
        self.check_a11y("privacy")
        self.shot("10-privacy")
        page.select_option("#ex-kind", "Window title")
        page.wait_for_function(
            "() => document.querySelector('#ex-hint').innerText.includes('* - Excel')"
        )
        page.fill("#ex-pattern", "Personal")
        page.press("#ex-pattern", "Enter")
        page.wait_for_selector("td >> text=Personal")
        self.assertIsNotNone(
            st.Settings(self.tmp).exclude_policy().title("Personal budget")
        )
        self.assertEqual(page.input_value("#ex-pattern"), "")
        page.click("button[aria-label='Remove Microsoft Excel']")
        page.wait_for_function(
            "() => !document.querySelector(\"button[aria-label='Remove Microsoft Excel']\")"
        )
        self.assertIsNone(st.Settings(self.tmp).exclude_policy().process("EXCEL.EXE"))
        with mock.patch.object(
            exclude, "running_programs", lambda: ["CaseDesk.exe", "Slack.exe"]
        ):
            page.click("button[data-act=ex-pick]")
            page.wait_for_selector("[role=dialog] >> text=Slack.exe")
        page.check("[role=dialog] >> text=Slack.exe")
        page.click("#dlg-yes")
        page.wait_for_selector("td >> text=Slack.exe")
        self.assertEqual(self.errors, [])

    def test_errors_are_explained_in_the_page(self):
        self.cfg.paired("http://127.0.0.1:9", 3, "Workstation 7", "tok")
        self.do(self.app.refresh_status)
        page = self.open(dark=True)
        self.do(
            self.app.apply,
            None,
            None,
            "the portal cannot be reached: connection refused",
        )
        page.wait_for_selector("text=We can't reach")
        self.assertIn("portal not reachable", page.inner_text("#pairing"))
        self.do(
            self.app.tell,
            "Not paired",
            "Not paired: the pairing code is not valid any more",
            "bad",
        )
        page.wait_for_selector(".toast.bad")
        self.check_a11y("offline, dark")
        self.shot("11-offline-dark")
        page.click(".toast .x")
        page.wait_for_selector(".toast", state="detached")

    def test_every_page_fits_the_minimum_window(self):
        self.cfg.paired("https://portal.example.test", 3, "Workstation 7", "tok")
        self.do(self.app.refresh_status)
        self._recorded()
        self.do(
            self.app.apply,
            {"sessions": [S1, S2], "deny_keys": []},
            {"apps": [], "deny_keys": []},
            None,
        )
        self.do(self.app.select_recording, SESSION)
        self.open(600, 460)
        for name in ("record", "recordings", "uploads", "privacy", "settings", "pair"):
            self.do(self.app.nav, name)
            self.settle()
            self.check_fits(name)
            self.shot(f"12-min-{name}")
        self.check_a11y("settings at 600x460")
        self.assertEqual(self.errors, [])


class EdgeFallbackTest(unittest.TestCase):
    """Without WebView2 the page is served on 127.0.0.1 for an Edge app window (webwindow.PageServer):
    driven here by headless Chromium instead; no Edge is started."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        policy = mock.patch.dict(
            os.environ, {"TWINLAB_RECORDER_POLICY": str(self.tmp / "policy.json")}
        )
        policy.start()
        self.addCleanup(policy.stop)
        self.cfg = st.Settings(self.tmp)
        self.cfg.portable = False

    def server(self):
        from twinlab2_recorder.webwindow import PageServer

        loop = Loop().start()
        self.addCleanup(loop.stop)
        with mock.patch.object(RecorderApp, "_poll_loop", lambda _self: None):
            app = loop.call(
                lambda: RecorderApp(self.cfg, None, loop, FakeHost()).start()
            )
        self.addCleanup(lambda: app.state.get("destroyed") or loop.call(app.destroy))
        server = PageServer(Bridge(app, loop, FakeHost())).start()
        self.addCleanup(server.stop)
        return app, server

    def post(self, server, name, args, token=None, host=None):
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        body = json.dumps(args)
        headers = {
            "Content-Type": "application/json",
            "X-TL-Token": server.token if token is None else token,
        }
        if host is not None:
            headers["Host"] = host
        conn.request("POST", f"/api/{name}", body, headers)
        r = conn.getresponse()
        return r.status, r.read()

    def test_only_the_window_with_the_token_reaches_the_bridge(self):
        app, server = self.server()
        self.assertEqual(self.post(server, "nav", ["privacy"])[0], 200)
        self.assertEqual(app.page, "privacy")
        self.assertEqual(self.post(server, "nav", ["record"], token="guess")[0], 403)
        self.assertEqual(
            self.post(server, "nav", ["record"], host="evil.example:80")[0], 403
        )  # DNS rebinding
        self.assertEqual(self.post(server, "_do", [])[0], 404)
        self.assertEqual(self.post(server, "nowhere", [])[0], 404)
        self.assertEqual(app.page, "privacy")
        import http.client

        for path in ("/../app.py", "/..%2Fapp.py", "/tokens.css/../../main.py"):
            conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
            conn.request("GET", path)
            self.assertEqual(conn.getresponse().status, 404, path)
        self.assertNotIn(
            server.token, server.url.split("#")[0]
        )  # never sent with a request

    def test_the_page_works_the_same_over_http(self):
        if sync_playwright is None:
            self.skipTest("playwright is not installed")
        app, server = self.server()
        with sync_playwright() as pw:
            browser = _chromium(pw)
            page = browser.new_page(viewport={"width": 1040, "height": 700})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(server.url)
            page.wait_for_selector(".pill")
            page.click("nav >> text=Privacy")
            page.wait_for_selector("#ex-pattern")
            self.assertEqual(app.page, "privacy")
            browser.close()
        self.assertEqual(errors, [])

    def test_the_edge_command_and_closing_the_window(self):
        import subprocess
        import sys

        from twinlab2_recorder import webwindow

        cmd = webwindow.edge_app_command(
            "msedge.exe", "http://127.0.0.1:5/index.html#t=x", self.tmp / "p"
        )
        self.assertEqual(cmd[1], "--app=http://127.0.0.1:5/index.html#t=x")
        self.assertIn(f"--user-data-dir={self.tmp / 'p'}", cmd)
        opened = []

        def fake_open(
            host,
        ):  # a stand-in for Edge's window: a process that ends at once
            opened.append(1)
            host.proc = subprocess.Popen([sys.executable, "-c", "pass"])

        with mock.patch("twinlab2_recorder.workstation.session.find_browser", lambda _b: "msedge.exe"), \
                mock.patch.object(webwindow.EdgeAppHost, "open", fake_open), \
                mock.patch.object(RecorderApp, "_poll_loop", lambda _self: None):  # fmt: skip
            self.assertTrue(webwindow.run_edge_app(self.cfg, None, "test"))
        self.assertEqual(
            opened, [1]
        )  # nothing recording: closing the window ends the program
        with mock.patch(
            "twinlab2_recorder.workstation.session.find_browser", lambda _b: None
        ):
            self.assertFalse(webwindow.run_edge_app(self.cfg, None, "test"))


if __name__ == "__main__":
    unittest.main()
