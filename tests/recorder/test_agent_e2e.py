"""End to end on this PC: the portal, the recorder, a real Edge and the real CaseDesk.

The portal runs on 127.0.0.1. A person makes a pairing code and offers a session that targets
a web application from the catalog; the recorder pairs, picks the session up, records it with
CaseDesk added as its own target (not in the catalog), and sends the references. The portal
then shows the recording mapped to the web application and to CaseDesk, registered as
discovered, with their flows, and nothing of the applications that were not targets.

It takes the foreground and clicks in CaseDesk, so it runs only when asked:

    set TWINLAB_AGENT_E2E=1
    set TWINLAB_REFERENCE=<a twin-lab checkout>   only for CaseDesk's sources (default ../twin-lab)
    python -m pytest -q tests/recorder/test_agent_e2e.py   (from twin-lab2)

CaseDesk (a WinForms app built with dotnet) is an external fixture from twin-lab's
reference/desktop/CaseDesk; its steps are driven here with the copied desktop Driver
(desktop_drive.py).

Leave the PC alone while it runs: a click or a window of yours in the foreground can still
make the flow refuse to click, and then the session's note says why.
"""

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

from twinlab2_recorder import main as agent_main
from twinlab2_recorder import settings as st
from twinlab2.portal import create_app
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


def casedesk_flow(flow, pids: set[int], pause: float = 0.6) -> None:
    """The steps of CaseDesk's own flow (twin-lab reference/desktop/CaseDesk/flow.py at 90bdd91),
    driven with the copied desktop Driver: sign in (a wrong password first), search, refresh,
    open a case, change it, save twice, exit."""
    from desktop_drive import Driver

    d = Driver(pids, pause)
    w = d.window("uia:AutomationId=LoginForm;ControlType=Window")
    d.type(w, "uia:AutomationId=txtUser;ControlType=Edit", flow.USER)
    d.type(w, "uia:AutomationId=txtPassword;ControlType=Edit", flow.BAD_PASSWORD)
    d.click(w, "uia:AutomationId=btnSignIn;ControlType=Button")
    box = d.window("uia:Name=Sign in;ControlType=Window;ClassName=#32770")
    d.click(box, "uia:Name=OK;ControlType=Button")
    d.type(w, "uia:AutomationId=txtPassword;ControlType=Edit", flow.PASSWORD)
    d.click(w, "uia:AutomationId=btnSignIn;ControlType=Button")

    main = d.window("uia:AutomationId=MainForm;ControlType=Window")
    d.type(main, "uia:AutomationId=txtSearch;ControlType=Edit", "C-100")
    if not d.key("Enter"):  # Enter presses Search (the form's AcceptButton)
        d.click(main, "uia:AutomationId=btnSearch;ControlType=Button")
    d.click_menu(main, "Case", "Refresh")
    d.click(main, "uia:AutomationId=btnOpen;ControlType=Button")

    case = d.window("uia:AutomationId=CaseDetailForm;ControlType=Window")
    d.type(case, "uia:AutomationId=txtSummary;ControlType=Edit", "Done")
    d.choose(case, "uia:AutomationId=cboStatus;ControlType=ComboBox", "Resolved")
    d.click(case, "uia:AutomationId=chkUrgent;ControlType=CheckBox")
    d.click(case, "uia:AutomationId=btnSave;ControlType=Button")
    box = d.window("uia:Name=Case;ControlType=Window;ClassName=#32770")
    d.click(box, "uia:Name=OK;ControlType=Button")
    d.type(case, "uia:AutomationId=txtSummary;ControlType=Edit", "Card resent by courier")
    d.click(case, "uia:AutomationId=btnSave;ControlType=Button")

    d.click_menu(main, "File", "Exit")


@unittest.skipUnless(sys.platform == "win32" and os.environ.get("TWINLAB_AGENT_E2E") == "1",
                     "starts Edge and CaseDesk and clicks: set TWINLAB_AGENT_E2E=1")
class EndToEndTest(unittest.TestCase):
    def test_pair_offer_record_send_and_see_it_mapped(self):
        twinlab = Path(os.environ.get("TWINLAB_REFERENCE") or Path(__file__).resolve().parents[3] / "twin-lab")
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
                    from twinlab2_recorder.desktop import win
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
                    casedesk_flow(flow, pids)  # the Driver takes the foreground and waits itself

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
