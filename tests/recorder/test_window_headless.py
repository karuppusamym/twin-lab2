"""The recorder's window logic (twinlab2_recorder.app.RecorderApp) on its own window thread, with
no window: what the page would show is the app's snapshot, and the page's buttons are its
actions. The page itself is tested in headless Chromium (test_window_page.py).

What needs the visible window is in recorder/GO-DESKTOP-CHECKLIST.md.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from twinlab2_recorder import app as app_module
from twinlab2_recorder import settings as st
from twinlab2_recorder.app import Loop, RecorderApp
from twinlab2_recorder.webwindow import Bridge

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SESSION = "20261120T140000Z-10a9b8c7"


class SlowPortal:
    """A portal on 127.0.0.1 that takes `delay` seconds to answer."""

    def __init__(self, delay=1.5):
        self.delay, self.got = delay, []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self, doc):
                time.sleep(outer.delay)
                raw = json.dumps(doc).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path.startswith("/api/agent/sessions"):
                    self._answer({"sessions": [], "deny_keys": []})
                else:
                    self._answer({"apps": [], "deny_keys": []})

            def do_POST(self):
                outer.got.append(self.path)
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.path == "/api/agent/pair":
                    self._answer({"token": "tok", "machine_id": 3, "name": "Workstation 7"})
                else:
                    self._answer({"recording_id": 5, "url": "/recordings/5"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class FakeHost:
    def __init__(self):
        self.closed, self.warnings = False, []

    def close(self):
        self.closed = True

    def warn(self, message):
        self.warnings.append(message)


S1 = {"id": 1, "name": "Billing", "automation_id": None, "targets": [{"name": "ERP", "level": "full"}]}
S2 = {"id": 2, "name": "Orders", "automation_id": None, "targets": [{"name": "Shop", "level": "full"}]}
NO_APPS = {"apps": [], "deny_keys": []}


class WindowLogicTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = st.Settings(self.tmp)
        self.cfg.portable = False
        self._no_office_policy()
        self.host = FakeHost()
        self.loop = Loop().start()
        self.addCleanup(self._close)
        self.app = None

    def _close(self):
        if self.app is not None and not self.app.state.get("destroyed"):
            self.loop.call(self.app.destroy)
        self.loop.stop()
        self.loop.thread.join(5)

    def window(self) -> RecorderApp:
        self.app = self.loop.call(lambda: RecorderApp(self.cfg, None, self.loop, self.host).start())
        return self.app

    def do(self, fn, *args):
        return self.loop.call(fn, *args)

    def snap(self) -> dict:
        return self.do(self.app.snapshot)

    def pump(self, until, timeout=10):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.do(until):
                return True
            time.sleep(0.02)
        return False

    def _no_office_policy(self, doc=None):
        path = self.tmp / "policy.json"
        if doc is not None:
            path.write_text(json.dumps(doc), encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {"TWINLAB_RECORDER_POLICY": str(path)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _recorded(self, name="Month-end close"):
        folder = self.cfg.recordings / SESSION
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURES / "references-ten-apps.json", folder / "portal-outbox.json")
        (folder / "session.json").write_text(json.dumps({"name": name, "state": "stopped"}), encoding="utf-8")
        return folder

    # -- the portal --------------------------------------------------------------------------------

    def test_an_offline_portal_shows_in_the_status(self):
        self.cfg.paired("http://127.0.0.1:9", 3, "Workstation 7", "tok")
        w = self.window()
        self.do(w.poll_portal)
        self.assertTrue(self.pump(lambda: "cannot be reached" in w.status), w.status)
        self.assertTrue(w.status.startswith("Paired as Workstation 7: the portal cannot be reached"))
        self.assertIn("cannot be reached", self.snap()["pairing"]["error"])  # the page's banner

    def test_pairing_and_sending_keep_the_window_responsive(self):
        portal = SlowPortal(delay=1.0)
        self.addCleanup(portal.stop)
        w = self.window()
        began = time.monotonic()
        self.assertTrue(self.do(w.on_pair, portal.url, "123-456", "Workstation 7"))
        self.assertLess(time.monotonic() - began, 0.3)  # the portal takes 1 s: not on this thread
        self.assertTrue(self.snap()["pairing"]["busy"])
        self.assertTrue(self.pump(lambda: w.activity.startswith("Connecting: pairing with")))
        self.assertTrue(self.pump(lambda: self.cfg.values.get("token")))
        self.assertIn("Paired as Workstation 7", w.status)
        self.assertTrue(self.pump(lambda: any(n["title"] == "Paired" for n in w.notices)))

        folder = self._recorded()
        self.do(w.refresh_done)
        self.do(w.select_recording, SESSION)
        review = self.snap()["recordings"]["review"]
        self.assertIn(f"Destination: {portal.url}", review["summary"])
        self.assertIsNone(review["json"])
        self.do(w.on_details)
        self.assertIn('"flows"', self.snap()["recordings"]["review"]["json"])
        self.do(w.on_send)
        dialog = self.snap()["dialog"]
        self.assertIn("Send the summary shown here", dialog["text"])
        began = time.monotonic()
        self.do(w.answer, dialog["id"], True, [])
        self.assertLess(time.monotonic() - began, 0.3)
        self.assertTrue(self.pump(lambda: w.activity.startswith("Sending: references of")))
        self.assertTrue(self.pump(lambda: any("Sent." in n["text"] for n in w.notices)), (w.notices, w.activity))
        self.assertIn("/api/agent/references", portal.got)
        self.assertTrue((folder / "sent.json").is_file())

    def test_a_wrong_address_is_refused_at_once(self):
        w = self.window()
        self.assertFalse(self.do(w.on_pair, "http://example.com", "1", "PC"))
        self.assertIn("HTTPS", w.notices[-1]["text"])
        self.assertFalse(self.do(w.on_pair, "", "1", "PC"))  # an incomplete form
        self.assertEqual(w.activity, "")

    def test_offered_selection_survives_a_poll(self):
        w = self.window()
        self.do(w.apply, {"sessions": [S1, S2], "deny_keys": []}, NO_APPS, None)
        self.do(w.select_offered, 2)
        self.do(w.apply, {"sessions": [dict(S2, name="Orders (EU)")], "deny_keys": []}, NO_APPS, None)
        offered = self.snap()["offered"]
        self.assertEqual(offered["selected"], 2)
        self.assertEqual(offered["sessions"][0]["label"], "Orders (EU)  ·  Shop")
        self.do(w.apply, {"sessions": [S1], "deny_keys": []}, NO_APPS, None)  # withdrawn
        self.assertIsNone(self.snap()["offered"]["selected"])
        self.assertFalse(self.snap()["can"]["start_offered"])

    # -- recording ---------------------------------------------------------------------------------

    def _recording(self, w, seconds, targets=None):
        folder = self.cfg.recordings / SESSION
        folder.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        conf = {"id": SESSION, "name": "Month-end close", "targets": targets or [], "preflight": {"ready": True}}
        self.do(lambda: (w.state.update(proc=proc), w.watch_recording(conf)))
        return proc

    def test_the_recording_indicator_follows_the_journal(self):
        w = self.window()
        targets = [{"name": "Ledger", "origin": "https://ledger.example.test"}, {"name": "CaseDesk", "exe": "CaseDesk.exe"}]
        self._recording(w, 3, targets)
        rec = self.snap()["recording"]
        self.assertEqual(rec["mode"], "recording")
        self.assertEqual([(a["name"], a["surface"], a["status"]) for a in rec["apps"]],
                         [("Ledger", "web", "Starting"), ("CaseDesk", "desktop", "Starting")])  # fmt: skip
        journal = self.cfg.recordings / SESSION / "journal.jsonl"
        rows = [{"kind": "channel", "channel": "web", "event": "ready"}, {"kind": "channel", "channel": "desktop", "event": "ready"},
                {"kind": "browser"}, {"kind": "channel", "channel": "desktop", "event": "restarted"}]  # fmt: skip
        journal.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        self.assertTrue(self.pump(lambda: w.snapshot()["recording"]["health"] == "trouble"))
        rec = self.snap()["recording"]
        self.assertTrue(rec["browser_ready"])
        self.assertEqual(rec["apps"][0]["status"], "Recording")
        self.assertEqual((rec["apps"][1]["status"], rec["apps"][1]["tone"]), ("Restarting · restarted 1×", "warn"))
        self.assertTrue(self.snap()["can"]["stop"])
        self.assertFalse(self.snap()["can"]["start_own"])
        self.do(w.on_stop)
        self.assertTrue((self.cfg.recordings / SESSION / "stop").is_file())
        self.assertEqual(self.snap()["recording"]["mode"], "stopping")
        self.assertFalse(self.snap()["can"]["stop"])
        self.assertTrue(self.pump(lambda: w.snapshot()["recording"]["mode"] == "idle"))
        s = self.snap()
        self.assertEqual(s["page"], "recordings")
        self.assertEqual(s["note"]["title"], "Recording saved on this PC")

    def test_failed_checks_ask_before_recording(self):
        w = self.window()
        block = {"checks": [{"name": "web", "ok": False, "detail": "no answer"}], "ready": False}
        launched = []
        with mock.patch.object(w.m, "preflight_checks", lambda cfg, conf: block), \
                mock.patch.object(w, "launch", launched.append):  # fmt: skip
            self.do(w.on_start_own, "Mine", [], "https://shop.example.test")
            self.assertTrue(self.pump(lambda: w.dialog is not None))
            dialog = self.snap()["dialog"]
            self.assertEqual(dialog["lines"], ["web: no answer"])
            self.do(w.answer, dialog["id"], False, [])
            self.assertEqual(self.snap()["note"]["title"], "Not recording: fix the failed checks, then press Start again")
            self.assertEqual(launched, [])
            self.do(w.on_start_own, "Mine", [], "https://shop.example.test")
            self.assertTrue(self.pump(lambda: w.dialog is not None))
            self.do(w.answer, w.dialog["id"], True, [])
        self.assertEqual(len(launched), 1)
        self.assertTrue(launched[0]["override_preflight"])
        self.assertEqual(launched[0]["targets"], [{"origin": "https://shop.example.test", "level": "full"}])

    def test_nothing_to_record_is_explained(self):
        w = self.window()
        self.do(w.on_start_own, "Mine", [], "")
        self.assertEqual(w.notices[-1]["title"], "Nothing to record")
        self.do(w.on_start_offered)
        self.assertEqual(w.notices[-1]["title"], "Choose a session")
        self.do(w.set_settings, {"cdp": "http://10.0.0.5:9222"})
        self.do(w.on_start_own, "Mine", [], "CaseDesk.exe")
        self.assertIn("attach only to a browser on this PC", w.notices[-1]["text"])

    def test_closing_during_a_recording_waits_off_the_window_thread(self):
        w = self.window()
        proc = self._recording(w, 1.5)
        self.assertFalse(self.do(w.on_close))  # asks first; the window stays
        dialog = self.snap()["dialog"]
        self.assertEqual(dialog["title"], "A recording is running")
        began = time.monotonic()
        self.do(w.answer, dialog["id"], True, [])
        self.assertLess(time.monotonic() - began, 0.3)
        self.assertTrue((self.cfg.recordings / SESSION / "stop").is_file())
        self.assertTrue(self.pump(lambda: w.activity.startswith("Stopping: finishing the recording")))
        self.assertEqual(self.snap()["recording"]["stop_limit"], app_module.STOP_WAIT_S)
        self.assertFalse(self.do(w.on_close))  # pressed again: told to wait, not a second stop
        self.assertIn("Still finishing", w.activity)
        self.assertTrue(self.pump(lambda: w.state.get("destroyed")))
        self.assertTrue(self.host.closed)
        self.assertIsNotNone(proc.poll())
        self.assertEqual(self.host.warnings, [])

    def test_closing_with_no_recording_closes_at_once_and_no_closes_nothing(self):
        w = self.window()
        self._recording(w, 5)
        self.assertFalse(self.do(w.on_close))
        self.do(w.answer, w.dialog["id"], False, [])
        self.assertFalse(w.state["closing"])
        self.assertEqual(self.snap()["recording"]["mode"], "recording")

    def test_a_recording_that_does_not_finish_in_time_is_explained(self):
        w = self.window()
        self._recording(w, 30)
        with mock.patch.object(app_module, "STOP_WAIT_S", 0.5):
            self.do(w.on_close)
            self.do(w.answer, w.dialog["id"], True, [])
            self.assertTrue(self.pump(lambda: w.state.get("destroyed")))
        self.assertEqual(len([m for m in self.host.warnings if "did not finish" in m]), 1, self.host.warnings)

    # -- the exclude list and the window's states ---------------------------------------------------

    def _rows(self):
        return [(r["kind"], r["pattern"], r["name"], r["source"]) for r in self.snap()["exclude"]["rows"]]

    def test_the_exclude_list_is_edited_in_the_window_and_kept(self):
        from twinlab2_recorder.workstation import exclude

        w = self.window()
        self.assertEqual(len(self._rows()), len(exclude.DEFAULTS))
        self.assertIn(("Program", "WINWORD.EXE", "Microsoft Word", "Default"), self._rows())
        self.do(w.exclude_kind, "Window title")
        self.assertIn("* - Excel", self.snap()["exclude"]["hint"])
        self.assertTrue(self.do(w.exclude_add, "Personal"))
        self.assertEqual(self._rows()[-1], ("Window title", "Personal", "", "Added on this PC"))
        excel = next(i for i, r in enumerate(self._rows()) if r[1] == "EXCEL.EXE")
        self.do(w.exclude_remove, [excel])
        kept = st.Settings(self.tmp).exclude_policy()
        self.assertIsNone(kept.process("EXCEL.EXE"))
        self.assertIsNotNone(kept.title("Personal budget"))
        self.do(w.exclude_defaults)
        self.assertIsNotNone(st.Settings(self.tmp).exclude_policy().process("EXCEL.EXE"))
        with mock.patch.object(exclude, "running_programs", lambda: ["CaseDesk.exe", "Slack.exe", "WINWORD.EXE"]):
            self.do(w.exclude_pick)
        dialog = self.snap()["dialog"]
        self.assertEqual(dialog["items"], ["CaseDesk.exe", "Slack.exe"])  # Word is excluded already
        self.do(w.answer, dialog["id"], True, ["Slack.exe", "not-offered.exe"])
        self.assertIsNotNone(st.Settings(self.tmp).exclude_policy().process("slack.exe"))
        self.assertIsNone(st.Settings(self.tmp).exclude_policy().process("not-offered.exe"))
        conf = self.do(w.options)
        self.assertIsNotNone(exclude.Policy.from_session(conf["exclude"]).process("slack.exe"))

    def test_an_office_policy_locks_its_entries_in_the_window(self):
        self._no_office_policy({"exclude": [{"kind": "web", "pattern": "mail.google.com", "name": "Gmail"}]})
        w = self.window()
        self.assertEqual(self._rows()[0], ("Web site", "mail.google.com", "Gmail", "Office policy (locked)"))
        self.assertTrue(self.snap()["exclude"]["rows"][0]["locked"])
        self.assertIn("set by your office's policy", self.snap()["exclude"]["note"])
        self.do(w.exclude_remove, [0])
        self.assertEqual(self._rows()[0][1], "mail.google.com")  # still there
        self._no_office_policy({"exclude": [], "allow_user_changes": False})
        self.do(w.refresh_exclude)
        self.assertFalse(self.snap()["exclude"]["can_edit"])
        self.assertFalse(self.do(w.exclude_add, "Personal"))
        self.assertIn("cannot be changed here", self.snap()["exclude"]["note"])

    def test_buttons_are_enabled_only_when_they_can_act(self):
        w = self.window()
        can = self.snap()["can"]
        for name in ("start_offered", "send", "details", "upload"):
            self.assertFalse(can[name], name)
        self.assertTrue(can["start_own"])
        self.assertIn("Pair with the portal", self.snap()["offered"]["note"])
        self.assertIn("Not paired", self.snap()["recordings"]["note"])
        self.assertIn("once this PC is paired", self.snap()["own"]["empty"])
        self.do(w.apply, {"sessions": [S1], "deny_keys": []}, NO_APPS, None)
        self.assertFalse(self.snap()["can"]["start_offered"])
        self.do(w.select_offered, 1)
        self.assertTrue(self.snap()["can"]["start_offered"])
        self.assertEqual(self.snap()["own"]["empty"], "The portal's catalog has no application yet.")
        app = {"app_id": 4, "name": "Billing Desk", "target": {"exe": "Billing.exe"}}
        self.do(w.apply, {"sessions": [S1], "deny_keys": []}, {"apps": [app], "deny_keys": []}, None)
        own = self.snap()["own"]
        self.assertEqual([(a["name"], a["surface"]) for a in own["apps"]], [("Billing Desk", "desktop")])
        self.assertIsNone(own["empty"])
        self._recorded()
        self.do(w.refresh_done)
        rows = self.snap()["recordings"]["rows"]
        self.assertEqual(rows[0]["row"], "Month-end close  ·  finished  ·  not sent yet")
        self.assertEqual((rows[0]["state"], rows[0]["sent"]), ("Finished", False))
        self.do(w.select_recording, SESSION)
        self.assertTrue(self.snap()["can"]["details"])
        self.assertFalse(self.snap()["can"]["send"])  # not paired: nowhere to send
        self.cfg.paired("http://127.0.0.1:9", 3, "Workstation 7", "tok")
        self.do(w.refresh_status)
        self.assertTrue(self.snap()["can"]["send"])
        self.assertTrue(self.snap()["pairing"]["paired"])

    def test_settings_are_kept_and_go_with_the_next_session(self):
        w = self.window()
        self.do(w.set_settings, {"browser": "chrome", "pictures": True, "cdp": " http://127.0.0.1:9222 "})
        again = st.Settings(self.tmp)
        self.assertEqual((again.values["browser"], again.values["pictures"], again.values["cdp"]),
                         ("chrome", True, "http://127.0.0.1:9222"))  # fmt: skip
        self.do(w.set_settings, {"browser": "firefox"})
        self.assertEqual(self.snap()["settings"]["browser"], "edge")
        opts = self.do(w.options)
        self.assertEqual((opts["cdp"], opts["pictures"]), ("http://127.0.0.1:9222", True))

    def test_the_bridge_answers_only_when_something_changed(self):
        w = self.window()
        bridge = Bridge(w, self.loop, self.host)
        first = bridge.snapshot(-1)
        for _ in range(100):  # the upload worker's first report may still come in
            time.sleep(0.05)
            again = bridge.snapshot(first["version"])
            if again is None:
                break
            first = again
        self.assertIsNone(bridge.snapshot(first["version"]))
        bridge.nav("privacy")
        self.assertEqual(bridge.snapshot(first["version"])["page"], "privacy")
        bridge.nav("nowhere")
        self.assertEqual(self.snap()["page"], "privacy")
        json.dumps(self.snap())  # everything the page gets is JSON


if __name__ == "__main__":
    unittest.main()
