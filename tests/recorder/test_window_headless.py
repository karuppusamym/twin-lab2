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


if __name__ == "__main__":
    unittest.main()
