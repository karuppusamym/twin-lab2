"""The recorder's real window code (main.build_window) on a Tk root that is never shown.

The Tk root and each test's window (a Toplevel) are withdrawn before Tk processes a single event,
so they are never mapped (each step checks `winfo_ismapped()`); dialogs are replaced. Opt in, on a PC with Tk:

    set TWINLAB_TK_HEADLESS=1
    python -m pytest -q tests/recorder/test_window_headless.py

What needs the visible window is in recorder/GO-DESKTOP-CHECKLIST.md.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import gc
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

from twinlab2_recorder import main as agent_main
from twinlab2_recorder import settings as st
from twinlab2_recorder import uithread

try:
    import tkinter as tk
    from tkinter import messagebox, simpledialog
except ImportError:  # pragma: no cover - a Python without Tk
    tk = None

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


@unittest.skipUnless(tk is not None and os.environ.get("TWINLAB_TK_HEADLESS") == "1",
                     "set TWINLAB_TK_HEADLESS=1 to run the window code on a hidden Tk root")  # fmt: skip
class HiddenWindowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tk_root = tk.Tk()  # one Tcl interpreter for the class: each test's window is a Toplevel
        cls.tk_root.withdraw()  # before any event is processed: never mapped

    @classmethod
    def tearDownClass(cls):
        cls.tk_root.destroy()

    def setUp(self):
        self.root = tk.Toplevel(self.tk_root)
        self.root.withdraw()
        self.addCleanup(self._destroy)
        self.addCleanup(uithread.clear_ui_thread)
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = st.Settings(self.tmp)
        self.cfg.portable = False
        self.dialogs = []
        for name in ("showinfo", "showerror", "showwarning"):
            patcher = mock.patch.object(messagebox, name, lambda *a, _n=name, **k: self.dialogs.append((_n, a[1])))
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(messagebox, "askyesno", lambda *a, **k: True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _destroy(self):
        w = getattr(self, "w", None)
        if w is not None and not w.state.get("destroyed"):
            w.destroy()  # stops its poller, upload worker and jobs
        try:
            self.root.destroy()
        except tk.TclError:
            pass
        # The window's closures form reference cycles holding Tk variables; collect them here, on
        # Tk's thread: collected on a worker thread, a variable's __del__ calls into Tcl from it.
        self.w = None
        gc.collect()

    def window(self):
        w = self.w = agent_main.build_window(self.cfg, None, self.root)
        self.assertFalse(self.root.winfo_ismapped())
        return w

    def pump(self, until, timeout=10):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                self.tk_root.update()
                self.assertFalse(self.tk_root.winfo_ismapped())
                self.assertFalse(self.root.winfo_ismapped())
            except tk.TclError:  # the window closed itself
                return True
            if until():
                return True
            time.sleep(0.02)
        return False

    def test_an_offline_portal_shows_in_the_status(self):
        self.cfg.paired("http://127.0.0.1:9", 3, "Workstation 7", "tok")
        w = self.window()
        w.poll_portal()
        self.assertTrue(self.pump(lambda: "cannot be reached" in w.status.get()), w.status.get())
        self.assertTrue(w.status.get().startswith("Paired as Workstation 7: the portal cannot be reached"))

    def test_pairing_and_sending_keep_the_window_responsive(self):
        portal = SlowPortal(delay=1.0)
        self.addCleanup(portal.stop)
        w = self.window()
        answers = iter([portal.url, "123-456", "Workstation 7"])
        with mock.patch.object(simpledialog, "askstring", lambda *a, **k: next(answers)):
            began = time.monotonic()
            w.on_pair()
            self.assertLess(time.monotonic() - began, 0.3)  # the portal takes 1 s: not on this thread
        self.assertTrue(self.pump(lambda: w.activity.get().startswith("Connecting: pairing with")))
        self.assertTrue(self.pump(lambda: self.cfg.values.get("token")))
        self.assertIn("Paired as Workstation 7", w.status.get())

        folder = self.cfg.recordings / SESSION
        folder.mkdir(parents=True)
        shutil.copy(FIXTURES / "references-ten-apps.json", folder / "portal-outbox.json")
        (folder / "session.json").write_text(json.dumps({"name": "Month-end close", "state": "stopped"}),
                                             encoding="utf-8")  # fmt: skip
        w.refresh_done()
        w.done_list.selection_set(0)
        w.show_done()
        summary = w.review.get("1.0", "end")
        self.assertIn(f"Destination: {portal.url}", summary)
        self.assertNotIn('"flows"', summary)
        w.on_details()
        self.assertIn('"flows"', w.review.get("1.0", "end"))
        began = time.monotonic()
        w.on_send()
        self.assertLess(time.monotonic() - began, 0.3)
        self.assertTrue(self.pump(lambda: w.activity.get().startswith("Sending: references of")))
        self.assertTrue(self.pump(lambda: any("Sent." in d for _, d in self.dialogs)), (self.dialogs, w.activity.get()))
        self.assertIn("/api/agent/references", portal.got)
        self.assertTrue((folder / "sent.json").is_file())

    def test_offered_selection_survives_a_poll(self):
        w = self.window()
        s1 = {"id": 1, "name": "Billing", "automation_id": None, "targets": [{"name": "ERP", "level": "full"}]}
        s2 = {"id": 2, "name": "Orders", "automation_id": None, "targets": [{"name": "Shop", "level": "full"}]}
        w.apply({"sessions": [s1, s2], "deny_keys": []}, {"apps": [], "deny_keys": []}, None)
        w.offered_list.selection_set(1)
        w.apply({"sessions": [dict(s2, name="Orders (EU)")], "deny_keys": []}, {"apps": [], "deny_keys": []}, None)
        self.assertEqual(w.offered_list.curselection(), (0,))
        self.assertEqual(w.offered_list.get(0), "Orders (EU)  ·  Shop")

    def _recording(self, w, seconds):
        folder = self.cfg.recordings / SESSION
        folder.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        w.state.update(proc=proc, session=SESSION)
        return proc

    def test_closing_during_a_recording_waits_off_the_window_thread(self):
        w = self.window()
        proc = self._recording(w, 1.5)
        began = time.monotonic()
        w.on_close()
        self.assertLess(time.monotonic() - began, 0.3)
        self.assertTrue((self.cfg.recordings / SESSION / "stop").is_file())
        self.assertTrue(self.pump(lambda: w.activity.get().startswith("Stopping: finishing the recording")))
        w.on_close()  # pressed again: told to wait, not a second stop
        self.assertIn("Still finishing", w.activity.get())
        self.assertTrue(self.pump(lambda: w.state.get("destroyed")))
        self.assertIsNotNone(proc.poll())
        self.assertEqual([d for d in self.dialogs if d[0] == "showwarning"], [])

    def test_a_recording_that_does_not_finish_in_time_is_explained(self):
        w = self.window()
        self._recording(w, 30)
        with mock.patch.object(agent_main, "STOP_WAIT_S", 0.5):
            w.on_close()
            self.assertTrue(self.pump(lambda: w.state.get("destroyed")))
        warnings = [d for _, d in self.dialogs if "did not finish" in d]
        self.assertEqual(len(warnings), 1, self.dialogs)

    # -- the exclude list and the window's states ---------------------------------------------------

    def _no_office_policy(self, doc=None):
        path = self.tmp / "policy.json"
        if doc is not None:
            path.write_text(json.dumps(doc), encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {"TWINLAB_RECORDER_POLICY": str(path)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _rows(self, w):
        return [w.ex_table.item(i, "values") for i in w.ex_table.get_children()]

    def test_the_exclude_list_is_edited_in_the_window_and_kept(self):
        from twinlab2_recorder.workstation import exclude

        self._no_office_policy()
        w = self.window()
        self.assertEqual(len(self._rows(w)), len(exclude.DEFAULTS))
        self.assertIn(("Program", "WINWORD.EXE", "Microsoft Word", "Default"), self._rows(w))
        self.assertEqual(str(w.ex_remove_btn.cget("state")), "disabled")  # nothing selected
        w.ex_kind.set("Window title")
        w.exclude_kind_changed()
        self.assertIn("* - Excel", w.ex_hint.get())
        w.ex_pattern.set("Personal")
        w.exclude_add()
        self.assertEqual(self._rows(w)[-1], ("Window title", "Personal", "", "Added on this PC"))
        self.assertEqual(w.ex_pattern.get(), "")
        excel = next(i for i in w.ex_table.get_children() if w.ex_table.item(i, "values")[1] == "EXCEL.EXE")
        w.ex_table.selection_set(excel)
        w.exclude_selection()
        self.assertEqual(str(w.ex_remove_btn.cget("state")), "normal")
        w.exclude_remove()
        kept = st.Settings(self.tmp).exclude_policy()
        self.assertIsNone(kept.process("EXCEL.EXE"))
        self.assertIsNotNone(kept.title("Personal budget"))
        w.exclude_defaults()
        self.assertIsNotNone(st.Settings(self.tmp).exclude_policy().process("EXCEL.EXE"))
        with mock.patch.object(exclude, "running_programs", lambda: ["CaseDesk.exe", "Slack.exe", "WINWORD.EXE"]),                 mock.patch.object(agent_main, "choose_from_list", lambda _r, _t, _p, items: self.offered.extend(items)
                                  or ["Slack.exe"]):  # fmt: skip
            self.offered = []
            w.exclude_pick()
        self.assertEqual(self.offered, ["CaseDesk.exe", "Slack.exe"])  # Word is excluded already
        self.assertIsNotNone(st.Settings(self.tmp).exclude_policy().process("slack.exe"))
        conf = w.options()
        self.assertIsNotNone(exclude.Policy.from_session(conf["exclude"]).process("slack.exe"))

    def test_an_office_policy_locks_its_entries_in_the_window(self):
        self._no_office_policy({"exclude": [{"kind": "web", "pattern": "mail.google.com", "name": "Gmail"}]})
        w = self.window()
        self.assertEqual(self._rows(w)[0], ("Web site", "mail.google.com", "Gmail", "Office policy (locked)"))
        self.assertIn("set by your office's policy", w.ex_note.get())
        w.ex_table.selection_set(w.ex_table.get_children()[0])
        w.exclude_selection()
        self.assertEqual(str(w.ex_remove_btn.cget("state")), "disabled")
        self._no_office_policy({"exclude": [], "allow_user_changes": False})
        w.refresh_exclude()
        self.assertEqual(str(w.ex_add_btn.cget("state")), "disabled")
        self.assertIn("cannot be changed here", w.ex_note.get())

    def test_buttons_are_enabled_only_when_they_can_act(self):
        self._no_office_policy()
        w = self.window()
        for b in (w.start_offered, w.send_btn, w.details_btn, w.upload_btn, w.retry_btn):
            self.assertEqual(str(b.cget("state")), "disabled", b.cget("text"))
        self.assertEqual(str(w.start_own.cget("state")), "normal")
        self.assertIn("Pair with the portal", w.offered_note.get())
        self.assertIn("Not paired", w.done_note.get())
        s1 = {"id": 1, "name": "Billing", "automation_id": None, "targets": [{"name": "ERP", "level": "full"}]}
        w.apply({"sessions": [s1], "deny_keys": []}, {"apps": [], "deny_keys": []}, None)
        self.assertEqual(str(w.start_offered.cget("state")), "disabled")
        w.offered_list.selection_set(0)
        w.refresh_buttons()
        self.assertEqual(str(w.start_offered.cget("state")), "normal")
        self.assertEqual(w.own_empty.cget("text"), "The portal's catalog has no application yet.")
        app = {"app_id": 4, "name": "Billing Desk", "target": {"exe": "Billing.exe"}}
        w.apply({"sessions": [s1], "deny_keys": []}, {"apps": [app], "deny_keys": []}, None)
        self.assertEqual([a["name"] for _, a, _ in w.own_vars], ["Billing Desk"])
        self.assertEqual(w.own_empty.winfo_manager(), "")  # the empty note is gone, not destroyed
        folder = self.cfg.recordings / SESSION
        folder.mkdir(parents=True)
        shutil.copy(FIXTURES / "references-ten-apps.json", folder / "portal-outbox.json")
        (folder / "session.json").write_text(json.dumps({"name": "Month-end close", "state": "stopped"}),
                                             encoding="utf-8")  # fmt: skip
        w.refresh_done()
        self.assertEqual(w.done_list.get(0), "Month-end close  ·  finished  ·  not sent yet")
        w.done_list.selection_set(0)
        w.show_done()
        self.assertEqual(str(w.details_btn.cget("state")), "normal")
        self.assertEqual(str(w.send_btn.cget("state")), "disabled")  # not paired: nowhere to send
        self.cfg.paired("http://127.0.0.1:9", 3, "Workstation 7", "tok")
        w.refresh_status()
        self.assertEqual(str(w.send_btn.cget("state")), "normal")
        self.assertEqual(w.pair_btn.cget("text"), "Pair again…")

    def test_updates_follow_the_policy_and_never_install_during_a_recording(self):
        from twinlab2_recorder import policy, updater

        work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, work, True)
        (work / "policy.json").write_text(json.dumps({"updates": "off"}), encoding="utf-8")
        for patcher in (mock.patch.dict(os.environ, {"TWINLAB_RECORDER_POLICY": str(work / "policy.json")}),
                        mock.patch.object(policy, "read_registry", return_value={})):  # fmt: skip
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cfg.paired("http://127.0.0.1:9", 3, "Workstation 7", "tok")
        w = self.window()
        w.check_updates()
        self.assertIn("managed by your office", w.update_note.get())
        self.assertEqual(str(w.check_update_btn.cget("state")), "disabled")
        self.assertIn("Managed by this PC's policy (updates off)", w.network_note.get())

        (work / "policy.json").write_text(json.dumps({"updates": "notify"}), encoding="utf-8")
        offer = updater.Offer("9.0.0", "TwinLabRecorder-9.0.0-x64.msi", "/api/agent/update/x", 3, "a" * 64)
        with mock.patch.object(updater, "check", return_value=offer):
            w.check_updates()
            self.assertTrue(self.pump(lambda: str(w.install_update_btn.cget("state")) == "normal"))
        self.assertIn("9.0.0 is available", w.update_note.get())
        # a prepared update waits while a session records, then installs (and the window closes)
        msi = self.tmp / "TwinLabRecorder-9.0.0-x64.msi"
        msi.write_bytes(b"x")
        w.state["update_ready"] = (offer, msi, True)
        w.state["proc"] = object()  # recording
        with mock.patch.object(updater, "install") as install:
            w.install_when_idle()
            install.assert_not_called()
            w.state["proc"] = None
            w.install_when_idle()
            install.assert_called_once()
        self.assertTrue(w.state.get("destroyed"))


if __name__ == "__main__":
    unittest.main()
