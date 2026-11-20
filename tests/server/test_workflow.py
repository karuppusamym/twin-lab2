import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from zipfile import ZipFile

from capture import recover
from openshift import resources
from support import freeze
from portal import add_release, create_app
from support import new_database
from test_registry import eid


def ev(path, n):
    """Event n of the recording at `path`: by its event id."""
    return eid(path.rsplit("/", 1)[1], n)


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.database = new_database(self.root)
        self.app = create_app(self.database)
        self.app.testing = True
        self.client = self.app.test_client()
        self.assertEqual(self.client.get("/").status_code, 200)
        with self.client.session_transaction() as session:
            self.token = session["form_token"]

    def post(self, path, **values):
        return self.client.post(path, data={"token": self.token, **values})

    def recording(self, name, data):
        response = self.post("/recordings", name=name, log=(io.BytesIO(data), "steps.jsonl"))
        self.assertEqual(response.status_code, 302, response.data)
        return response.location

    def demo(self, name):
        path = self.post("/apps", name=name).location
        self.assertEqual(self.post(path + "/scenarios", name=f"Build {name}.").status_code, 302)
        return path

    def pack(self, path):
        response = freeze(self.client, self.token, path)
        self.assertEqual(response.status_code, 200)
        with ZipFile(io.BytesIO(response.data)) as archive:
            manifest = archive.read("evidence.json")
            return hashlib.sha256(manifest).hexdigest(), json.loads(manifest), response.data

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
        self.assertEqual(self.post(shared, app_id="1", event=ev(shared, 1), notes="Order saved").status_code, 302)
        (channels / "desktop/steps.jsonl").write_bytes(
            b'{"seq":1,"action":"submit","surface":"desktop","process":"Billing.exe"}\n'
        )
        self.assertEqual(recover(channels), 2)
        self.assertEqual(self.recording("Recovered", (channels / "steps.jsonl").read_bytes()), shared)
        self.assertEqual(self.recording("Older partial copy", first), shared)
        self.assertIn(b"Billing.exe", self.client.get(shared + "?app_id=2").data)
        self.assertEqual(self.post(shared, app_id="2", event=ev(shared, 1000000001)).status_code, 302)
        other = self.recording("Invalid amount", b'{"seq":4,"action":"submit"}\n')
        self.assertEqual(self.post(other, app_id="2", event=ev(other, 4)).status_code, 302)
        _, orders_pack, _ = self.pack(orders)
        _, billing_pack, _ = self.pack(billing)
        self.assertEqual(orders_pack["evidence"][0]["events"], [{"event_id": ev(shared, 1), "seq": 1, "action": "click"}])
        self.assertEqual(len(billing_pack["evidence"]), 2)
        self.assertNotIn("orders.local", str(orders_pack))

    def test_build_revision_git_release_and_deployment(self):
        path = self.demo("Billing")
        attachments = {"title": "API contract", "kind": "API", "share": "reviewed"}
        response = self.post(path + "/attachments", **attachments,
                             file=(io.BytesIO(b'{"endpoint":"/invoice"}'), "contract.json"))
        self.assertEqual(response.status_code, 302)
        revision, manifest, snapshot = self.pack(path)
        self.assertEqual(manifest["attachments"][0]["kind"], "API")
        self.assertEqual(self.post(path + "/attachments/1/remove").status_code, 302)
        self.assertNotEqual(self.pack(path)[0], revision)
        self.assertEqual(self.client.get(f"{path}/briefs/{revision}").data, snapshot)
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
        commit = add_release(self.database, 1, "1.0", revision, repo, "HEAD")
        self.assertEqual(commit, git("rev-parse", "HEAD"))
        archive = self.client.get("/releases/1/download").data
        extracted = self.root / "download"
        with ZipFile(io.BytesIO(archive)) as package:
            package.extractall(extracted)
        output = subprocess.check_output([sys.executable, str(extracted / "run.py")], text=True)
        self.assertEqual(output.strip(), "Invoice demo ready")
        git("add", "run.py")
        git("commit", "-qm", "Second release")
        add_release(self.database, 1, "2.0", self.pack(path)[0], repo, "HEAD")
        self.assertEqual(self.client.get("/releases/1/download").data, archive)
        self.assertEqual(self.post("/releases/1", environment="Local", url="javascript:bad").status_code, 400)
        self.assertEqual(self.post("/releases/1", environment="OpenShift", url="https://demo.example",
                                   username="demo", secret_ref="vault:demo/billing").status_code, 302)
        page = self.client.get("/releases/1").data
        self.assertIn(b"https://demo.example", page)
        self.assertIn(hashlib.sha256(archive).hexdigest().encode(), page)
        self.assertIn(b"2.0", create_app(self.database).test_client().get(path).data)
        manifest = resources("billing", "registry.example/billing:1.0", 8080, "/health")
        deployment, service, route = manifest["items"]
        self.assertEqual(deployment["spec"]["selector"]["matchLabels"], service["spec"]["selector"])
        self.assertEqual(route["spec"]["port"]["targetPort"], service["spec"]["ports"][0]["name"])


if __name__ == "__main__":
    unittest.main()
