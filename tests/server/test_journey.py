"""The journey: Choose application -> Add examples -> Review required behaviour -> Build ->
Verify -> Open or download, and the one next action the pages show."""

import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from zipfile import ZipFile

from twinlab2.journey import JOURNEY, STATUS_LABELS, app_journey
from twinlab2.portal import add_release, create_app
from support import Client, freeze, new_database
from test_registry import eid


def facts(**changes):
    base = {"status": "known", "owner": "Ops team", "purpose": "Order demo", "identities": 1,
            "machines": 1, "examples": 1, "behaviour": True, "briefs": 1, "releases": []}
    return {**base, **changes}


class JourneyTest(unittest.TestCase):
    def test_the_first_step_not_done_is_the_stage_and_the_only_next_action(self):
        j = app_journey(1, **facts(owner="", purpose=""))
        self.assertEqual([s.title for s in j.steps], list(JOURNEY))
        self.assertEqual(j.stage, "Choose application")
        self.assertEqual(j.next.action, "Add owner and purpose")
        self.assertEqual([s.state for s in j.steps].count("next"), 1)
        self.assertEqual(j.missing, ["Owner", "Purpose"])

    def test_a_discovered_application_asks_for_a_scope_decision(self):
        j = app_journey(1, **facts(status="discovered"))
        self.assertEqual(j.next.action, "Decide scope")
        self.assertEqual(j.next.href, "/apps/1#choose")

    def test_examples_need_an_identity_then_a_machine(self):
        j = app_journey(4, **facts(examples=0, identities=0, machines=0))
        self.assertEqual(j.stage, "Add examples")
        self.assertEqual(j.next.href, "/apps/4/setup#identity")
        self.assertIn("Identity (to request a recording)", j.missing)
        self.assertEqual(app_journey(4, **facts(examples=0, machines=0)).next.href, "/machines")
        self.assertEqual(app_journey(4, **facts(examples=0)).next.action, "Request a recording")

    def test_build_then_verify_then_open_follow_the_release_state(self):
        self.assertEqual(app_journey(1, **facts(behaviour=False)).stage, "Review required behaviour")
        self.assertEqual(app_journey(1, **facts(briefs=0)).next.action, "Freeze builder package")
        self.assertEqual(app_journey(1, **facts()).next.action, "Register a release")
        built = app_journey(1, **facts(releases=[{"id": 7, "version": "1.0.0"}]))
        self.assertEqual(built.stage, "Verify")  # no release state yet: not verified
        self.assertEqual(built.next.href, "/releases/7")
        tested = app_journey(1, **facts(releases=[{"id": 7, "version": "1.0.0", "state": "Tested"}]))
        self.assertEqual(tested.stage, "Open or download")
        done = app_journey(1, **facts(releases=[{"id": 7, "version": "1.0.0", "state": "published"}]))
        self.assertIsNone(done.next)
        self.assertEqual(done.stage, "Ready to open or download")

    def test_a_not_replicated_application_is_out_of_the_journey(self):
        j = app_journey(1, **facts(status="not_replicated"))
        self.assertTrue(j.out_of_scope)
        self.assertIsNone(j.next)
        self.assertEqual(j.stage, "Not replicated")


class JourneyPagesTest(unittest.TestCase):
    """The journey as the API gives it to the web app: the catalog's cards and the setup."""

    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.app = create_app(new_database(self.work.name))
        self.client = Client(self.app)

    def ok(self, response, status=200):
        self.assertEqual(response.status_code, status, response.text[:800])
        return response.json()

    def test_catalog_card_shows_owner_purpose_stage_missing_and_one_action(self):
        self.ok(self.client.post("/api/v1/apps", json={"name": "Orders", "owner": "Ops team",
                                                       "purpose": "Order to invoice demo"}), 201)
        [card] = self.ok(self.client.get("/api/v1/apps"))["apps"]
        self.assertIn("Ops team", card["owner"])
        self.assertEqual(card["purpose"], "Order to invoice demo")
        self.assertEqual(card["status_label"], STATUS_LABELS["known"])
        journey = card["journey"]
        self.assertEqual(journey["stage"], "Add examples")
        self.assertIn("Identity (to request a recording)", journey["missing"])
        # one next action
        self.assertEqual([s["state"] for s in journey["steps"]].count("next"), 1)
        self.assertEqual(journey["next"]["href"], "/apps/1/setup#identity")

    def test_application_page_forms_keep_each_others_fields(self):
        self.ok(self.client.post("/api/v1/apps", json={"name": "Orders"}), 201)
        self.ok(self.client.patch("/api/v1/apps/1", json={"git_url": "https://git.example.test/orders.git"}))
        self.ok(self.client.patch("/api/v1/apps/1", json={"owner": "Ops team", "purpose": "Demo"}))
        self.ok(self.client.post("/api/v1/apps/1/scenarios", json={"name": "Show the order screen."}), 201)
        setup = self.ok(self.client.get("/api/v1/apps/1/setup"))
        self.assertEqual(setup["app"]["git_url"], "https://git.example.test/orders.git")
        detail = self.ok(self.client.get("/api/v1/apps/1"))
        self.assertIn("Ops team", detail["owner"])
        self.assertEqual(detail["purpose"], "Demo")
        self.assertEqual(detail["git_url"], "https://git.example.test/orders.git")  # kept by the other change
        scenarios = self.ok(self.client.get("/api/v1/apps/1/scenarios"))["scenarios"]
        self.assertEqual([s["name"] for s in scenarios], ["Show the order screen."])
        build = next(s for s in detail["journey"]["steps"] if s["id"] == "build")
        self.assertIn("Preview the builder package", build["hint"])
        self.assertEqual(build["action"], "Freeze builder package")

    def test_identity_lives_in_setup(self):
        self.ok(self.client.post("/api/v1/apps", json={"name": "Orders"}), 201)
        self.ok(self.client.post("/api/v1/apps/1/identities", json={"key": "CaseDesk.exe"}), 201)
        setup = self.ok(self.client.get("/api/v1/apps/1/setup"))
        self.assertIn("exe:casedesk.exe", [i["key"] for i in setup["identities"]])


