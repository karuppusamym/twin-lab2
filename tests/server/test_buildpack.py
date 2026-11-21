"""The build pack carries the reviewed evidence the portal shows, and no raw capture."""

from contextlib import closing
import hashlib
import html
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from zipfile import ZipFile

from support import freeze
from twinlab2.portal import create_app
from twinlab2 import scenarios
from support import connect, new_database
from test_registry import CV, DESK, WEB, eid, references

RECORDING = "ws-20261119T100000Z-0a1b2c3d"


def ref(n):
    """A scenario's link to event n of the recording: by its event id."""
    return f"{RECORDING}#{eid(RECORDING, n)}"
SECRET = "hunter2-SECRET-VALUE"
SSN = "078-05-1120"


def sensitive_references():
    doc = references()
    # what a recorder must not send, and the portal must not pass on if one did
    doc["flows"][DESK][0]["value"] = SECRET
    doc["flows"][DESK][0]["typed"] = SECRET
    doc["flows"][WEB][0]["screen"] = f"http://clerk:{SECRET}@127.0.0.1:8080/cases/{{id}}?ssn={SSN}#{SECRET}"
    doc["flows"][DESK][1]["key"] = "x"  # a single typed character is not a named key
    doc["headers"] = {"Cookie": f"session={SECRET}"}
    doc["screenshots"] = [{"png": SECRET}]
    return doc


class BuildPackTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.database = new_database(self.work.name)
        self.client = self.client_for(self.database)

    def client_for(self, database):
        app = create_app(database)
        app.testing = True
        client = app.test_client()
        self.assertEqual(client.get("/").status_code, 200)
        with client.session_transaction() as session:
            self.token = session["form_token"]
        return client

    def post(self, path, **values):
        return self.client.post(path, data={"token": self.token, **values})

    def record(self):
        self.post("/machines/pair")
        code = re.search(r'id="pair-code">([A-Z0-9-]+)<', self.client.get("/machines").data.decode()).group(1)
        token = self.client.post("/api/agent/pair", json={"contract_version": CV, "code": code, "name": "PC"}).json["token"]
        response = self.client.post("/api/agent/references", json=sensitive_references(),
                                    headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 200, response.data)
        return response.json["discovered"]

    def scenario(self, app_id, **values):
        fields = {"name": "Find a case", "starting": "Signed in as a clerk.",
                  "actions": "Search for the case\nOpen it", "expected": "The case is shown.",
                  "failures": "Unknown case: a message, nothing opens", "examples": "Case number: 1001",
                  "questions": "Can a clerk search closed cases?", "notes": "",
                  "event": [ref(1), ref(2)]}
        fields.update(values)
        response = self.post(f"/apps/{app_id}/scenarios", **fields)
        self.assertEqual(response.status_code, 302, response.data)

    def download(self, app_id):
        response = freeze(self.client, self.token, f"/apps/{app_id}")
        self.assertEqual(response.status_code, 200, response.data)
        with ZipFile(io.BytesIO(response.data)) as archive:
            return response.data, {name: archive.read(name) for name in archive.namelist()}

    def test_pack_has_screen_control_and_label_of_each_selected_event(self):
        desk, web = self.record()
        self.scenario(desk)
        _, files = self.download(desk)
        manifest = json.loads(files["evidence.json"])
        self.assertEqual(manifest["format"], 2)
        events = manifest["evidence"][0]["events"]
        self.assertEqual(events[0], {"event_id": eid(RECORDING, 1), "seq": 1, "action": "type", "control": "Edit",
                                     "label": "txtSearch", "screen": "MainForm"})
        scenario = manifest["scenarios"][0]
        self.assertEqual([e["label"] for e in scenario["events"]], ["txtSearch", "btnOpen"])
        self.assertEqual(scenario["expected_result"], "The case is shown.")
        self.assertEqual(scenario["example_values"], ["Case number: 1001"])
        self.assertEqual(scenario["failure_cases"], ["Unknown case: a message, nothing opens"])
        self.assertEqual(scenario["unresolved_questions"], ["Can a clerk search closed cases?"])
        task = files["BUILD_TASK.md"].decode()
        self.assertIn("label `txtSearch`", task)
        self.assertIn("screen `MainForm`", task)
        self.assertIn("Can a clerk search closed cases?", task)
        # the web application's screen keeps its path, not its credentials, query or fragment
        self.scenario(web, name="Save a case", event=[ref(5)])
        _, files = self.download(web)
        web_event = json.loads(files["evidence.json"])["evidence"][0]["events"][0]
        self.assertEqual(web_event["screen"], "http://127.0.0.1:8080/cases/{id}")
        self.assertEqual(web_event["label"], "btnSave")

    def test_raw_sensitive_capture_stays_out_of_the_pack(self):
        desk, web = self.record()
        for app_id in (desk, web):
            self.scenario(app_id, event=[])
            package, files = self.download(app_id)
            for blob in (package, *files.values()):
                self.assertNotIn(SECRET.encode(), blob)
                self.assertNotIn(SSN.encode(), blob)
                self.assertNotIn(b"clerk:", blob)
                self.assertNotIn(b"Cookie", blob)
            self.assertNotIn('"key": "x"', files["evidence.json"].decode())
        # the reference the recorder sent is kept, separately, in the catalog database
        with closing(connect(self.database)) as db:
            payload = db.execute("SELECT payload FROM session_refs").fetchone()[0]
        self.assertIn(SSN, payload)

    def test_preview_shows_exactly_what_the_download_contains(self):
        desk, _ = self.record()
        self.scenario(desk, notes="Use <b>bold</b> & ampersands as text.")
        page = self.client.get(f"/apps/{desk}/build-pack/preview")
        self.assertEqual(page.status_code, 200)
        text = page.data.decode()
        package, files = self.download(desk)
        self.assertIn(hashlib.sha256(package).hexdigest(), text)
        self.assertIn(f"{len(package)} bytes", text)
        shown = {html.unescape(path): html.unescape(body)
                 for path, body in re.findall(r'<pre class="file" data-path="([^"]+)">(.*?)</pre>', text, re.S)}
        self.assertEqual(set(shown), set(files))
        for path, content in files.items():
            self.assertEqual(shown[path], content.decode("utf-8"), path)
            self.assertIn(hashlib.sha256(content).hexdigest(), text)
        revision = re.search(r"Revision <code>([0-9a-f]{64})</code>", text).group(1)
        self.assertEqual(self.client.get(f"/apps/{desk}/briefs/{revision}").data, package)
        # a second export of the same state is byte-identical
        self.assertEqual(self.download(desk)[0], package)

    def test_scenario_fields_round_trip_and_events_must_be_selected(self):
        desk, _ = self.record()
        self.scenario(desk)
        with closing(connect(self.database)) as db:
            [saved] = scenarios.load(db, desk)
        self.assertEqual(
            {k: saved[k] for k in ("name", "starting", "actions", "expected", "failures", "examples", "questions",
                                   "events")},
            {"name": "Find a case", "starting": "Signed in as a clerk.", "actions": "Search for the case\nOpen it",
             "expected": "The case is shown.", "failures": "Unknown case: a message, nothing opens",
             "examples": "Case number: 1001", "questions": "Can a clerk search closed cases?",
             "events": [ref(1), ref(2)]})
        page = self.client.get(f"/apps/{desk}").data.decode()
        for value in ("Find a case", "Signed in as a clerk.", "Search for the case\nOpen it", "Case number: 1001"):
            self.assertIn(value, page)
        self.assertIn(f'value="{ref(1)}" checked', page)
        edited = self.post(f"/apps/{desk}/scenarios/{saved['id']}", name="Find a closed case",
                           expected="Shown read-only.", event=ref(2))
        self.assertEqual(edited.status_code, 302)
        manifest = json.loads(self.download(desk)[1]["evidence.json"])
        self.assertEqual(manifest["scenarios"][0]["name"], "Find a closed case")
        self.assertEqual([e["seq"] for e in manifest["scenarios"][0]["events"]], [2])
        self.assertEqual(manifest["unresolved_questions"], [f"Selected event {ref(1)} is not part of any scenario."])
        self.assertEqual(self.post(f"/apps/{desk}/scenarios", name="X", event=ref(5)).status_code, 400)
        self.assertEqual(self.post(f"/apps/{desk}/scenarios", name="").status_code, 400)
        self.assertEqual(self.post(f"/apps/{desk}/scenarios/999", name="X").status_code, 404)
        self.assertEqual(self.post(f"/apps/{desk}/scenarios/{saved['id']}/remove").status_code, 302)
        self.assertEqual(self.client.get(f"/apps/{desk}/build-pack/preview").status_code, 400)

    def test_existing_requirements_move_into_a_scenario(self):
        with closing(connect(self.database)) as db, db:
            db.execute("INSERT INTO apps(name, git_url, requirements) VALUES ('Old', '', ?)",
                       ("Invoices total the order lines.\nVAT is 20%.",))
        self.client = self.client_for(self.database)
        page = self.client.get("/apps/1").data.decode()
        self.assertIn(scenarios.MIGRATED_NAME, page)
        self.assertIn("Invoices total the order lines.\nVAT is 20%.", page)
        manifest = json.loads(self.download(1)[1]["evidence.json"])
        self.assertEqual(manifest["scenarios"][0]["notes"], "Invoices total the order lines.\nVAT is 20%.")
        self.assertIn("The expected result of this scenario is not stated.", manifest["scenarios"][0]["unresolved_questions"])
        self.client_for(self.database)  # migrating again adds nothing
        with closing(connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM scenarios").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
