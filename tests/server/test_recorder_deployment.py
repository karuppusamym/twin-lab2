"""The recorder against a real portal on this machine, as on an office network: the upload queue
when the network dies in the middle of an upload (a TCP relay cuts every connection half-way
through a chunk and refuses new ones), the portal's update endpoint, and an update downloaded,
checked and handed to the installer (never run here)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from net_harness import ForwardProxy, Relay
from support import Client, Server, new_database
from twinlab2.portal import create_app
from twinlab2_recorder import net, policy, updater
from twinlab2_recorder.client import Portal, PortalError
from twinlab2_recorder.upload_queue import UploadQueue

SESSION = "20261121T100000Z-0a1b2c3d"
CHUNK = 1024
DIRECT = net.Network(proxy="direct")


class PortalCase(unittest.TestCase):
    env: dict = {}

    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.root = Path(work.name)
        patch = mock.patch.dict(os.environ, {"TWINLAB2_UPLOAD_CHUNK": str(CHUNK),
                                             "TWINLAB_RECORDER_POLICY": str(self.root / "no-policy.json"),
                                             **self.env})  # fmt: skip
        patch.start()
        self.addCleanup(patch.stop)
        registry = mock.patch.object(policy, "read_registry", return_value={})
        registry.start()
        self.addCleanup(registry.stop)
        self.database = new_database(self.root / "portal")
        self.server = Server(create_app(self.database))
        self.addCleanup(lambda: self.server.stop())
        code = Client(self.server.app).post("/api/v1/machines/pairing-codes").json()["code"]
        self.token = Portal(self.server.url, network=DIRECT).pair(code, "PC")["token"]


class NetworkLossTest(PortalCase):
    def test_the_network_dies_mid_upload_and_the_queue_resumes(self):
        session = self.root / "recorder" / "recordings" / SESSION
        session.mkdir(parents=True)
        (session / "steps.jsonl").write_bytes(os.urandom(CHUNK * 5 + 11))  # does not compress
        (session / "stop").write_text("")
        relay = Relay(self.server.port)
        self.addCleanup(relay.stop)
        waits = []
        portal = Portal(relay.url, self.token, timeout=5, network=DIRECT, sleep=waits.append)
        queue = UploadQueue(self.root / "recorder", destination={"portal": relay.url.rstrip("/"), "machine_id": 1})
        job = queue.add_session(session, SESSION)
        self.assertGreater(job["size"], 4 * CHUNK)

        relay.cut_on = (b"PUT /api/uploads/", 3)  # the third chunk is cut half-way, then no network
        job = queue.run(portal)[0]
        self.assertEqual(job["state"], "uploading")
        self.assertEqual(job["received_bytes"], 2 * CHUNK)  # two chunks confirmed before the cut
        self.assertIn("cannot be reached", job["last_error"])
        self.assertEqual(len(waits), 2)  # the chunk was retried with backoff before the queue gave up
        self.assertTrue(queue.file_of(job).is_file())
        self.assertGreater(job["next_try"], 0)
        self.assertEqual(queue.run(portal)[0]["state"], "uploading")  # not due yet: nothing is sent

        # the recorder restarts (a new queue object) and the network comes back
        relay.restore()
        relay.lines.clear()
        queue = UploadQueue(self.root / "recorder", destination={"portal": relay.url.rstrip("/"), "machine_id": 1})
        job = queue.run(Portal(relay.url, self.token, timeout=5, network=DIRECT), force=True)[0]
        self.assertEqual(job["state"], "confirmed", job)
        puts = [line for line in relay.lines if line.startswith("PUT ")]
        self.assertIn(f"offset={2 * CHUNK}", puts[0])  # resumed where the portal stopped, not from 0
        self.assertFalse(any("offset=0 " in line for line in puts))
        self.assertFalse(queue.file_of(job).exists())
        status = Client(self.server.app).http.get(f"/api/recordings/ws-{SESSION}/upload").json()
        self.assertEqual(status["state"], "confirmed")

    def test_through_the_office_proxy(self):
        proxy = ForwardProxy()
        self.addCleanup(proxy.stop)
        portal = Portal(self.server.url, self.token, network=net.Network(proxy=proxy.url))
        self.assertEqual(portal.sessions()["sessions"], [])
        self.assertEqual(proxy.seen, [("GET", f"{self.server.url}/api/agent/sessions")])


class UpdateEndpointTest(PortalCase):
    def setUp(self):
        self.releases = Path(tempfile.mkdtemp(prefix="twinlab-releases-"))
        self.addCleanup(lambda: [p.unlink() for p in self.releases.iterdir()] and self.releases.rmdir())
        self.env = {"TWINLAB2_RECORDER_RELEASES": str(self.releases)}
        super().setUp()
        self.msi = b"MSI-" + os.urandom(3 * 1024 * 1024 + 5)

    def release(self, version="9.1.0", sha=None, data=None):
        name = f"TwinLabRecorder-{version}-x64.msi"
        (self.releases / name).write_bytes(data if data is not None else self.msi)
        (self.releases / "latest.json").write_text(json.dumps({
            "version": version, "file": name, "sha256": sha or hashlib.sha256(self.msi).hexdigest(),
            "notes": "Proxy and update support", "published_at": "2026-11-21T10:00:00Z"}), encoding="utf-8")
        return name

    def ask(self, version="0.3.0", token=None):
        return Client(self.server.app).http.get(f"/api/agent/update?version={version}",
                                               headers={"Authorization": f"Bearer {token or self.token}"})

    def test_nothing_offered_without_a_release(self):
        answer = self.ask()
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(answer.json(), {"current": "0.3.0", "update_available": False, "latest": None})
        self.assertEqual(self.ask(token="x" * 50).status_code, 401)

    def test_an_offered_release_and_its_download(self):
        name = self.release()
        doc = self.ask().json()
        self.assertTrue(doc["update_available"])
        self.assertEqual(doc["latest"]["url"], f"/api/agent/update/{name}")
        self.assertEqual(doc["latest"]["size"], len(self.msi))
        self.assertFalse(self.ask("9.1.0").json()["update_available"])
        http = Client(self.server.app).http
        got = http.get(doc["latest"]["url"], headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual((got.status_code, got.content), (200, self.msi))
        self.assertEqual(http.get(doc["latest"]["url"]).status_code, 401)
        self.assertEqual(http.get("/api/agent/update/TwinLabRecorder-1.0.0-x64.msi",
                                  headers={"Authorization": f"Bearer {self.token}"}).status_code, 404)

    def test_a_release_that_does_not_match_its_checksum_is_not_offered(self):
        self.release(sha="0" * 64)
        self.assertIsNone(self.ask().json()["latest"])
        self.release(data=self.msi[:-1])  # half-copied
        self.assertIsNone(self.ask().json()["latest"])

    def test_the_recorder_downloads_checks_and_hands_it_to_the_installer(self):
        self.release()
        folder = self.root / "recorder"
        folder.mkdir()
        portal = Portal(self.server.url, self.token, network=DIRECT)
        trial = policy.MachinePolicy(allow_unsigned_updates=True)
        unsigned = lambda _p: {"status": "NotSigned", "thumbprint": "", "subject": ""}  # noqa: E731
        offer, msi, note = updater.prepare(portal, folder, trial, verifier=unsigned)
        self.assertEqual((offer.version, note), ("9.1.0", "unsigned (allowed by policy)"))
        self.assertEqual(msi.read_bytes(), self.msi)
        ran = []
        updater.take_lock(folder)
        with self.assertRaises(updater.UpdateRefused):
            updater.install(msi, folder, True, run=lambda cmd, **kw: ran.append(cmd))
        updater.release_lock(folder)
        updater.install(msi, folder, True, run=lambda cmd, **kw: ran.append(cmd))
        self.assertEqual(ran[0][:3], ["msiexec", "/i", str(msi)])
        # the office's default: only signed updates; an unsigned one is deleted, not kept
        with self.assertRaises(updater.UpdateRefused):
            updater.prepare(portal, folder, policy.MachinePolicy(), verifier=unsigned)
        self.assertFalse(msi.exists())
        # updates off: the portal is not even asked
        self.assertIsNone(updater.prepare(portal, folder, policy.MachinePolicy(updates="off")))

    def test_an_answer_bigger_than_announced_is_refused(self):
        name = self.release()
        portal = Portal(self.server.url, self.token, network=DIRECT)
        with self.assertRaisesRegex(PortalError, "larger"):
            b"".join(portal.download(f"/api/agent/update/{name}", max_bytes=1024))
        with self.assertRaisesRegex(PortalError, "invalid update address"):
            list(portal.download("https://evil.example.test/x.msi", max_bytes=10))


if __name__ == "__main__":
    unittest.main()