class JourneyThroughReleaseStatesTest(unittest.TestCase):
    """Build, Verify and Open or download follow the release states of the real portal."""

    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        root = Path(self.work.name)
        self.database = new_database(root)
        self.client = Client(create_app(self.database))
        self.ok(self.client.post("/api/v1/apps", json={"name": "Billing"}), 201)
        recording = self.ok(self.client.post("/api/v1/recordings", data={"name": "Invoice"}, files={"log": (
            "steps.jsonl", b'{"seq":1,"action":"submit","url":"http://billing.local/"}\n', "application/x-ndjson")}), 201)
        rid = recording["id"]
        self.ok(self.client.put(f"/api/v1/recordings/{rid}/assignments/1",
                                json={"events": [eid(rid, 1)], "notes": "Invoice is saved"}))
        self.ok(self.client.post("/api/v1/apps/1/scenarios", json={
            "name": "Invoice is saved", "expected": "The invoice has its number.", "events": [f"{rid}#{eid(rid, 1)}"]}), 201)
        with ZipFile(io.BytesIO(freeze(self.client, 1).content)) as archive:
            revision = archive.read("BUILD_TASK.md").decode().split("`")[1]
        repo = root / "demo"
        repo.mkdir()
        git = lambda *args: subprocess.check_output(["git", "-C", str(repo), *args], text=True)  # noqa: E731
        git("init", "-q")
        git("config", "user.email", "demo@example.invalid")
        git("config", "user.name", "TwinLab2 test")
        (repo / "run.py").write_text('print("ready")\n', encoding="utf-8")
        git("add", "run.py")
        git("commit", "-qm", "Release")
        self.commit = add_release(self.database, 1, "1.0", revision, repo, "HEAD")

    def ok(self, response, status=200):
        self.assertEqual(response.status_code, status, response.text[:800])
        return response.json()

    def card(self):
        return self.ok(self.client.get("/api/v1/apps"))["apps"][0]

    def test_tested_then_published(self):
        self.ok(self.client.patch("/api/v1/apps/1", json={"owner": "Finance IT", "purpose": "Invoice demo"}))
        card = self.card()
        self.assertEqual(card["journey"]["stage"], "Verify")
        self.assertEqual(card["journey"]["next"]["action"], "Verify release")
        self.assertIsNone(card["tested_release"])
        self.ok(self.client.post("/api/v1/releases/1/tests", json={
            "commit": self.commit, "results": {"scenario-1": "pass"}, "platform": "Windows 11, Python 3.13",
            "launch": "python run.py", "reset_result": "pass", "reset_notes": "seed restored"}))
        card = self.card()
        self.assertEqual(card["journey"]["stage"], "Open or download")
        self.assertEqual(card["journey"]["next"]["action"], "Publish release")
        self.assertEqual(card["tested_release"]["state"], "tested")  # Download tested release
        self.ok(self.client.post("/api/v1/releases/1/publish"))
        card = self.card()
        self.assertEqual(card["journey"]["stage"], "Ready to open or download")
        self.assertIsNone(card["journey"]["next"])
        self.assertEqual(card["tested_release"]["state"], "published")
        self.assertIn("Ready", self.ok(self.client.get("/api/v1/apps/1"))["journey"]["stage"])


