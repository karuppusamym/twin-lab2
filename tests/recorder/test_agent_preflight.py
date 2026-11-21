"""The recorder runs Twin Lab's preflight before it records, refuses to start when a check fails
unless the person overrides it, and sends the checks (`preflight`) with the references.

The recordings run in their own process (`python recorder/twinlab2_recorder/main.py`), as the
window starts them. The session's one target is a mainframe host that does not answer:
its check fails on every PC, and the recorder's mainframe channel (a local proxy) needs nothing
else to start.
"""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import unittest

from twinlab2_recorder import main as agent_main
from twinlab2_contract import recorder_contract as contract
from test_agent import AgentPortalTest

MAIN = Path(agent_main.__file__).resolve()


def unused_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


FAILED = {"checks": [{"name": "recorder", "ok": True, "detail": "contract 1.1.0"},
                     {"name": "reach https://billing.example.test", "ok": False, "detail": "does not answer"}],
          "ready": False}  # fmt: skip


class PreflightUnitTest(unittest.TestCase):
    def test_failed_checks_and_their_details_are_shown(self):
        self.assertEqual(agent_main.failed_checks(FAILED), ["reach https://billing.example.test: does not answer"])
        text = agent_main.describe_checks(FAILED)
        self.assertIn("FAIL  reach https://billing.example.test: does not answer", text)
        self.assertIn("ok    recorder", text)
        self.assertTrue(text.endswith("NOT READY"))
        self.assertTrue(agent_main.describe_checks({"checks": FAILED["checks"][:1], "ready": True}).endswith("Ready"))

    def test_review_says_the_recording_started_with_failed_checks(self):
        box = {"session": {"id": "s", "name": "n"}, "apps": [], "flows": {}, "timeline": [], "preflight": FAILED}
        self.assertIn("RECORDING STARTED WITH FAILED CHECKS", agent_main.describe(box))
        self.assertIn("reach https://billing.example.test: does not answer", agent_main.describe(box))
        box["preflight"] = {"checks": FAILED["checks"][:1], "ready": True}
        self.assertNotIn("FAILED CHECKS", agent_main.describe(box))


class PreflightRecordTest(unittest.TestCase):
    """`record` and `run-session` against the collectors, with a portal on this machine."""

    setUp, code = AgentPortalTest.setUp, AgentPortalTest.code

    def recorder(self, *args, timeout=180):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        return subprocess.run([sys.executable, str(MAIN), *args], capture_output=True, text=True,
                              encoding="utf-8", env=env, timeout=timeout)  # fmt: skip

    def recordings(self) -> list[Path]:
        folder = self.root / "local" / "TwinLabRecorder" / "recordings"
        return sorted(p for p in folder.iterdir() if p.is_dir()) if folder.is_dir() else []

    def mainframe_args(self):
        return ["--tn3270", f"127.0.0.1:{unused_port()}", "--tn3270-listen", str(unused_port()),
                "--minutes", "0.05", "--name", "Preflight trial"]  # fmt: skip

    def test_a_failed_check_refuses_to_start(self):
        out = self.recorder("record", *self.mainframe_args())
        self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
        self.assertRegex(out.stdout, r"FAIL  mainframe 127\.0\.0\.1:\d+: the host does not answer")
        self.assertIn("NOT READY", out.stdout)
        self.assertIn("--override-preflight", out.stderr)
        self.assertNotIn("recording session", out.stdout)
        self.assertEqual(self.recordings(), [])

    def test_run_session_refuses_a_failed_block_without_the_override(self):
        # as the window writes it
        conf = {"id": "20261120T120000Z-0123abcd", "name": "From the window", "deny_keys": [],
                "targets": [{"origin": "https://billing.example.test"}], "minutes": 1, "preflight": FAILED}  # fmt: skip
        path = self.root / "next-session.json"
        path.write_text(json.dumps(conf), encoding="utf-8")
        out = self.recorder("run-session", "--config", str(path))
        self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
        self.assertIn("not ready: reach https://billing.example.test: does not answer", out.stderr)
        self.assertEqual(self.recordings(), [])

    def test_override_records_with_the_failures_in_the_block_and_the_portal_shows_them(self):
        self.assertEqual(agent_main.main(["pair", "--portal", self.url, "--code", self.code(), "--name", "PC 7"]), 0)
        out = self.recorder("record", *self.mainframe_args(), "--override-preflight", "--send")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("recording anyway (--override-preflight)", out.stderr)
        self.assertIn("RECORDING STARTED WITH FAILED CHECKS", out.stdout)
        [folder] = self.recordings()
        box = json.loads((folder / "portal-outbox.json").read_text(encoding="utf-8"))
        pre = box["preflight"]
        self.assertFalse(pre["ready"])
        failed = [c for c in pre["checks"] if not c["ok"]]
        self.assertEqual([c["name"].split(" ")[0] for c in failed], ["mainframe"], pre)
        self.assertIn("does not answer", failed[0]["detail"])
        self.assertEqual({c["name"] for c in pre["checks"] if c["ok"]} >= {"recorder", "disk"}, True, pre)
        # nothing local in what goes to the portal (the data folder's path names the user)
        self.assertNotIn(str(self.root), json.dumps(pre))
        self.assertNotIn(str(Path.home()), json.dumps(pre))
        # within the contract, and sent: the portal's recording page shows the failed check
        doc = agent_main.contract_references(box)
        self.assertEqual(contract.errors(doc, "references"), [])
        self.assertEqual(doc["preflight"], pre)
        sent = json.loads((folder / "sent.json").read_text(encoding="utf-8"))
        shown = self.ui.get(f"/api/v1/recordings/{sent['recording_id']}")
        self.assertEqual(shown.status_code, 200, shown.text)
        preflight = shown.json()["status"]["preflight"]
        self.assertFalse(preflight["ready"])  # not ready: recording started with failed checks
        [check] = [c for c in preflight["checks"] if not c["ok"]]
        self.assertRegex(check["name"], r"mainframe 127\.0\.0\.1:\d+")
        self.assertIn("the host does not answer", check["detail"])


if __name__ == "__main__":
    unittest.main()
