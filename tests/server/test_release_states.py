"""Release states (Packaged → Tested → Published), deployment version checks and the migration."""

from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from zipfile import ZipFile

from support import Client, connect, freeze, new_database
from twinlab2.portal import VERSION_PATH, add_release, create_app
from test_registry import eid

NOT_VERIFIED = "Registered URL (not verified)"


class VersionStub:
    """A deployment on 127.0.0.1 and a free port that answers GET /<prefix>/.twinlab/version."""

    def __init__(self):
        self.body, self.status, self.paths = b"", 200, []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                stub.paths.append(self.path)
                self.send_response(stub.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(stub.body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/demo/"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def catalog_release(client, app_id=1):
    """The catalog's latest Tested or Published release of an application (None when there is none)."""
    apps = {a["id"]: a for a in client.get("/api/v1/apps").json()["apps"]}
    return apps[app_id]["tested_release"]


class ReleaseStatesTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.database = new_database(self.root)
        self.app = create_app(self.database)
        self.client = Client(self.app)
        # An app, a recording linked to it, the brief's one scenario, its brief and a release.
        created = self.client.post("/api/v1/apps", json={"name": "Billing"})
        self.assertEqual(created.status_code, 201, created.text)
        app_id = created.json()["id"]
        imported = self.client.post("/api/v1/recordings", data={"name": "Invoice"}, files={"log": (
            "steps.jsonl", b'{"seq":1,"action":"submit","url":"http://billing.local/"}\n', "application/jsonl")})
        self.assertEqual(imported.status_code, 201, imported.text)
        recording_id = imported.json()["id"]
        assigned = self.client.put(f"/api/v1/recordings/{recording_id}/assignments/{app_id}", json={
            "events": [eid(recording_id, 1)], "notes": "Invoice is saved\nwith its number"})
        self.assertEqual(assigned.status_code, 200, assigned.text)
        scenario = self.client.post(f"/api/v1/apps/{app_id}/scenarios", json={
            "name": "Invoice is saved", "expected": "The invoice has its number.",
            "events": [f"{recording_id}#{eid(recording_id, 1)}"]})
        self.assertEqual(scenario.status_code, 201, scenario.text)
        with ZipFile(io.BytesIO(freeze(self.client, app_id).content)) as archive:
            self.revision = archive.read("BUILD_TASK.md").decode().split("`")[1]
            self.task = archive.read("BUILD_TASK.md").decode()
        self.repo = self.root / "demo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "demo@example.invalid")
        self.git("config", "user.name", "TwinLab2 test")
        (self.repo / "run.py").write_text('print("ready")\n', encoding="utf-8")
        self.git("add", "run.py")
        self.git("commit", "-qm", "Release")
        self.commit = add_release(self.database, 1, "1.0", self.revision, self.repo, "HEAD")
        self.scenario = "scenario-1"

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True).strip()

    def state(self, release_id=1):
        with closing(connect(self.database)) as connection:
            return connection.execute("SELECT state FROM releases WHERE id = ?", (release_id,)).fetchone()[0]

    def detail(self, release_id=1):
        response = self.client.get(f"/api/v1/releases/{release_id}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def evidence(self, results=None, **changes):
        """A test body: `results` changes the per-scenario results (None drops a scenario's)."""
        values = {"commit": self.commit, "platform": "Windows 11, Python 3.13",
                  "launch": "python run.py", "reset_result": "pass", "reset_notes": "seed restored"}
        values.update(changes)
        body = {key: value for key, value in values.items() if value is not None}
        scenario_results = {self.scenario: "pass"}
        scenario_results.update(results or {})
        body["results"] = {key: value for key, value in scenario_results.items() if value is not None}
        return body

    def record(self, release_id=1, **kwargs):
        return self.client.post(f"/api/v1/releases/{release_id}/tests", json=self.evidence(**kwargs))

    def publish(self, release_id=1):
        return self.client.post(f"/api/v1/releases/{release_id}/publish")

    def test_a_new_release_is_packaged_and_cannot_be_published(self):
        self.assertEqual(self.state(), "packaged")
        self.assertEqual(self.detail()["release"]["state"], "packaged")
        response = self.publish()
        self.assertEqual(response.status_code, 400)
        self.assertIn("Only a tested release can be published", response.json()["detail"])
        self.assertEqual(self.state(), "packaged")
        self.assertIsNone(catalog_release(self.client))  # no tested release to download

    def test_testing_refuses_incomplete_or_wrong_evidence(self):
        refusals = {
            "another commit": self.evidence(commit="0" * 40),
            "a short commit": self.evidence(commit=self.commit[:12]),
            "no platform": self.evidence(platform=" "),
            "no launch instructions": self.evidence(launch=None),
            "no reset check": self.evidence(reset_result=None),
            "a reset check that is not pass or fail": self.evidence(reset_result="not-run"),
            "a scenario without a result": self.evidence(results={self.scenario: None}),
            "an unknown result": self.evidence(results={self.scenario: "maybe"}),
            "a scenario not in the brief": self.evidence(results={"invented": "pass"}),
        }
        for name, body in refusals.items():
            with self.subTest(name):
                self.assertEqual(self.client.post("/api/v1/releases/1/tests", json=body).status_code, 400)
                self.assertEqual(self.state(), "packaged")
        with closing(connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM release_tests").fetchone()[0], 0)

    def test_packaged_to_tested_to_published(self):
        tested = self.record(results={self.scenario: "not-run"})
        self.assertEqual(tested.status_code, 200, tested.text)
        self.assertEqual(tested.json()["notice"], "Recorded: the release is Tested.")
        self.assertEqual(self.state(), "tested")
        detail = self.detail()
        self.assertEqual(detail["release"]["state"], "tested")
        # the scenario is named by the brief
        self.assertEqual([(r["name"], r["result"]) for r in detail["test"]["results"]],
                         [("Invoice is saved", "not-run")])
        self.assertEqual(detail["test"]["platform"], "Windows 11, Python 3.13")
        catalog = catalog_release(self.client)
        self.assertEqual((catalog["id"], catalog["state"]), (1, "tested"))  # the tested release to download
        self.assertEqual(catalog["counts"], {"pass": 0, "fail": 0, "not-run": 1})
        self.assertIsNone(catalog["deployment"])  # no deployment registered yet
        # Testing again replaces the evidence while the release is not published.
        self.assertEqual(self.record().status_code, 200)
        self.assertEqual(catalog_release(self.client)["counts"], {"pass": 1, "fail": 0, "not-run": 0})
        published = self.publish()
        self.assertEqual(published.status_code, 200, published.text)
        self.assertEqual(published.json()["release"]["state"], "published")
        self.assertEqual(self.state(), "published")
        self.assertEqual(catalog_release(self.client)["state"], "published")
        refused = self.record()
        self.assertEqual(refused.status_code, 400)
        self.assertIn("published release is final", refused.json()["detail"])
        self.assertEqual(self.publish().status_code, 400)
        self.assertEqual(self.client.get("/api/v1/releases/1/download").status_code, 200)

    def test_catalog_shows_the_latest_tested_release_not_a_newer_packaged_one(self):
        self.record()
        (self.repo / "run.py").write_text('print("two")\n', encoding="utf-8")
        self.git("commit", "-qam", "Two")
        add_release(self.database, 1, "2.0", self.revision, self.repo, "HEAD")
        catalog = catalog_release(self.client)
        self.assertEqual((catalog["id"], catalog["version"]), (1, "1.0"))
        self.assertTrue(self.detail(1)["can_download"])
        self.assertEqual(self.client.get("/api/v1/releases/1/download").status_code, 200)

    def test_explicit_brief_scenarios(self):
        manifest = json.dumps({"format": 1, "evidence": [], "scenarios": [
            {"id": "create-invoice", "name": "Create an invoice"}, "Reject an invalid amount"]})
        with closing(connect(self.database)) as connection, connection:
            connection.execute("INSERT INTO briefs VALUES ('explicit-scenarios', 1, ?)", (manifest,))
            # a release is registered against a frozen builder package revision
            connection.execute("""INSERT INTO builder_packages(app_id, rev, revision, sha256, size, storage_ref,
                                  approved_by, approved_at, summary, diff, report)
                                  VALUES (1, 2, 'explicit-scenarios', '', 0, '', 'local', '', '', '', '[]')""")
        add_release(self.database, 1, "3.0", "explicit-scenarios", self.repo, "HEAD")
        self.assertEqual(self.detail(2)["scenarios"], [
            {"id": "create-invoice", "name": "Create an invoice"},
            {"id": "scenario-2", "name": "Reject an invalid amount"}])
        partial = {self.scenario: None, "create-invoice": "pass"}
        self.assertEqual(self.record(2, results=partial).status_code, 400)
        partial["scenario-2"] = "fail"
        self.assertEqual(self.record(2, results=partial).status_code, 200)
        self.assertEqual(catalog_release(self.client)["counts"], {"pass": 1, "fail": 1, "not-run": 0})

    def test_build_pack_documents_the_version_endpoint(self):
        self.assertIn(f"GET /{VERSION_PATH}", self.task)
        self.assertIn('{"commit"', self.task)

    def deploy(self, url, environment="Demo"):
        """Register a deployment; that environment's deployment in the release detail."""
        response = self.client.put("/api/v1/releases/1/deployments", json={"environment": environment, "url": url})
        self.assertEqual(response.status_code, 200, response.text)
        return {d["environment"]: d for d in response.json()["deployments"]}[environment]

    def test_deployment_is_verified_when_it_reports_the_release_commit(self):
        stub = VersionStub()
        self.addCleanup(stub.close)
        self.assertNotEqual(stub.server.server_address[1], 8000)
        stub.body = json.dumps({"commit": self.commit.upper()}).encode()
        deployment = self.deploy(stub.url)
        self.assertEqual(stub.paths, ["/demo/.twinlab/version"])
        self.assertEqual((deployment["verification"], deployment["label"]), ("verified", "Verified"))
        self.record()
        catalog = catalog_release(self.client)["deployment"]
        self.assertEqual((catalog["url"], catalog["label"]), (stub.url, "Verified"))  # Open demo
        # The deployment moves on to another commit: checking again withdraws the verification.
        stub.body = json.dumps({"commit": "f" * 40}).encode()
        checked = self.client.post("/api/v1/releases/1/deployments/check", json={"environment": "Demo"})
        self.assertEqual(checked.status_code, 200, checked.text)
        deployment = self.detail()["deployments"][0]
        self.assertEqual(deployment["label"], NOT_VERIFIED)
        self.assertIn("ffffffffffff, not this release", deployment["reason"])
        self.assertEqual(catalog_release(self.client)["deployment"]["label"], NOT_VERIFIED)
        other = self.client.post("/api/v1/releases/1/deployments/check", json={"environment": "Other"})
        self.assertEqual(other.status_code, 404)

    def test_deployment_without_a_usable_version_is_a_registered_url(self):
        stub = VersionStub()
        self.addCleanup(stub.close)
        self.record()
        cases = {
            "not found": (404, json.dumps({"commit": self.commit}).encode()),
            "not JSON": (200, b"<html>demo</html>"),
            "a JSON list": (200, b"[]"),
            "a short commit": (200, json.dumps({"commit": self.commit[:12]}).encode()),
        }
        for name, (status, body) in cases.items():
            with self.subTest(name):
                stub.status, stub.body = status, body
                deployment = self.deploy(stub.url)
                self.assertEqual(deployment["label"], NOT_VERIFIED)
                self.assertIn("No commit at .twinlab/version", deployment["reason"])
                catalog = catalog_release(self.client)["deployment"]
                self.assertEqual((catalog["url"], catalog["label"]), (stub.url, NOT_VERIFIED))  # Open demo
        stub.close()
        self.assertEqual(self.deploy(stub.url, "Stopped")["label"], NOT_VERIFIED)


class MigrationTest(unittest.TestCase):
    def test_existing_releases_become_packaged_and_deployments_unverified(self):
        with tempfile.TemporaryDirectory() as work:
            database = Path(work) / "catalog.db"
            with closing(connect(database)) as connection, connection:
                connection.executescript("""
                    CREATE TABLE apps (id INTEGER PRIMARY KEY, name TEXT NOT NULL, git_url TEXT NOT NULL,
                        requirements TEXT NOT NULL);
                    CREATE TABLE briefs (revision TEXT PRIMARY KEY, app_id INTEGER NOT NULL REFERENCES apps(id),
                        manifest TEXT NOT NULL);
                    CREATE TABLE releases (id INTEGER PRIMARY KEY, app_id INTEGER NOT NULL REFERENCES apps(id),
                        version TEXT NOT NULL, revision TEXT NOT NULL REFERENCES briefs(revision),
                        commit_sha TEXT NOT NULL, archive BLOB NOT NULL, archive_sha TEXT NOT NULL,
                        UNIQUE (app_id, version));
                    CREATE TABLE deployments (release_id INTEGER NOT NULL REFERENCES releases(id),
                        environment TEXT NOT NULL, url TEXT NOT NULL, username TEXT NOT NULL,
                        secret_ref TEXT NOT NULL, PRIMARY KEY (release_id, environment));
                    INSERT INTO apps VALUES (1, 'Orders', '', 'Build Orders.');
                    INSERT INTO briefs VALUES ('r1', 1, '{"evidence": []}');
                    INSERT INTO releases VALUES (1, 1, '1.0', 'r1', 'abc', x'00', 'sha');
                    INSERT INTO deployments VALUES (1, 'OpenShift', 'https://orders.example', '', '');
                """)
            for _ in range(2):  # the migration runs on every start
                client = Client(create_app(database))
            with closing(connect(database)) as connection:
                self.assertEqual(connection.execute("SELECT state, published_at FROM releases").fetchone(),
                                 ("packaged", None))
                self.assertEqual(connection.execute(
                    "SELECT verification, observed_commit, checked_at FROM deployments").fetchone(),
                    ("unverified", "", None))
            response = client.get("/api/v1/releases/1")
            self.assertEqual(response.status_code, 200, response.text)
            detail = response.json()
            self.assertEqual(detail["release"]["state"], "packaged")
            self.assertEqual([d["label"] for d in detail["deployments"]], [NOT_VERIFIED])
            self.assertEqual(detail["scenarios"], [])  # cannot be tested: the old brief names no scenarios
            self.assertIsNone(catalog_release(client))  # no tested release to download


if __name__ == "__main__":
    unittest.main()
