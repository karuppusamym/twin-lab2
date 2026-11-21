import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from twinlab2_recorder import settings as st
from twinlab2_recorder.client import Portal, PortalError, check_portal, clean_targets
from twinlab2_recorder import main as agent_main
from twinlab2.portal import create_app
from test_registry import references
from support import Client, Server, new_database


class AgentUnitTest(unittest.TestCase):
    def test_portal_address_must_be_https_except_on_this_machine(self):
        self.assertEqual(check_portal("https://twinlab2.apps.example.test/"), "https://twinlab2.apps.example.test")
        self.assertEqual(check_portal("http://127.0.0.1:8765"), "http://127.0.0.1:8765")
        for bad in ("http://twinlab2.example.test", "ftp://x", "https://u:p@x.test", "https://x.test/?a=1", ""):
            with self.assertRaises(PortalError, msg=bad):
                check_portal(bad)

    def test_targets_from_the_portal_are_data_in_a_fixed_shape(self):
        got = clean_targets([{"exe": "CaseDesk.exe", "level": "steps", "app_id": 3, "run": "calc.exe"},
                             {"level": "full"}, "x", {"origin": "https://a.test", "level": "admin"}])
        self.assertEqual(got, [{"level": "steps", "exe": "CaseDesk.exe", "app_id": 3},
                               {"level": "full", "origin": "https://a.test"}])

    @unittest.skipUnless(sys.platform == "win32", "DPAPI is Windows")
    def test_token_is_protected_with_dpapi(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = st.Settings(Path(tmp))
            cfg.paired("http://127.0.0.1:1", 1, "PC", "secret-token-value-123")
            raw = Path(tmp, "settings.json").read_text(encoding="utf-8")
            self.assertNotIn("secret-token-value-123", raw)
            self.assertEqual(st.Settings(Path(tmp)).token(), "secret-token-value-123")


class AgentPortalTest(unittest.TestCase):
    """The recorder's client against a real portal server on this machine (no recording)."""

    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.app = create_app(new_database(self.root))
        self.server = Server(self.app)
        self.addCleanup(self.server.stop)
        self.url = self.server.url
        self.ui = Client(self.app)
        env = mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "local")})
        env.start()
        self.addCleanup(env.stop)

    def code(self):
        return self.ui.post("/api/v1/machines/pairing-codes").json()["display"]

    def test_pair_list_sessions_and_send_references(self):
        self.assertEqual(agent_main.main(["pair", "--portal", self.url, "--code", self.code(), "--name", "PC 7"]), 0)
        cfg = st.Settings(st.data_dir())
        self.assertEqual(cfg.values["machine"], "PC 7")
        self.ui.post("/api/v1/apps", json={"name": "Orders"})
        self.ui.post("/api/v1/apps/1/identities", json={"key": "http://127.0.0.1:8080"})
        self.ui.post("/api/v1/capture-sessions", json={"name": "Trial", "machine_id": 1,
                                                       "targets": [{"app_id": 1, "level": "full"}]})
        offered = agent_main._portal(cfg, None).sessions()
        self.assertEqual(offered["sessions"][0]["targets"],
                         [{"level": "full", "origin": "http://127.0.0.1:8080", "name": "Orders", "app_id": 1}])
        folder = cfg.recordings / "20261119T100000Z-0a1b2c3d"
        folder.mkdir(parents=True)
        (folder / "portal-outbox.json").write_text(json.dumps(references(portal_session_id=1)), encoding="utf-8")
        result = agent_main.send(cfg, folder.name)
        self.assertEqual(result["recording_id"], "ws-20261119T100000Z-0a1b2c3d")
        self.assertTrue((folder / "sent.json").is_file())
        flows = self.ui.get("/api/v1/apps/1/evidence").json()["recorded_flows"]
        self.assertIn("btnSave", [step.get("label") for flow in flows for step in flow["steps"]])
        self.assertEqual(self.ui.get("/api/v1/capture-sessions").json()["sessions"][0]["status"], "recorded")
        with self.assertRaises(PortalError):
            Portal(self.url, "wrong" * 10).sessions()


if __name__ == "__main__":
    unittest.main()
