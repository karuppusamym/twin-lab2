"""End to end on this PC: the portal, the recorder, a real Edge and the real CaseDesk.

The portal runs on 127.0.0.1. A person makes a pairing code and offers a session that targets
a web application from the catalog; the recorder pairs, picks the session up, records it with
CaseDesk added as its own target (not in the catalog), and sends the references. The portal
then shows the recording mapped to the web application and to CaseDesk, registered as
discovered, with their flows, and nothing of the applications that were not targets.

It takes the foreground and clicks in CaseDesk, so it runs only when asked:

    python -m agent.twinlab fetch        the pinned collectors (twinlab.lock.json)
    set TWINLAB_AGENT_E2E=1
    set TWINLAB_REFERENCE=<a twin-lab checkout>   only for the CaseDesk reference application
    python -m pytest -q tests/test_agent_e2e.py   (from twin-lab2)

Leave the PC alone while it runs: a click or a window of yours in the foreground can still
make the flow refuse to click, and then the session's note says why.
"""

import contextlib
import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from werkzeug.serving import make_server

from agent import main as agent_main
from agent import settings as st
from portal import create_app
from support import new_database

PAGE = b"""<!DOCTYPE html><html><head><title>Orders</title></head><body>
<form id="f" onsubmit="event.preventDefault(); fetch('/api/find?q=' + q.value).then(r => r.json())
  .then(d => { out.textContent = d.found; })">
<input id="q" name="q"><button id="go" type="submit">Find</button></form><p id="out">none</p></body></html>"""


