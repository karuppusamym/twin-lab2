"""The owner-facing overviews against the demo portal (tools/demo_portal.py): a recording's
workboard (one row per application, one action each, the swim-lane timeline, shared hosts), the
scope triage (ranked suggestions, bulk and reversible decisions), the role-aware home, registering
a release from a build session or a commit, and what each role may not do (403s): add an
application as a viewer or builder, read the storage location, see who paired a machine, see
providers that are not ready or the stand-in provider outside a demo."""

import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from twinlab2 import auth
from support import Client

TOOL = Path(__file__).resolve().parents[2] / "tools" / "demo_portal.py"
PASSWORDS = {"alice": "alice-demo", "bob": "bob-demo", "vera": "vera-demo"}


def load_demo():
    spec = importlib.util.spec_from_file_location("demo_portal_for_workboard", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DemoCase(unittest.TestCase):
    """One seeded demo portal for the class; each test signs in its own clients."""

    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory()
        # the portal's default rule for adding applications (conftest relaxes it for other tests)
        cls.env = mock.patch.dict(os.environ, {"TWINLAB2_APP_CREATORS": "owners"})
        cls.env.start()
        cls.app, _world, _ctx, _database = load_demo().build(cls.work.name)
        alice = cls.sign_in("alice")
        catalog = alice.get("/api/v1/apps").json()
        cls.ids = {a["name"]: a["id"] for a in catalog["apps"]}
        cls.recording = catalog["recordings"][0]["id"]

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.work.cleanup()

    @classmethod
    def sign_in(cls, user):
        client = Client(cls.app, remote="10.0.0.9")
        response = client.post("/api/v1/session", json={"user": user, "password": PASSWORDS[user]})
        assert response.status_code == 200, response.text
        return client

    def ok(self, response, status=200):
        self.assertEqual(response.status_code, status, response.text[:800])
        return response.json()


class WorkboardTest(DemoCase):
    def test_one_row_per_application_with_one_action_each(self):
        board = self.ok(self.sign_in("alice").get(f"/api/v1/recordings/{self.recording}/workboard"))
        rows = {r["name"]: r for r in board["apps"]}
        self.assertEqual(len(rows), 13, sorted(rows))
        self.assertEqual(board["counts"]["needs_decision"], 6)
        # undecided applications come first, and their action is the triage
        self.assertEqual({r["scope"]["status"] for r in board["apps"][:6]}, {"discovered"})
        contoso = rows["Billing (Contoso Ltd)"]
        self.assertEqual(contoso["scope"]["label"], "Needs a scope decision")
        self.assertEqual(contoso["action"]["label"], "Decide scope")
        self.assertEqual(contoso["action"]["href"], f"/recordings/{self.recording}/triage#app-{contoso['app_id']}")
        self.assertEqual(contoso["identity"]["display"], "ContosoBilling.exe")
        self.assertEqual(contoso["identity"]["surface"], "desktop")
        billing = rows["Billing"]
        self.assertEqual(billing["identity"]["display"], "erp.example.test/billing")
        self.assertGreater(billing["steps"], 0)
        self.assertTrue(billing["reviewed"])
        self.assertEqual(billing["scenarios"], 2)
        # frozen as revision 1; the demo set the repository afterwards, so the draft differs
        self.assertEqual((billing["package"]["state"], billing["package"]["rev"]), ("draft", 1))
        self.assertFalse(billing["package"]["up_to_date"])
        self.assertEqual(billing["package"]["blockers"], 0)
        self.assertEqual(billing["release"]["state"], "published")
        self.assertEqual(billing["build"]["state"], "ready_for_verification")
        # Accounts Receivable has a frozen package but no demo repository: the action says so
        receivable = rows["Accounts Receivable"]
        self.assertEqual(receivable["package"]["rev"], 1)
        self.assertEqual(receivable["action"]["label"], "Set repository")
        self.assertEqual(receivable["stage"], "Connect repository")
        self.assertEqual(board["download_href"], f"/api/v1/recordings/{self.recording}/packages/download")
        for row in board["apps"]:
            self.assertIsNotNone(row["action"], row["name"])

    def test_timeline_is_a_lane_per_application_over_time(self):
        board = self.ok(self.sign_in("alice").get(f"/api/v1/recordings/{self.recording}/workboard"))
        timeline = board["timeline"]
        self.assertEqual(timeline["axis"], "time")
        self.assertEqual(len(timeline["lanes"]), 13)
        self.assertEqual(sum(lane["steps"] for lane in timeline["lanes"]), timeline["total"])
        self.assertLessEqual(timeline["start"], timeline["end"])
        surfaces = {lane["surface"] for lane in timeline["lanes"]}
        self.assertEqual(surfaces, {"web", "desktop", "mainframe"})
        for lane in timeline["lanes"]:
            for run in lane["runs"]:
                self.assertLessEqual(run["from_index"], run["to_index"])
                self.assertTrue(1 <= len(run["samples"]) <= 5)
        # the shared hosts, told apart by the longest path prefix
        hosts = {h["host"]: h for h in board["shared_hosts"]}
        self.assertEqual({a["name"] for a in hosts["erp.example.test"]["apps"]}, {"Billing", "Billing Admin"})
        self.assertEqual([a["path"] for a in hosts["fin.example.test"]["apps"]][0], "/finance/billing")
        self.assertIn("longest path prefix", hosts["fin.example.test"]["text"])

    def test_a_builder_sees_package_and_build_but_no_evidence(self):
        bob = self.sign_in("bob")
        # bob is not a reviewer of the recording (restricted evidence)
        self.assertEqual(bob.get(f"/api/v1/recordings/{self.recording}/workboard").status_code, 403)
        self.assertEqual(self.sign_in("vera").get(f"/api/v1/recordings/{self.recording}/workboard").status_code, 403)


class TriageTest(DemoCase):
    def test_one_card_per_undecided_application_with_ranked_suggestions(self):
        triage = self.ok(self.sign_in("alice").get("/api/v1/triage"))
        cards = {c["name"]: c for c in triage["apps"]}
        self.assertEqual(len(cards), 6, sorted(cards))
        contoso = cards["Billing (Contoso Ltd)"]
        self.assertEqual(contoso["surface"], "desktop")
        self.assertGreater(contoso["steps"], 0)
        self.assertTrue(contoso["screens"] or contoso["controls"])
        suggested = [s["name"] for s in contoso["suggestions"]]
        self.assertIn("Billing", suggested)
        self.assertIn("Billing (billing.exe)", suggested)
        reasons = {s["name"]: s["reasons"] for s in contoso["suggestions"]}
        self.assertIn("same name", reasons["Billing"])
        # one shared word ("Nord") is not a similar name
        self.assertEqual(cards["Kasse – Filiale Nord"]["suggestions"], [])
        # "discovered" is never offered as a decision
        labels = {label["value"]: label for label in triage["labels"]}
        self.assertIsNone(labels["discovered"]["decision"])
        self.assertEqual(labels["not_replicated"]["label"], "Not replicated (used only)")
        self.assertEqual(self.ok(self.sign_in("alice").get(f"/api/v1/triage?recording_id={self.recording}"))
                         ["recording"]["id"], self.recording)



class TriageDecisionTest(DemoCase):
    def test_bulk_decisions_are_all_or_none_and_reversible(self):
        alice = self.sign_in("alice")
        a, b = self.ids["orders.example.test"], self.ids["127.0.0.1:8081"]
        outcome = self.ok(alice.post("/api/v1/triage/decisions", json={"decisions": [
            {"app_id": a, "decision": "replicate"}, {"app_id": b, "decision": "not_replicated"}]}))
        self.assertEqual([x["status"] for x in outcome["apps"]], ["known", "not_replicated"])
        self.assertIn("reverse", outcome["message"])
        self.assertEqual(alice.get(f"/api/v1/apps/{b}").json()["journey"]["stage"], "Not replicated")
        # reversed
        self.ok(alice.post("/api/v1/triage/decisions", json={"decisions": [
            {"app_id": a, "decision": "not_replicated"}, {"app_id": b, "decision": "replicate"}]}))
        self.assertEqual(alice.get(f"/api/v1/apps/{b}").json()["status"], "known")
        # a stale revision refuses the whole batch
        current = alice.get(f"/api/v1/apps/{a}").json()["revision"]
        stale = alice.post("/api/v1/triage/decisions", json={"decisions": [
            {"app_id": b, "decision": "not_replicated"}, {"app_id": a, "decision": "replicate", "expected_revision": current - 1}]})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(alice.get(f"/api/v1/apps/{b}").json()["status"], "known")
        self.assertEqual(alice.post("/api/v1/triage/decisions", json={"decisions": [
            {"app_id": a, "decision": "discovered"}]}).status_code, 400)
        # a viewer decides nothing
        vera = self.sign_in("vera")
        self.assertEqual(vera.post("/api/v1/triage/decisions", json={"decisions": [
            {"app_id": self.ids["Billing"], "decision": "not_replicated"}]}).status_code, 403)
        self.assertEqual(vera.get("/api/v1/triage").json()["apps"], [])

    def test_the_app_page_does_not_offer_discovered(self):
        detail = self.ok(self.sign_in("alice").get(f"/api/v1/apps/{self.ids['Kasse – Filiale Nord']}"))
        self.assertEqual([s["value"] for s in detail["statuses"]], ["known", "not_replicated"])
        self.assertEqual(detail["journey"]["next"]["href"], f"/triage#app-{detail['id']}")


class HomeTest(DemoCase):
    def test_each_role_gets_its_own_home(self):
        vera = self.ok(self.sign_in("vera").get("/api/v1/home"))
        self.assertEqual(vera["role"], "viewer")
        self.assertEqual([d["name"] for d in vera["published"]], ["Billing"])
        self.assertEqual((vera["ready_to_build"], vera["workboards"], vera["triage_count"]), ([], [], 0))
        self.assertFalse(vera["can_create_apps"])
        bob = self.ok(self.sign_in("bob").get("/api/v1/home"))
        self.assertEqual(bob["role"], "builder")
        self.assertEqual([r["name"] for r in bob["ready_to_build"]], ["Billing"])
        self.assertEqual(bob["ready_to_build"][0]["build"]["state"], "ready_for_verification")
        alice = self.ok(self.sign_in("alice").get("/api/v1/home"))
        self.assertEqual(alice["role"], "admin")
        self.assertEqual(alice["triage_count"], 6)
        self.assertEqual([(w["id"], w["needs_decision"]) for w in alice["workboards"]][0], (self.recording, 6))
        self.assertIn("Accounts Receivable", [r["name"] for r in alice["ready_to_build"]])
        session = self.sign_in("bob").get("/api/v1/session").json()
        self.assertEqual((session["home_role"], session["can_create_apps"]), ("builder", False))


class RoleGatingTest(DemoCase):
    def test_a_viewer_or_builder_cannot_add_an_application(self):
        for user in ("vera", "bob"):
            refused = self.sign_in(user).post("/api/v1/apps", json={"name": f"Made by {user}"})
            self.assertEqual(refused.status_code, 403, user)
            self.assertEqual(refused.json()["code"], "role")
        self.ok(self.sign_in("alice").post("/api/v1/apps", json={"name": "Made by alice"}), 201)

    def test_who_may_add_applications_is_a_setting(self):
        with mock.patch.dict(os.environ, {"TWINLAB2_APP_CREATORS": "anyone"}):
            self.assertEqual(auth.Settings.from_env().app_creators, "anyone")
        with mock.patch.dict(os.environ, {"TWINLAB2_APP_CREATORS": "everyone"}):
            with self.assertRaises(auth.ConfigError):
                auth.Settings.from_env()
        # what each rule lets a user with no owner role do (the demo portal runs with `owners`)
        for rule, expected in (("anyone", True), ("admins", False), ("owners", False)):
            with mock.patch.object(auth, "_settings", lambda rule=rule: auth.Settings(app_creators=rule)), \
                    mock.patch.object(auth, "_db", lambda: _NoOwner()):
                self.assertEqual(auth.may_create_apps("vera", False), expected, rule)
                self.assertTrue(auth.may_create_apps("root", True), rule)

    def test_setup_and_machines_hide_sensitive_fields_from_non_admins(self):
        self.assertIsNone(self.ok(self.sign_in("vera").get("/api/v1/setup"))["storage"])
        self.assertIsInstance(self.ok(self.sign_in("alice").get("/api/v1/setup"))["storage"], dict)
        # machines: a user sees only those they paired (an admin all)
        self.assertEqual(self.ok(self.sign_in("vera").get("/api/v1/machines"))["machines"], [])
        self.assertTrue(self.ok(self.sign_in("alice").get("/api/v1/machines"))["machines"])

    def test_only_ready_providers_and_the_stand_in_only_on_a_demo(self):
        billing = self.ids["Billing"]
        bob = self.sign_in("bob")
        names = [p["name"] for p in self.ok(bob.get(f"/api/v1/apps/{billing}/builds"))["providers"]]
        self.assertEqual(names, ["fake"])  # Devin and Claude are not set up here: hidden from a builder
        alice = self.sign_in("alice")
        everyone = {p["name"]: p["ready"] for p in self.ok(alice.get(f"/api/v1/apps/{billing}/builds"))["providers"]}
        self.assertIn("devin", everyone)  # an admin sees them, with why they are not ready
        self.assertIsNotNone(everyone["devin"])
        with mock.patch.dict(os.environ, {"TWINLAB2_DEMO": "0"}):
            names = [p["name"] for p in self.ok(alice.get(f"/api/v1/apps/{billing}/builds"))["providers"]]
        self.assertNotIn("fake", names)


class _NoOwner:
    def execute(self, *_args):
        class Rows:
            def fetchone(self):
                return None
        return Rows()


class ReleaseTest(DemoCase):
    def test_a_release_from_a_build_session_or_a_commit(self):
        billing = self.ids["Billing"]
        bob = self.sign_in("bob")
        build = self.ok(bob.get(f"/api/v1/apps/{billing}/builds"))["sessions"][0]
        registered = self.ok(bob.post(f"/api/v1/apps/{billing}/releases", json={"build_id": build["id"]}), 201)
        self.assertFalse(registered["created"])  # the demo verified this commit already
        self.assertEqual(registered["release"]["commit_sha"], build["head_sha"])
        self.assertEqual(registered["href"], f"/releases/{registered['release']['id']}")
        again = self.ok(bob.post(f"/api/v1/apps/{billing}/releases", json={"sha": build["head_sha"]}), 201)
        self.assertEqual(again["release"]["id"], registered["release"]["id"])
        # a viewer cannot; an application without a demo repository is told to set one
        self.assertEqual(self.sign_in("vera").post(f"/api/v1/apps/{billing}/releases",
                                                   json={"build_id": build["id"]}).status_code, 403)
        receivable = self.ids["Accounts Receivable"]
        refused = self.sign_in("alice").post(f"/api/v1/apps/{receivable}/releases", json={"sha": "a" * 40})
        self.assertEqual(refused.status_code, 400)
        self.assertIn("demo repository", refused.json()["detail"])
        other = self.sign_in("alice").post(f"/api/v1/apps/{receivable}/releases", json={"build_id": build["id"]})
        self.assertEqual(other.status_code, 400)


class ReadinessCacheTest(unittest.TestCase):
    """The workboard keeps each application's draft readiness (readiness_cache): on the scale demo
    (tools/demo_portal.py --scale: 50 applications, a 2,000-event recording assigned to them), a
    second load builds no draft; each write that changes an application's draft rebuilds that
    one only, and what the board shows is what a fresh computation shows."""

    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory()
        cls.env = mock.patch.dict(os.environ, {"TWINLAB2_APP_CREATORS": "owners"})
        cls.env.start()
        cls.app, _world, _ctx, cls.database = load_demo().build(cls.work.name, scale=True)
        cls.alice = DemoCase.sign_in.__func__(cls, "alice")
        catalog = cls.alice.get("/api/v1/apps?limit=100").json()
        cls.recording = next(r["id"] for r in catalog["recordings"] if "2000 events" in r["name"])
        ids = cls.alice.get(f"/api/v1/recordings/{cls.recording}/events?ids=true&limit=1").json()["ids"]
        assert len(ids) == 2000
        scale = [a for a in catalog["apps"] if a["name"].startswith("Scale app")]
        cls.events = {}
        for n, item in enumerate(scale):
            chunk = ids[n * 40:(n + 1) * 40]
            cls.events[item["id"]] = chunk
            saved = cls.alice.put(f"/api/v1/recordings/{cls.recording}/assignments/{item['id']}",
                                  json={"events": chunk, "expected_revision": 0})
            assert saved.status_code == 200, saved.text
            if item["status"] == "known":
                made = cls.alice.post(f"/api/v1/apps/{item['id']}/scenarios", json={
                    "name": "A record is saved", "expected": "The record is listed.",
                    "events": [f"{cls.recording}#{e}" for e in chunk[:5]]})
                assert made.status_code in (200, 201), made.text
        cls.known = [a["id"] for a in scale if a["status"] == "known"]

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.work.cleanup()

    def setUp(self):
        from twinlab2 import readiness_cache

        self.cache = readiness_cache
        self.cache.clear()
        self.board()  # warm

    def ok(self, response, status=200):
        self.assertEqual(response.status_code, status, response.text[:800])
        return response.json()

    def board(self):
        """(the workboard, the applications whose draft it built)."""
        from twinlab2 import buildpack

        built = []
        real = buildpack.draft

        def counted(db, app_id, *args, **kwargs):
            built.append(app_id)
            return real(db, app_id, *args, **kwargs)

        with mock.patch.object(buildpack, "draft", counted):
            board = self.ok(self.alice.get(f"/api/v1/recordings/{self.recording}/workboard"))
        return board, built

    def packages(self, board):
        return {r["app_id"]: r["package"] for r in board["apps"] if r["package"] is not None}

    def assert_fresh(self, board):
        """The board's readiness is what an empty cache computes."""
        self.cache.clear()
        fresh, _ = self.board()
        self.assertEqual(self.packages(board), self.packages(fresh))

    def test_a_second_load_builds_no_draft(self):
        import time

        self.cache.clear()
        started = time.perf_counter()
        first, built = self.board()
        cold = time.perf_counter() - started
        self.assertGreaterEqual(len(self.known), 20)
        self.assertLessEqual(set(self.known), set(built))  # every replicated application, once
        self.assertEqual(len(built), len(set(built)))
        started = time.perf_counter()
        second, again = self.board()
        warm = time.perf_counter() - started
        self.assertEqual(again, [])
        self.assertEqual(first, second)
        rows = {r["app_id"]: r for r in second["apps"]}
        self.assertEqual(rows[self.known[0]]["package"]["state"], "draft")
        self.assertIsNotNone(rows[self.known[0]]["package"]["blockers"])
        print(f"\nworkboard of {len(second['apps'])} applications: {cold:.2f}s cold, {warm:.2f}s cached")

    def test_each_write_rebuilds_only_the_application_it_changes(self):
        alice = self.alice
        a, b, c, d, e = self.known[:5]
        # a scenario changed
        scenario = self.ok(alice.get(f"/api/v1/apps/{a}/scenarios"))["scenarios"][0]
        self.ok(alice.put(f"/api/v1/apps/{a}/scenarios/{scenario['id']}",
                          json={"name": scenario["name"], "expected": "", "events": scenario["events"]}))
        board, built = self.board()
        self.assertEqual(built, [a])
        self.assert_fresh(board)
        # an assignment saved with fewer events
        current = self.ok(alice.get(f"/api/v1/recordings/{self.recording}/assignments/{b}"))
        self.ok(alice.put(f"/api/v1/recordings/{self.recording}/assignments/{b}",
                          json={"events": self.events[b][:10], "expected_revision": current["revision"]}))
        self.assertEqual(self.board()[1], [b])
        # the application's own row (its demo repository)
        self.ok(alice.patch(f"/api/v1/apps/{c}", json={"git_url": "https://github.com/example/scale-c"}))
        self.assertEqual(self.board()[1], [c])
        # out of scope and back: no draft while not replicated, a new one after
        self.ok(alice.post("/api/v1/triage/decisions",
                           json={"decisions": [{"app_id": d, "decision": "not_replicated"}]}))
        board, built = self.board()
        self.assertEqual(built, [])
        self.assertIsNone({r["app_id"]: r for r in board["apps"]}[d]["package"])
        self.ok(alice.post("/api/v1/triage/decisions", json={"decisions": [{"app_id": d, "decision": "replicate"}]}))
        self.assertEqual(self.board()[1], [d])
        # frozen: the frozen revision is read on every load; the draft is the same one
        draft = self.ok(alice.get(f"/api/v1/apps/{e}/package-draft",
                                  params={"override_reason": "Synthetic scale data: gaps are expected."}))
        self.ok(alice.post(f"/api/v1/apps/{e}/packages", json={
            "sha256": draft["sha256"], "approve": True, "summary": "scale",
            "override_reason": "Synthetic scale data: gaps are expected."}), 201)
        board, built = self.board()
        package = {r["app_id"]: r for r in board["apps"]}[e]["package"]
        self.assertEqual(package["rev"], 1)
        self.assert_fresh(board)

    def test_a_draft_that_could_not_be_built_is_tried_again(self):
        from twinlab2 import buildpack

        app_id = self.known[1]
        self.cache.clear()
        real = buildpack.draft

        def broken(db, wanted, *args, **kwargs):
            if wanted == app_id:
                raise OSError("storage unavailable")
            return real(db, wanted, *args, **kwargs)

        with mock.patch.object(buildpack, "draft", broken):
            board = self.ok(self.alice.get(f"/api/v1/recordings/{self.recording}/workboard"))
        row = {r["app_id"]: r for r in board["apps"]}[app_id]
        self.assertIn("storage unavailable", row["package"]["reason"])
        board, built = self.board()  # not kept: built again, and it works now
        self.assertEqual(built, [app_id])
        self.assertNotIn("could not be built", {r["app_id"]: r for r in board["apps"]}[app_id]["package"]["reason"])

    def test_a_write_from_another_process_is_noticed(self):
        """The stamp of the rows a draft reads catches what this process did not write (the
        worker, a second portal process, a script)."""
        from contextlib import closing

        from twinlab2.db import connect

        app_id = self.known[-1]
        with closing(connect(self.database)) as other:
            with other:
                other.execute("UPDATE scenarios SET expected = 'Changed elsewhere.', revision = revision + 1 "
                              "WHERE app_id = ?", (app_id,))
        board, built = self.board()
        self.assertEqual(built, [app_id])
        self.assert_fresh(board)
        self.assertEqual(self.board()[1], [])

if __name__ == "__main__":
    unittest.main()
