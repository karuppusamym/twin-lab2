"""Release states (Packaged → Tested → Published), deployment version checks and the migration."""

from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from zipfile import ZipFile

from support import freeze
from portal import VERSION_PATH, add_release, create_app
from test_registry import eid


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


class ReleaseStatesTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.database = self.root / "catalog.db"
        self.app = create_app(self.database)
        self.app.testing = True
        self.client = self.app.test_client()
        self.assertEqual(self.client.get("/").status_code, 200)
        with self.client.session_transaction() as session:
            self.token = session["form_token"]
        # An app, a recording linked to it, the brief's one scenario, its brief and a release.
        self.app_path = self.post("/apps", name="Billing").location
        recording = self.post("/recordings", name="Invoice", log=(
            io.BytesIO(b'{"seq":1,"action":"submit","url":"http://billing.local/"}\n'), "steps.jsonl")).location
        recording_id = recording.rsplit("/", 1)[1]
        self.post(recording, app_id="1", event=eid(recording_id, 1), notes="Invoice is saved\nwith its number")
        self.post(self.app_path + "/scenarios", name="Invoice is saved", expected="The invoice has its number.",
                  event=f"{recording_id}#{eid(recording_id, 1)}")
        with ZipFile(io.BytesIO(freeze(self.client, self.token, self.app_path).data)) as archive:
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
        self.scenario = "result:scenario-1"

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True).strip()

    def post(self, path, **values):
        return self.client.post(path, data={"token": self.token, **values})

    def state(self, release_id=1):
        with closing(sqlite3.connect(self.database)) as connection:
            return connection.execute("SELECT state FROM releases WHERE id = ?", (release_id,)).fetchone()[0]

    def evidence(self, **changes):
        values = {"commit": self.commit, self.scenario: "pass", "platform": "Windows 11, Python 3.13",
                  "launch": "python run.py", "reset_result": "pass", "reset_notes": "seed restored"}
        values.update(changes)
        return {key: value for key, value in values.items() if value is not None}

    def test_a_new_release_is_packaged_and_cannot_be_published(self):
        self.assertEqual(self.state(), "packaged")
        self.assertIn(b"Packaged", self.client.get("/releases/1").data)
        response = self.post("/releases/1/publish")
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"Only a tested release can be published", response.data)
        self.assertEqual(self.state(), "packaged")
        self.assertNotIn(b"Download tested release", self.client.get("/").data)

    def test_testing_refuses_incomplete_or_wrong_evidence(self):
        refusals = {
            "another commit": self.evidence(commit="0" * 40),
            "a short commit": self.evidence(commit=self.commit[:12]),
            "no platform": self.evidence(platform=" "),
            "no launch instructions": self.evidence(launch=None),
            "no reset check": self.evidence(reset_result=None),
            "a reset check that is not pass or fail": self.evidence(reset_result="not-run"),
            "a scenario without a result": self.evidence(**{self.scenario: None}),
            "an unknown result": self.evidence(**{self.scenario: "maybe"}),
            "a scenario not in the brief": self.evidence(**{"result:invented": "pass"}),
        }
        for name, values in refusals.items():
            with self.subTest(name):
                self.assertEqual(self.post("/releases/1/test", **values).status_code, 400)
                self.assertEqual(self.state(), "packaged")
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM release_tests").fetchone()[0], 0)

    def test_packaged_to_tested_to_published(self):
        self.assertEqual(self.post("/releases/1/test", **self.evidence(**{self.scenario: "not-run"})).status_code, 302)
        self.assertEqual(self.state(), "tested")
        page = self.client.get("/releases/1").data.decode()
        self.assertIn("Invoice is saved", page)  # the scenario is named by the brief
        self.assertIn("not-run", page)
        self.assertIn("Windows 11, Python 3.13", page)
        catalog = self.client.get("/").data.decode()
        self.assertIn("Download tested release", catalog)
        self.assertIn("0 pass · 0 fail · 1 not run", catalog)
        self.assertNotIn("Open demo", catalog)  # no deployment registered yet
        # Testing again replaces the evidence while the release is not published.
        self.assertEqual(self.post("/releases/1/test", **self.evidence()).status_code, 302)
        self.assertIn("1 pass · 0 fail · 0 not run", self.client.get("/").data.decode())
        self.assertEqual(self.post("/releases/1/publish").status_code, 302)
        self.assertEqual(self.state(), "published")
        self.assertIn(b"Published", self.client.get("/").data)
        refused = self.post("/releases/1/test", **self.evidence())
        self.assertEqual(refused.status_code, 400)
        self.assertIn(b"published release is final", refused.data)
        self.assertEqual(self.post("/releases/1/publish").status_code, 400)
        self.assertEqual(self.client.get("/releases/1/download").status_code, 200)

    def test_catalog_shows_the_latest_tested_release_not_a_newer_packaged_one(self):
        self.post("/releases/1/test", **self.evidence())
        (self.repo / "run.py").write_text('print("two")\n', encoding="utf-8")
        self.git("commit", "-qam", "Two")
        add_release(self.database, 1, "2.0", self.revision, self.repo, "HEAD")
        catalog = self.client.get("/").data.decode()
        self.assertIn('href="/releases/1"', catalog)
        self.assertNotIn('href="/releases/2"', catalog)
        self.assertIn('href="/releases/1/download"', catalog)

    def test_explicit_brief_scenarios(self):
        manifest = json.dumps({"format": 1, "evidence": [], "scenarios": [
            {"id": "create-invoice", "name": "Create an invoice"}, "Reject an invalid amount"]})
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("INSERT INTO briefs VALUES ('explicit-scenarios', 1, ?)", (manifest,))
            # a release is registered against a frozen builder package revision
            connection.execute("""INSERT INTO builder_packages(app_id, rev, revision, sha256, size, storage_ref,
                                  approved_by, approved_at, summary, diff, report)
                                  VALUES (1, 2, 'explicit-scenarios', '', 0, '', 'local', '', '', '', '[]')""")
        add_release(self.database, 1, "3.0", "explicit-scenarios", self.repo, "HEAD")
        page = self.client.get("/releases/2").data.decode()
        self.assertIn("Create an invoice", page)
        self.assertIn('name="result:scenario-2"', page)
        partial = self.evidence(**{self.scenario: None, "result:create-invoice": "pass"})
        self.assertEqual(self.post("/releases/2/test", **partial).status_code, 400)
        partial["result:scenario-2"] = "fail"
        self.assertEqual(self.post("/releases/2/test", **partial).status_code, 302)
        self.assertIn("1 pass · 1 fail · 0 not run", self.client.get("/").data.decode())

    def test_build_pack_documents_the_version_endpoint(self):
        self.assertIn(f"GET /{VERSION_PATH}", self.task)
        self.assertIn('{"commit"', self.task)

    def deploy(self, url, environment="Demo"):
        self.assertEqual(self.post("/releases/1", environment=environment, url=url).status_code, 302)
        return self.client.get("/releases/1").data.decode()

    def test_deployment_is_verified_when_it_reports_the_release_commit(self):
        stub = VersionStub()
        self.addCleanup(stub.close)
        self.assertNotEqual(stub.server.server_address[1], 8000)
        stub.body = json.dumps({"commit": self.commit.upper()}).encode()
        page = self.deploy(stub.url)
        self.assertEqual(stub.paths, ["/demo/.twinlab/version"])
        self.assertIn("Verified", page)
        self.assertNotIn("Registered URL (not verified)", page)
        self.post("/releases/1/test", **self.evidence())
        catalog = self.client.get("/").data.decode()
        self.assertIn(f'href="{stub.url}">Open demo', catalog)
        self.assertIn("Verified", catalog)
        # The deployment moves on to another commit: checking again withdraws the verification.
        stub.body = json.dumps({"commit": "f" * 40}).encode()
        self.assertEqual(self.post("/releases/1/deployments/check", environment="Demo").status_code, 302)
        page = self.client.get("/releases/1").data.decode()
        self.assertIn("Registered URL (not verified)", page)
        self.assertIn("ffffffffffff, not this release", page)
        self.assertIn("Registered URL (not verified)", self.client.get("/").data.decode())
        self.assertEqual(self.post("/releases/1/deployments/check", environment="Other").status_code, 404)

    def test_deployment_without_a_usable_version_is_a_registered_url(self):
        stub = VersionStub()
        self.addCleanup(stub.close)
        self.post("/releases/1/test", **self.evidence())
        cases = {
            "not found": (404, json.dumps({"commit": self.commit}).encode()),
            "not JSON": (200, b"<html>demo</html>"),
            "a JSON list": (200, b"[]"),
            "a short commit": (200, json.dumps({"commit": self.commit[:12]}).encode()),
        }
        for name, (status, body) in cases.items():
            with self.subTest(name):
                stub.status, stub.body = status, body
                page = self.deploy(stub.url)
                self.assertIn("Registered URL (not verified)", page)
                self.assertIn("No commit at .twinlab/version", page)
                catalog = self.client.get("/").data.decode()
                self.assertIn("Open demo", catalog)
                self.assertIn("Registered URL (not verified)", catalog)
        stub.close()
        self.assertIn("Registered URL (not verified)", self.deploy(stub.url, "Stopped"))


class MigrationTest(unittest.TestCase):
    def test_existing_releases_become_packaged_and_deployments_unverified(self):
        with tempfile.TemporaryDirectory() as work:
            database = Path(work) / "catalog.db"
            with closing(sqlite3.connect(database)) as connection, connection:
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
                client = create_app(database).test_client()
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute("SELECT state, published_at FROM releases").fetchone(),
                                 ("packaged", None))
                self.assertEqual(connection.execute(
                    "SELECT verification, observed_commit, checked_at FROM deployments").fetchone(),
                    ("unverified", "", None))
            page = client.get("/releases/1").data.decode()
            self.assertIn("Packaged", page)
            self.assertIn("Registered URL (not verified)", page)
            self.assertIn("cannot be tested", page)  # the old brief names no scenarios
            self.assertNotIn("Download tested release", client.get("/").data.decode())


if __name__ == "__main__":
    unittest.main()