class Site(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body, ctype = (b'{"found": 3}', "application/json") if self.path.startswith("/api/") else (PAGE, "text/html")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def to_front(win, pids: set[int], timeout: float = 5.0) -> None:
    """Put CaseDesk in the foreground before the flow clicks in it.

    Windows lets a process take the foreground only if it sent the last input event (or already
    owns the foreground). This test runs in the background and the web part has just brought Edge
    to the front, so the Driver's own SetForegroundWindow is refused, its first click raises
    DriveError, and the session keeps only the steps typed before it. A relative mouse move of
    zero pixels is real input from this process (it moves nothing, and the recorder hears no click
    in it), so the next SetForegroundWindow is granted."""
    import ctypes
    from ctypes import wintypes

    class MI(ctypes.Structure):
        _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_size_t)]  # fmt: skip

    class INPUT(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [("mi", MI), ("pad", ctypes.c_byte * 32)]

        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        fg = win.foreground()
        if fg and win.window_pid(fg) in pids:
            return
        top = win.top_windows(pids)
        move = INPUT(type=0)
        move.mi = MI(0, 0, 0, 0x0001, 0, 0)  # MOUSEEVENTF_MOVE, relative, 0 x 0
        win.user32.SendInput(1, ctypes.byref(move), ctypes.sizeof(INPUT))
        if top:
            win.user32.SetForegroundWindow(top[0])
        time.sleep(0.3)
    raise AssertionError("CaseDesk could not be brought to the foreground; not driving it")


@contextlib.contextmanager
def patient_driver(pids: set[int]):
    """The twin-lab Driver (4385949) with two lookups that wait instead of looking once.

    - choose expands the combo box, sleeps 0.4 s and looks for the item once; a CaseDesk slow to
      fill its drop-down list raises "item 'Resolved' found 0 time(s)" and the flow stops before
      Save. Here that lookup polls up to 3 s while it finds nothing.
    - click and click_menu refuse when the window under the point is another process's (a toast,
      a window still closing) and raise "covered by another window". Here the check waits up to
      3 s for CaseDesk to be on top there; it still never clicks another application.
    Nothing else changes: the same inputs, so the same steps recorded."""
    from labctl.desktop import drive, win

    original, window_at = drive.Driver.choose, win.window_at

    def uncovered(x, y):
        end = time.monotonic() + 3.0
        hwnd = window_at(x, y)
        while win.window_pid(hwnd) not in pids and time.monotonic() < end:
            print(f"\n({x}, {y}) covered by {win.process_name(win.window_pid(hwnd))} "
                  f"{win.class_name(hwnd)!r}; waiting", file=sys.stderr)  # fmt: skip
            time.sleep(0.3)
            hwnd = window_at(x, y)
        return hwnd

    def choose(self, window, selector, item):
        find_all = self.win.find_all

        def waiting(el, sel):
            end = time.monotonic() + 3.0
            found = find_all(el, sel)
            while not found and time.monotonic() < end:
                time.sleep(0.2)
                found = find_all(el, sel)
            return found

        with mock.patch.object(self.win, "find_all", waiting):
            original(self, window, selector, item)

    with mock.patch.object(drive.Driver, "choose", choose), mock.patch.object(win, "window_at", uncovered):
        yield


@unittest.skipUnless(sys.platform == "win32" and os.environ.get("TWINLAB_AGENT_E2E") == "1",
                     "starts Edge and CaseDesk and clicks: set TWINLAB_AGENT_E2E=1")
class EndToEndTest(unittest.TestCase):
    def test_pair_offer_record_send_and_see_it_mapped(self):
        agent_main._labctl()
        twinlab = Path(os.environ.get("TWINLAB_REFERENCE") or Path(__file__).resolve().parents[2] / "twin-lab")
        casedesk = twinlab / "reference" / "desktop" / "CaseDesk"
        spec = importlib.util.spec_from_file_location("casedesk_flow", casedesk / "flow.py")
        flow = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(flow)
        exe = flow.build()

        work = Path(tempfile.mkdtemp(prefix="tl2e2e-"))
        site = ThreadingHTTPServer(("127.0.0.1", 0), Site)
        threading.Thread(target=site.serve_forever, daemon=True).start()
        site_url = f"http://127.0.0.1:{site.server_address[1]}"
        app = create_app(new_database(work))
        server = make_server("127.0.0.1", 0, app, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        portal = f"http://127.0.0.1:{server.server_port}"
        ui = app.test_client()
        ui.get("/")
        with ui.session_transaction() as session:
            token = session["form_token"]
        try:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(work / "local")}):
                # in the portal: a pairing code, an application, an automation, a session
                ui.post("/machines/pair", data={"token": token})
                code = re.search(r'id="pair-code">([A-Z0-9-]+)<', ui.get("/machines").data.decode()).group(1)
                self.assertEqual(agent_main.main(["pair", "--portal", portal, "--code", code, "--name", "Test PC"]), 0)
                ui.post("/apps", data={"token": token, "name": "Orders web"})
                ui.post("/apps/1/identity", data={"token": token, "key": site_url})
                ui.post("/automations", data={"token": token, "name": "UiPath orders", "kind": "uipath", "app": "1"})
                ui.post("/sessions", data={"token": token, "name": "Orders trial", "machine_id": "1",
                                           "automation_id": "1", "app": "1", "level_1": "full"})
                # on the PC: the offered session, CaseDesk added as its own target
                cfg = st.Settings(st.data_dir())
                offered = agent_main._portal(cfg, None).sessions()
                s = offered["sessions"][0]
                targets = s["targets"] + [{"exe": "CaseDesk.exe", "start": str(exe)}]
                conf = agent_main.session_config(targets, offered["deny_keys"], s["name"], s["id"],
                                                 s["automation_id"], minutes=5)

                def script(session):
                    from labctl.desktop import win
                    from playwright.sync_api import sync_playwright

                    end = time.monotonic() + 30
                    while session.cdp is None and time.monotonic() < end:
                        time.sleep(0.2)
                    time.sleep(2.0)
                    with sync_playwright() as pw:
                        page = pw.chromium.connect_over_cdp(session.cdp).contexts[0].pages[0]
                        page.bring_to_front()
                        page.wait_for_selector("#q")
                        page.fill("#q", "C-7")
                        page.click("#go")
                        page.wait_for_function("document.querySelector('#out').textContent === '3'")
                    notepad = subprocess.Popen(["notepad.exe"])
                    time.sleep(2.0)
                    subprocess.run(["taskkill", "/F", "/IM", "notepad.exe"], capture_output=True)
                    notepad.wait(5)
                    pids, end = set(), time.monotonic() + 20
                    while not pids and time.monotonic() < end:
                        pids = {p for p in win.pids_by_name("CaseDesk.exe") if win.top_windows({p})}
                        time.sleep(0.3)
                    time.sleep(1.0)
                    to_front(win, pids)
                    with patient_driver(pids):
                        flow.flow(pids)

                folder = agent_main.run_session(cfg, conf, script=script)
                # a failing script only leaves a note in the session; say it rather than a missing step
                notes = json.loads((folder / "session.json").read_text(encoding="utf-8"))["notes"]
                self.assertFalse([n for n in notes if "script failed" in n], notes)
                result = agent_main.send(cfg, folder.name)
            # in the portal: the recording, mapped
            recording = ui.get(f"/recordings/{result['recording_id']}").data.decode()
            self.assertIn("Orders web", recording)
            self.assertIn("CaseDesk", recording)
            self.assertIn("discovered", recording)
            self.assertIn("Other applications", recording)
            self.assertIn("UiPath orders", recording)
            for leak in ("otepad", "C-7", "tester"):
                self.assertNotIn(leak, recording)
            web_app = ui.get("/apps/1").data.decode()
            self.assertIn("UiPath orders", web_app)
            self.assertIn("<td>go</td>", web_app)  # the web flow: button#go
            self.assertIn("<td>q</td>", web_app)  # and input#q, never what was typed
            desk = next(a for a in json.loads(ui.get("/api/agent/apps", headers={
                "Authorization": f"Bearer {st.Settings(work / 'local' / st.APP).token()}"}).data)["apps"]
                if a["name"] == "CaseDesk")  # fmt: skip
            desk_page = ui.get(f"/apps/{desk['app_id']}").data.decode()
            self.assertIn("btnSave", desk_page)
            self.assertIn("UiPath orders", desk_page)
            self.assertIn("recorded", ui.get("/sessions").data.decode())
            print(f"\nE2E session folder: {folder}\nportal recording: {result['recording_id']}")
        finally:
            server.shutdown()
            site.shutdown()


if __name__ == "__main__":
    unittest.main()
