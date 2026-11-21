import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from zipfile import ZipFile

from twinlab2_recorder.capture import recover
from twinlab2.openshift import resources
from twinlab2.portal import add_release, create_app
from support import Client, freeze, new_database
from test_registry import eid


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.database = new_database(self.root)
        self.app = create_app(self.database)
        self.client = Client(self.app)
        self.assertEqual(self.client.get("/api/v1/apps").status_code, 200)

    def recording(self, name, data):
        response = self.client.post("/api/v1/recordings", data={"name": name},
                                    files={"log": ("steps.jsonl", data, "application/x-ndjson")})
        self.assertIn(response.status_code, (200, 201), response.text)
        return response.json()["id"]

    def assign(self, recording, app_id, *events, notes=""):
        return self.client.put(f"/api/v1/recordings/{recording}/assignments/{app_id}",
                               json={"events": list(events), "notes": notes})

    def demo(self, name):
        response = self.client.post("/api/v1/apps", json={"name": name})
        self.assertEqual(response.status_code, 201, response.text)
        app_id = response.json()["id"]
        scenario = self.client.post(f"/api/v1/apps/{app_id}/scenarios", json={"name": f"Build {name}."})
        self.assertEqual(scenario.status_code, 201, scenario.text)
        return app_id

    def pack(self, app_id):
        response = freeze(self.client, app_id)
        self.assertEqual(response.status_code, 200, response.text)
        with ZipFile(io.BytesIO(response.content)) as archive:
            manifest = archive.read("evidence.json")
            return hashlib.sha256(manifest).hexdigest(), json.loads(manifest), response.content

    def test_recording_recovery_and_shared_evidence(self):
        orders, billing = self.demo("Orders"), self.demo("Billing")
        channels = self.root / "capture"
        (channels / "web").mkdir(parents=True)
        (channels / "desktop").mkdir()
        (channels / "session.json").write_text(json.dumps({
            "id": "a" * 32, "channels": [{"name": "web"}, {"name": "desktop"}],
        }))
        (channels / "web/steps.jsonl").write_bytes(
            b'{"seq":1,"action":"click","url":"http://orders.local/?secret=excluded"}\n{"seq":'
        )
        self.assertEqual(recover(channels), 1)
        first = (channels / "steps.jsonl").read_bytes()
        shared = self.recording("Order handoff", first)
        self.assertEqual(self.assign(shared, orders, eid(shared, 1), notes="Order saved").status_code, 200)
        (channels / "desktop/steps.jsonl").write_bytes(
            b'{"seq":1,"action":"submit","surface":"desktop","process":"Billing.exe"}\n'
        )
        self.assertEqual(recover(channels), 2)
        self.assertEqual(self.recording("Recovered", (channels / "steps.jsonl").read_bytes()), shared)
        self.assertEqual(self.recording("Older partial copy", first), shared)
        self.assertIn("Billing.exe", self.client.get(f"/api/v1/recordings/{shared}").text)
        self.assertEqual(self.assign(shared, billing, eid(shared, 1000000001)).status_code, 200)
        other = self.recording("Invalid amount", b'{"seq":4,"action":"submit"}\n')
        self.assertEqual(self.assign(other, billing, eid(other, 4)).status_code, 200)
        _, orders_pack, _ = self.pack(orders)
        _, billing_pack, _ = self.pack(billing)
        self.assertEqual(orders_pack["evidence"][0]["events"], [{"event_id": eid(shared, 1), "seq": 1, "action": "click"}])
        self.assertEqual(len(billing_pack["evidence"]), 2)
        self.assertNotIn("orders.local", str(orders_pack))

    def test_build_revision_git_release_and_deployment(self):
        app_id = self.demo("Billing")
        response = self.client.post(f"/api/v1/apps/{app_id}/attachments",
                                    data={"title": "API contract", "kind": "API", "share": "reviewed"},
                                    files={"file": ("contract.json", b'{"endpoint":"/invoice"}', "application/json")})
        self.assertEqual(response.status_code, 201, response.text)
        attachment = response.json()["id"]
        revision, manifest, snapshot = self.pack(app_id)
        self.assertEqual(manifest["attachments"][0]["kind"], "API")
        self.assertEqual(self.client.delete(f"/api/v1/apps/{app_id}/attachments/{attachment}").status_code, 204)
        self.assertNotEqual(self.pack(app_id)[0], revision)
        self.assertEqual(self.client.get(f"/api/v1/apps/{app_id}/briefs/{revision}/download").content, snapshot)
        repo = self.root / "demo"
        repo.mkdir()

        def git(*args):
            return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()

        git("init", "-q")
        git("config", "user.email", "demo@example.invalid")
        git("config", "user.name", "TwinLab2 test")
        entry = repo / "run.py"
        entry.write_text('print("Invoice demo ready")\n', encoding="utf-8")
        git("add", "run.py")
        git("commit", "-qm", "First release")
        entry.write_text('print("Uncommitted")\n', encoding="utf-8")
        commit = add_release(self.database, app_id, "1.0", revision, repo, "HEAD")
        self.assertEqual(commit, git("rev-parse", "HEAD"))
        archive = self.client.get("/api/v1/releases/1/download").content
        extracted = self.root / "download"
        with ZipFile(io.BytesIO(archive)) as package:
            package.extractall(extracted)
        output = subprocess.check_output([sys.executable, str(extracted / "run.py")], text=True)
        self.assertEqual(output.strip(), "Invoice demo ready")
        git("add", "run.py")
        git("commit", "-qm", "Second release")
        add_release(self.database, app_id, "2.0", self.pack(app_id)[0], repo, "HEAD")
        self.assertEqual(self.client.get("/api/v1/releases/1/download").content, archive)
        bad = self.client.put("/api/v1/releases/1/deployments", json={"environment": "Local", "url": "javascript:bad"})
        self.assertEqual(bad.status_code, 400)
        registered = self.client.put("/api/v1/releases/1/deployments", json={
            "environment": "OpenShift", "url": "https://demo.example", "username": "demo",
            "secret_ref": "vault:demo/billing"})
        self.assertEqual(registered.status_code, 200, registered.text)
        detail = self.client.get("/api/v1/releases/1").json()
        self.assertIn("https://demo.example", [d["url"] for d in detail["deployments"]])
        self.assertEqual(detail["release"]["archive_sha"], hashlib.sha256(archive).hexdigest())
        fresh = Client(create_app(self.database)).get(f"/api/v1/apps/{app_id}").json()
        self.assertIn("2.0", [r["version"] for r in fresh["releases"]])
        manifest = resources("billing", "registry.example/billing:1.0", 8080, "/health")
        deployment, service, route = manifest["items"]
        self.assertEqual(deployment["spec"]["selector"]["matchLabels"], service["spec"]["selector"])
        self.assertEqual(route["spec"]["port"]["targetPort"], service["spec"]["ports"][0]["name"])


if __name__ == "__main__":
    unittest.main()
