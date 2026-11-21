"""An attach-mode session (a robot's browser over CDP) is recorded and sent to a portal on this
machine: its manifest stays within the recorder contract (gap 1 of the rebuild proof: the attach
note was 351 characters where the contract allows 300, so `send` refused every attached
recording). Headless Chromium (--headless=new); skips without Playwright's Chromium."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import web_harness as wh

from twinlab2_contract import recorder_contract as contract
from twinlab2_recorder import main as agent_main
from twinlab2_recorder import settings as st
from twinlab2_recorder.workstation import completeness

EXE = wh.chromium_path()

PAGE = b"""<!DOCTYPE html><html><head><title>Orders</title></head><body>
<label for="q">Search</label><input id="q" name="q"><button id="go" type="button"
onclick="document.getElementById('out').textContent='found'">Find</button><p id="out">none</p></body></html>"""


class Fit(unittest.TestCase):
    def test_notes_and_reasons_are_cut_to_the_contract(self):
        long = "word " * 120
        block = completeness._fit_channel({"status": "complete", "notes": [long] * 60,
                                           "gaps": [{"from": "a", "to": "b", "reason": long}]})  # fmt: skip
        self.assertEqual(len(block["notes"]), 50)
        self.assertTrue(all(len(n) <= 300 for n in block["notes"]))
        self.assertTrue(block["notes"][0].endswith("..."))
        self.assertIn("more note(s)", block["notes"][-1])
        self.assertLessEqual(len(block["gaps"][0]["reason"]), 200)
        self.assertEqual(completeness.fit("short note", 300), "short note")

    def test_the_attach_note_fits(self):
        from twinlab2_recorder.workstation import supervisor

        self.assertLessEqual(len(supervisor.CDP_WARNING), 300)


@unittest.skipUnless(EXE, "Playwright's Chromium is not installed")
class AttachSend(unittest.TestCase):
    def test_an_attached_recording_is_sent(self):
        from playwright.sync_api import sync_playwright
        from twinlab2.portal import create_app
        from support import Client, Server, new_database

        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        root = Path(work.name)
        app = create_app(new_database(root))
        server = Server(app)
        self.addCleanup(server.stop)
        ui = Client(app)
        env = mock.patch.dict(os.environ, {"LOCALAPPDATA": str(root / "local")})
        env.start()
        self.addCleanup(env.stop)
        code = ui.post("/api/v1/machines/pairing-codes").json()["display"]
        self.assertEqual(agent_main.main(["pair", "--portal", server.url, "--code", code, "--name", "PC"]), 0)
        with wh.site({"/": PAGE}) as base, wh.headless_chromium(EXE) as cdp:
            ui.post("/api/v1/apps", json={"name": "Orders"})
            ui.post("/api/v1/apps/1/identities", json={"key": base})
            ui.post("/api/v1/capture-sessions", json={"name": "Attached", "machine_id": 1,
                                                      "targets": [{"app_id": 1, "level": "full"}]})  # fmt: skip
            cfg = st.Settings(st.data_dir())
            offered = agent_main._portal(cfg, None).sessions()["sessions"][0]
            conf = agent_main.session_config(offered["targets"], [], offered["name"], offered["id"],
                                             minutes=3, cdp=cdp, robot=False)  # fmt: skip

            def script(session):
                with sync_playwright() as pw:
                    page = pw.chromium.connect_over_cdp(cdp).contexts[0].pages[0]
                    page.goto(base + "/")
                    page.wait_for_timeout(800)
                    page.click("#q")
                    page.keyboard.type("C-7", delay=30)
                    page.click("#go")
                    page.wait_for_timeout(1200)

            folder = agent_main.run_session(cfg, dict(conf, override_preflight=True), script=script)
        box = json.loads((folder / "portal-outbox.json").read_text(encoding="utf-8"))
        notes = box["completeness"]["channels"]["web"]["notes"]
        self.assertTrue(any("over CDP" in n for n in notes), notes)
        self.assertEqual(contract.errors(agent_main.contract_references(box), "references"), [])
        result = agent_main.send(cfg, folder.name)
        self.assertTrue(result.get("recording_id"))
        self.assertTrue((folder / "sent.json").is_file())


if __name__ == "__main__":
    unittest.main()
