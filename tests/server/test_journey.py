"""The journey: Choose application -> Add examples -> Review required behaviour -> Build ->
Verify -> Open or download, and the one next action the pages show."""

import re
from pathlib import Path
import tempfile
import unittest

from twinlab2.journey import JOURNEY, STATUS_LABELS, app_journey
from twinlab2.portal import create_app
from support import new_database


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
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.app = create_app(new_database(self.work.name))
        self.app.testing = True
        self.client = self.app.test_client()
        self.client.get("/")
        with self.client.session_transaction() as session:
            self.token = session["form_token"]

    def post(self, path, **values):
        return self.client.post(path, data={"token": self.token, **values})

    def test_catalog_card_shows_owner_purpose_stage_missing_and_one_action(self):
        self.post("/apps", name="Orders", owner="Ops team", purpose="Order to invoice demo")
        page = self.client.get("/").data.decode()
        for text in ("Ops team", "Order to invoice demo", "Add examples", STATUS_LABELS["known"],
                     "Identity (to request a recording)"):
            self.assertIn(text, page)
        card = page.split('<article class="card">')[1].split("</article>")[0]
        self.assertEqual(len(re.findall(r'class="button"', card)), 1)
        self.assertIn('href="/apps/1/setup#identity"', card)

    def test_application_page_forms_keep_each_others_fields(self):
        self.post("/apps", name="Orders")
        self.post("/apps/1", git_url="https://git.example.test/orders.git", next="setup")
        self.post("/apps/1", owner="Ops team", purpose="Demo")
        self.post("/apps/1/scenarios", name="Show the order screen.")
        setup = self.client.get("/apps/1/setup").data.decode()
        self.assertIn("https://git.example.test/orders.git", setup)
        page = self.client.get("/apps/1").data.decode()
        self.assertIn("Ops team", page)
        self.assertIn("Show the order screen.", page)
        self.assertNotIn("git.example.test", page)  # Git details are in Setup / Advanced
        self.assertIn("Preview and approve a builder package", page)

    def test_identity_and_machines_live_in_setup(self):
        self.post("/apps", name="Orders")
        response = self.post("/apps/1/identity", key="CaseDesk.exe")
        self.assertEqual(response.location, "/apps/1/setup")
        self.assertIn("exe:casedesk.exe", self.client.get("/apps/1/setup").data.decode())
        nav = self.client.get("/").data.decode().split("</nav>")[0]
        self.assertNotIn("/machines", nav)
        self.assertIn("Setup / Advanced", nav)
        self.assertIn("/machines", self.client.get("/setup").data.decode())

    def test_request_a_recording_wording(self):
        page = self.client.get("/sessions").data.decode()
        self.assertIn("Request a recording", page)
        self.assertNotIn("Offer a session", page)
        self.assertIn("pair a recorder machine", page)


class JourneyThroughReleaseStatesTest(unittest.TestCase):
    """Build, Verify and Open or download follow the release states of the real portal."""
    from test_release_states import ReleaseStatesTest as _R
    setUp, git, post, evidence = _R.setUp, _R.git, _R.post, _R.evidence
    del _R

    def card(self):
        page = self.client.get("/").data.decode()
        return page.split('<article class="card">')[1].split("</article>")[0]

    def test_tested_then_published(self):
        self.post(self.app_path, owner="Finance IT", purpose="Invoice demo")
        self.assertIn("<dd>Verify</dd>", self.card())
        self.assertIn(">Verify release<", self.card())
        self.assertEqual(self.post("/releases/1/test", **self.evidence()).status_code, 302)
        card = self.card()
        self.assertIn("<dd>Open or download</dd>", card)
        self.assertIn(">Publish release<", card)
        self.assertIn("Download tested release", card)  # the release session's buttons stay
        self.assertEqual(self.post("/releases/1/publish").status_code, 302)
        card = self.card()
        self.assertIn("<dd>Ready to open or download</dd>", card)
        self.assertNotIn(">Publish release<", card)
        self.assertIn("Download tested release", card)
        self.assertIn("Ready", self.client.get(self.app_path).data.decode())
