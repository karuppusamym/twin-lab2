"""The /api/v1 contract: every operation of api/openapi.yaml against the real portal.

One catalog is built once for the class, behind the OAuth proxy (`X-Forwarded-User`):

- alice owns Billing Desk (1) and CaseDesk (2); bob is a builder and vera a viewer of Billing
  Desk; carol is signed in with no role; root is a portal admin;
- a paired recorder sent one recording that holds both applications (web and desktop), and a
  steps.jsonl was imported;
- Billing Desk has a reviewed assignment, a scenario, an attachment, a frozen package, a build
  session with the fake provider (tests/server/fake_provider.py, no network) and a release that
  was verified against a fake GitHub and a fake deployment, then published.

Every response goes through support.Client, which checks it against the spec's schemas
(tests/contract.py). `test_every_operation_is_covered` fails when an operation of the spec was
not called with a successful status; the role tests check each role's refusals.
"""

from contextlib import closing
import io
import json
import os
import tempfile
import unittest
from unittest import mock
from zipfile import ZipFile

import contract
import fake_provider
from fakes import FakeGitHub
from support import Client, connect, new_database, preview, wait_verification
from test_registry import DESK, WEB, references
from test_verification import REPO, SHA, Deployment, FakeGitHub as ChecksGitHub, FakeTwinLab, run
from twinlab2 import auth, builds, rest, verification, worker
from twinlab2.portal import create_app
from twinlab2_contract import recorder_contract

CV = recorder_contract.CONTRACT_VERSION
RECORDING = "ws-20261119T100000Z-0a1b2c3d"
IMPORTED_LOG = (b'{"seq":1,"action":"submit","url":"http://billing.local/"}\n'
                b'{"seq":2,"action":"click","url":"http://billing.local/invoices"}\n')
AUTOMATION_YAML = b"id: invoice-bot\nname: Invoice bot\ntype: rpa\ntouches:\n  - app: Billing Desk\n  - app: Unknown\n"


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory()
        cls.database = new_database(cls.work.name)
        cls.builds_github = FakeGitHub().start()
        cls.builds_github.add_repo(REPO, sha="b" * 40)
        cls.checks_github = ChecksGitHub()
        cls.deployment = Deployment(SHA)
        cls.twin = FakeTwinLab()
        cls.env = mock.patch.dict(os.environ, {
            "TWINLAB2_TRUST_PROXY_USER": "1", "TWINLAB2_ADMINS": "root",
            "TWINLAB2_GITHUB_TOKEN": cls.builds_github.token, "TWINLAB2_GITHUB_API": cls.builds_github.url,
            "TWINLAB2_WORKSPACES": os.path.join(cls.work.name, "workspaces"), "TWINLAB2_DEV_WORKER": "0",
            # the fake provider is a stand-in offered only on a demo portal
            "TWINLAB2_DEMO": "1"})
        cls.env.start()
        for name in ("TWINLAB2_PASSWORD",):
            os.environ.pop(name, None)
        cls.world = fake_provider.reset()
        cls.app = create_app(cls.database)
        cls.app.config.update(GITHUB=verification.GitHub(token="test-token", api=cls.checks_github.url),
                              TWINLAB_RUNNER=cls.twin)
        cls.client = Client(cls.app, "alice")
        cls.build_world()

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.builds_github.stop()
        cls.checks_github.close()
        cls.deployment.close()
        cls.work.cleanup()

    # -- the catalog ---------------------------------------------------------------------------

    @classmethod
    def ok(cls, response, status=200):
        assert response.status_code == status, (response.status_code, response.text[:1500])
        return response.json() if response.content and "json" in response.headers.get("content-type", "") else None

    @classmethod
    def as_(cls, user):
        return cls.client.as_user(user)

    @classmethod
    def build_world(cls):
        alice, root = cls.as_("alice"), cls.as_("root")
        ok = cls.ok
        cls.billing = ok(alice.post("/api/v1/apps", json={"name": "Billing Desk", "purpose": "Invoices"}), 201)["id"]
        cls.casedesk = ok(alice.post("/api/v1/apps", json={"name": "CaseDesk", "owner": "dora"}), 201)["id"]
        ok(alice.patch(f"/api/v1/apps/{cls.billing}", json={"git_url": f"https://github.com/{REPO}.git"}))
        ok(alice.put(f"/api/v1/apps/{cls.billing}/roles/bob", json={"role": "builder"}))
        ok(alice.put(f"/api/v1/apps/{cls.billing}/roles/vera", json={"role": "viewer"}))
        ok(alice.post(f"/api/v1/apps/{cls.billing}/identities", json={"key": WEB.removeprefix("web:")}), 201)
        ok(alice.post(f"/api/v1/apps/{cls.casedesk}/identities", json={"key": DESK}), 201)
        # a paired recorder sends one recording that holds both applications
        code = ok(alice.post("/api/v1/machines/pairing-codes"), 201)["code"]
        paired = cls.client.http.post("/api/agent/pair", json={"contract_version": CV, "code": code, "name": "PC-1"})
        assert paired.status_code == 200, paired.text
        cls.machine_token = paired.json()["token"]
        sent = cls.client.http.post("/api/agent/references", json=references(),
                                    headers={"Authorization": f"Bearer {cls.machine_token}"})
        assert sent.status_code == 200, sent.text
        assert sent.json()["url"] == f"http://localhost/recordings/{RECORDING}", sent.json()
        assert sorted(sent.json()["mapped"]) == sorted([cls.billing, cls.casedesk]), sent.json()
        detail = ok(alice.get(f"/api/v1/recordings/{RECORDING}"))
        cls.events = [e["event_id"] for e in detail["events"]]
        cls.web_events = web_events = [s["event_id"] for a in detail["refs"]["apps"] if a["app_id"] == cls.billing for s in a["steps"]]
        ok(alice.put(f"/api/v1/recordings/{RECORDING}/assignments/{cls.billing}",
                     json={"events": web_events, "notes": "Saving a case"}))
        cls.scenario = ok(alice.post(f"/api/v1/apps/{cls.billing}/scenarios", json={
            "name": "Invoice is saved", "expected": "The invoice has its number.",
            "events": [f"{RECORDING}#{web_events[0]}"]}), 201)["id"]
        cls.attachment = ok(alice.post(f"/api/v1/apps/{cls.billing}/attachments", data={
            "title": "API notes", "kind": "API", "share": "reviewed"},
            files={"file": ("notes.md", b"# The invoice API\n", "text/markdown")}), 201)["id"]
        draft, extra = preview(alice, cls.billing)
        cls.package = ok(alice.post(f"/api/v1/apps/{cls.billing}/packages",
                                    json={"sha256": ok(draft)["sha256"], "approve": True, "summary": "First",
                                          **extra}), 201)
        # build setup and a build session with the fake provider
        ok(root.post("/api/v1/setup/builds/repos", json={"repo": REPO}), 201)
        started = ok(cls.as_("bob").post(f"/api/v1/apps/{cls.billing}/builds", json={
            "provider": "fake", "revision": cls.package["revision"], "repo": REPO, "budget": 5,
            "request_key": "k-first"}), 201)
        cls.build_id = started["build"]["id"]
        ctx = builds.Context(poll_seconds=0, reconcile_delay=0, reconcile_checks=1, timeout=2)
        for _ in range(4):
            worker.run(cls.database, once=True, ctx=ctx, max_jobs=2, lease=5)
        with closing(connect(cls.database)) as db:
            sid = builds.get(db, cls.build_id)["provider_session_id"]
        cls.world.ask_tool(sid, "bash", {"command": "npm test"})
        for _ in range(3):
            worker.run(cls.database, once=True, ctx=ctx, max_jobs=2, lease=5)
        cls.ctx = ctx
        # release verification: green checks on SHA, a deployment reporting it
        ok(alice.put(f"/api/v1/apps/{cls.billing}/verification-settings", json={
            "required_checks": "", "target_platform": "web", "windows_check_pattern": "", "lab_url": "http://lab.test/",
            "launch": "python run.py"}))
        cls.checks_github.runs[SHA] = [run("build", run_id=1), run("test", run_id=2)]
        outcome = wait_verification(alice, alice.post(f"/api/v1/apps/{cls.billing}/verify-commit",
                                                      json={"repo": REPO, "sha": SHA}))
        cls.release = outcome["release_id"]
        ok(alice.put(f"/api/v1/releases/{cls.release}/deployments",
                     json={"environment": "Demo", "url": cls.deployment.url}))
        verified = wait_verification(alice, alice.post(f"/api/v1/releases/{cls.release}/verify"))
        assert verified["state"] == "passed", verified
        assert ok(alice.get(f"/api/v1/releases/{cls.release}"))["release"]["state"] == "tested"

    # -- every operation ------------------------------------------------------------------------

    def test_session(self):
        session = self.ok(self.as_("alice").get("/api/v1/session"))
        self.assertEqual((session["user"], session["auth_mode"], session["login_available"]), ("alice", "proxy", False))
        self.assertEqual({(r["app_name"], r["role"]) for r in session["roles"]},
                         {("Billing Desk", "owner"), ("CaseDesk", "owner")})
        self.assertTrue(self.ok(self.as_("root").get("/api/v1/session"))["is_admin"])
        nobody = Client(self.app)  # behind the proxy without a user: signed out, still 200
        self.assertFalse(self.ok(nobody.get("/api/v1/session"))["signed_in"])
        self.assertEqual(nobody.get("/api/v1/apps").json()["code"], "signed_out")
        # behind the proxy there is no local sign-in; sign-out answers 204
        self.assertEqual(self.as_("alice").post("/api/v1/session", json={"user": "a", "password": "b"}).status_code, 404)
        self.assertEqual(self.as_("alice").delete("/api/v1/session").status_code, 204)

    def test_csrf_is_required_on_every_change(self):
        response = self.as_("alice").post("/api/v1/apps", json={"name": "X"}, csrf=False)
        self.assertEqual((response.status_code, response.json()["code"]), (403, "csrf"))
        response = self.as_("alice").post("/api/v1/apps", json={"name": "X"}, headers={"X-CSRF-Token": "wrong"})
        self.assertEqual((response.status_code, response.json()["code"]), (403, "csrf"))

    def test_catalog_and_application(self):
        alice = self.as_("alice")
        catalog = self.ok(alice.get("/api/v1/apps"))
        self.assertEqual([a["name"] for a in catalog["apps"]], ["Billing Desk", "CaseDesk"])
        self.assertIn(RECORDING, [r["id"] for r in catalog["recordings"]])
        detail = self.ok(alice.get(f"/api/v1/apps/{self.billing}"))
        self.assertEqual(detail["packages"][0]["rev"], 1)
        self.assertEqual(self.ok(self.as_("vera").get(f"/api/v1/apps/{self.billing}"))["packages"], [])
        changed = self.ok(alice.patch(f"/api/v1/apps/{self.casedesk}", json={"purpose": "Cases", "status": "known"}))
        self.assertEqual(changed["purpose"], "Cases")
        self.assertEqual(alice.patch(f"/api/v1/apps/{self.casedesk}", json={"status": "nope"}).status_code, 400)
        self.assertEqual(alice.get("/api/v1/apps/999").status_code, 403)  # no role on it
        self.assertEqual(self.as_("root").get("/api/v1/apps/999").status_code, 404)

    def test_roles_and_history(self):
        alice = self.as_("alice")
        roles = self.ok(alice.get(f"/api/v1/apps/{self.billing}/roles"))
        self.assertEqual({r["user_name"]: r["role"] for r in roles["roles"]},
                         {"alice": "owner", "bob": "builder", "vera": "viewer"})
        self.ok(self.as_("root").put(f"/api/v1/apps/{self.casedesk}/roles/temp", json={"role": "viewer"}))
        after = self.ok(alice.delete(f"/api/v1/apps/{self.casedesk}/roles/temp"))
        self.assertNotIn("temp", [r["user_name"] for r in after["roles"]])
        last = alice.delete(f"/api/v1/apps/{self.billing}/roles/alice")
        self.assertEqual(last.status_code, 400)  # the last owner stays
        history = self.ok(alice.get(f"/api/v1/apps/{self.billing}/history"))
        self.assertIn("package.freeze", [e["action"] for e in history["entries"]])
        self.ok(self.as_("root").get(f"/api/v1/apps/{self.billing}/history"))

    def test_setup_identities_and_verification_settings(self):
        alice = self.as_("alice")
        setup = self.ok(alice.get(f"/api/v1/apps/{self.billing}/setup"))
        self.assertEqual(setup["github_repo"], REPO)
        self.assertEqual(setup["identities"][0]["key"], WEB)
        clash = alice.post(f"/api/v1/apps/{self.casedesk}/identities", json={"key": WEB.removeprefix("web:")})
        self.assertEqual(clash.status_code, 409)
        self.ok(alice.put(f"/api/v1/apps/{self.casedesk}/url-match", json={"url_match": "host"}))
        self.ok(alice.put(f"/api/v1/apps/{self.casedesk}/url-match", json={"url_match": "path"}))
        self.assertEqual(setup["data_backend"]["source"], "predicted")
        chosen = self.ok(alice.put(f"/api/v1/apps/{self.billing}/data-backend", json={"data_backend": "postgresql"}))
        self.assertEqual((chosen["app"]["data_backend"], chosen["data_backend"]),
                         ("postgresql", {"engine": "postgresql", "source": "chosen",
                                         "reason": "Chosen by the application's owner.", "tables": []}))
        self.assertEqual(alice.put(f"/api/v1/apps/{self.billing}/data-backend", json={"data_backend": "oracle"})
                         .status_code, 400)  # outside the enum
        self.assertEqual(self.as_("carol").put(f"/api/v1/apps/{self.billing}/data-backend",
                                               json={"data_backend": ""}).status_code, 403)
        self.ok(alice.put(f"/api/v1/apps/{self.billing}/data-backend", json={"data_backend": ""}))
        bad = alice.put(f"/api/v1/apps/{self.billing}/verification-settings", json={"required_checks": "",
                                                                                  "target_platform": "mars"})
        self.assertEqual(bad.status_code, 400)
        self.ok(self.as_("carol").get("/api/v1/setup"))
        self.assertEqual([a["name"] for a in self.ok(alice.get("/api/v1/setup"))["apps"]], ["Billing Desk", "CaseDesk"])

    def test_merge_plan_and_merge(self):
        alice = self.as_("alice")
        spare = self.ok(alice.post("/api/v1/apps", json={"name": "Spare"}), 201)["id"]
        plan = self.ok(alice.get(f"/api/v1/apps/{spare}/merge-plan?into={self.casedesk}"))
        stale = alice.post(f"/api/v1/apps/{spare}/merge", json={"into": self.casedesk, "confirm": "0" * 16})
        self.assertEqual((stale.status_code, stale.json()["code"]), (409, "plan_changed"))
        merged = self.ok(alice.post(f"/api/v1/apps/{spare}/merge", json={"into": self.casedesk,
                                                                         "confirm": plan["fingerprint"]}))
        self.assertEqual(merged["id"], self.casedesk)
        self.assertEqual(self.as_("root").get(f"/api/v1/apps/{spare}").status_code, 404)

    def test_evidence_attachments_and_scenarios(self):
        alice = self.as_("alice")
        evidence = self.ok(alice.get(f"/api/v1/apps/{self.billing}/evidence"))
        self.assertIn(RECORDING, [f["recording_id"] for f in evidence["recorded_flows"]])
        self.assertEqual(evidence["links"][0]["reviewed"], True)
        shared = self.ok(alice.patch(f"/api/v1/apps/{self.billing}/attachments/{self.attachment}",
                                     json={"share": "reviewed"}))
        self.assertEqual(shared["share"], "reviewed")
        extra = self.ok(alice.post(f"/api/v1/apps/{self.casedesk}/attachments", data={"title": "Shot", "kind": "Screen"},
                                   files={"file": ("shot.png", b"\x89PNG\r\n", "image/png")}), 201)
        self.assertEqual(extra["share"], "restricted")  # uploading never shares
        wrong = alice.post(f"/api/v1/apps/{self.casedesk}/attachments", data={"title": "x", "kind": "Screen"},
                           files={"file": ("x.exe", b"MZ", "application/octet-stream")})
        self.assertEqual(wrong.status_code, 400)
        self.assertEqual(alice.delete(f"/api/v1/apps/{self.casedesk}/attachments/{extra['id']}").status_code, 204)
        listed = self.ok(alice.get(f"/api/v1/apps/{self.billing}/scenarios"))
        self.assertEqual(listed["scenarios"][0]["name"], "Invoice is saved")
        self.assertTrue(listed["event_choices"])
        made = self.ok(alice.post(f"/api/v1/apps/{self.casedesk}/scenarios", json={"name": "Open a case"}), 201)
        changed = self.ok(alice.put(f"/api/v1/apps/{self.casedesk}/scenarios/{made['id']}",
                                    json={"name": "Open a case", "expected": "The case opens."}))
        self.assertEqual(changed["expected"], "The case opens.")
        foreign = alice.put(f"/api/v1/apps/{self.casedesk}/scenarios/{made['id']}",
                            json={"name": "x", "events": [f"{RECORDING}#{self.web_events[0]}"]})
        self.assertEqual(foreign.status_code, 400)  # only events selected for this app
        self.assertEqual(alice.delete(f"/api/v1/apps/{self.casedesk}/scenarios/{made['id']}").status_code, 204)
        self.assertEqual(alice.post(f"/api/v1/apps/{self.casedesk}/scenarios", json={}).status_code, 400)

    def test_packages(self):
        alice, bob = self.as_("alice"), self.as_("bob")
        draft, extra = preview(alice, self.billing)
        draft = self.ok(draft)
        if not draft["unchanged"]:  # another test linked more evidence: freeze it as the next revision
            frozen = self.ok(alice.post(f"/api/v1/apps/{self.billing}/packages",
                                        json={"sha256": draft["sha256"], "approve": True, **extra}), 201)
            self.assertEqual(frozen["rev"], draft["next_rev"])
            draft = self.ok(preview(alice, self.billing)[0])
        self.assertTrue(draft["unchanged"])
        listed = self.ok(bob.get(f"/api/v1/apps/{self.billing}/packages"))
        self.assertEqual(listed["packages"][-1]["rev"], 1)
        self.assertIsNone(self.ok(bob.get(f"/api/v1/apps/{self.billing}/packages/1"))["report"])
        self.assertIsInstance(self.ok(alice.get(f"/api/v1/apps/{self.billing}/packages/1"))["report"], list)
        again = alice.post(f"/api/v1/apps/{self.billing}/packages", json={"sha256": draft["sha256"], "approve": True,
                                                                          **extra})
        self.assertEqual((again.status_code, again.json()["code"]), (409, "unchanged"))
        stale = alice.post(f"/api/v1/apps/{self.billing}/packages", json={"sha256": "0" * 64, "approve": True})
        self.assertEqual((stale.status_code, stale.json()["code"]), (409, "draft_changed"))
        unapproved = alice.post(f"/api/v1/apps/{self.billing}/packages", json={"sha256": draft["sha256"],
                                                                              "approve": False})
        self.assertEqual(unapproved.status_code, 400)
        self.assertEqual(alice.get(f"/api/v1/apps/{self.casedesk}/package-draft").status_code, 400)  # no scenario
        for path in (f"/api/v1/apps/{self.billing}/packages/1/download", f"/api/v1/apps/{self.billing}/build-pack",
                     f"/api/v1/apps/{self.billing}/briefs/{self.package['revision']}/download"):
            response = bob.get(path)
            self.assertEqual((response.status_code, response.headers["content-type"]), (200, "application/zip"), path)
            with ZipFile(io.BytesIO(response.content)) as pack:
                self.assertIn("BUILD_TASK.md", pack.namelist())
        self.assertEqual(bob.get(f"/api/v1/apps/{self.casedesk}/build-pack").status_code, 403)
        self.assertEqual(alice.get(f"/api/v1/apps/{self.casedesk}/build-pack").status_code, 400)

    def test_builds(self):
        bob, alice = self.as_("bob"), self.as_("alice")
        page = self.ok(bob.get(f"/api/v1/apps/{self.billing}/builds"))
        self.assertEqual(page["pack"]["rev"], 1)
        self.assertIn("fake", [p["name"] for p in page["providers"]])
        again = self.ok(bob.post(f"/api/v1/apps/{self.billing}/builds", json={
            "provider": "fake", "revision": self.package["revision"], "repo": REPO, "budget": 5,
            "request_key": "k-first"}), 201)
        self.assertEqual((again["created"], again["build"]["id"]), (False, self.build_id))
        self.assertIn("already started", again["notice"])
        detail = self.ok(bob.get(f"/api/v1/builds/{self.build_id}"))
        tool = next(m for m in detail["messages"] if m["kind"] == "tool_approval")
        decided = self.ok(bob.post(f"/api/v1/builds/{self.build_id}/tool-decisions/{tool['id']}",
                                   json={"decision": "approve", "request_key": "tool-1"}))
        self.assertEqual(next(m for m in decided["messages"] if m["id"] == tool["id"])["decision"], "approve")
        scope = self.ok(bob.post(f"/api/v1/builds/{self.build_id}/messages", json={
            "answer": "Also export to CSV", "scope_change": True, "request_key": "scope-1"}), 201)
        self.assertIn("Not sent", scope["notice"])
        self.assertEqual(bob.post(f"/api/v1/builds/{self.build_id}/messages",
                                  json={"answer": " ", "request_key": "x"}).status_code, 400)
        self.ok(bob.post(f"/api/v1/builds/{self.build_id}/interrupt"))
        second = self.ok(bob.post(f"/api/v1/apps/{self.billing}/builds", json={
            "provider": "fake", "revision": self.package["revision"], "repo": REPO, "budget": 5,
            "request_key": "k-second"}), 201)["build"]["id"]
        cancelled = self.ok(bob.post(f"/api/v1/builds/{second}/cancel"))
        self.assertEqual(cancelled["build"]["id"], second)
        retried = self.ok(bob.post(f"/api/v1/builds/{second}/retry", json={"request_key": "k-retry"}), 201)
        self.assertTrue(retried["created"])
        compared = self.ok(bob.get(f"/api/v1/apps/{self.billing}/builds/compare?session={self.build_id}"))
        self.assertEqual(compared["results"][0]["build"]["id"], self.build_id)
        run_compare = self.ok(alice.post(f"/api/v1/apps/{self.billing}/builds/compare",
                                         json={"sessions": [self.build_id]}))
        self.assertTrue(run_compare["errors"])  # the session has no commit yet
        self.assertEqual(bob.get("/api/v1/builds/9999").status_code, 404)

    def test_recordings(self):
        alice = self.as_("alice")
        listed = self.ok(alice.get("/api/v1/recordings"))
        self.assertIn(RECORDING, [r["id"] for r in listed["recordings"]])
        detail = self.ok(alice.get(f"/api/v1/recordings/{RECORDING}"))
        apps = {a["app_id"]: a for a in detail["refs"]["apps"]}
        self.assertEqual(set(apps), {self.billing, self.casedesk})  # one recording, two applications
        self.assertGreaterEqual(apps[self.billing]["latest_package"]["rev"], 1)  # builders and owners see it
        self.assertIsNone(apps[self.casedesk]["latest_package"])
        self.assertTrue(apps[self.casedesk]["steps"] and apps[self.billing]["steps"])
        assignment = self.ok(alice.get(f"/api/v1/recordings/{RECORDING}/assignments/{self.billing}"))
        self.assertTrue(assignment["reviewed"])
        bad = alice.put(f"/api/v1/recordings/{RECORDING}/assignments/{self.billing}", json={"events": ["nope"]})
        self.assertEqual(bad.status_code, 400)
        imported = self.ok(alice.post("/api/v1/recordings", data={"name": "Invoice"},
                                      files={"log": ("steps.jsonl", IMPORTED_LOG, "application/x-ndjson")}), 201)
        self.assertEqual(self.ok(alice.get(f"/api/v1/recordings/{imported['id']}"))["name"], "Invoice")
        events = self.ok(alice.get(f"/api/v1/recordings/{imported['id']}/events?limit=1&ids=true"))
        self.assertEqual((len(events["events"]), events["page"]["total"], len(events["ids"])), (1, 2, 2))
        # grouped and filtered by screen: the counts per screen, in first-reached order, and one screen
        every = self.ok(alice.get(f"/api/v1/recordings/{RECORDING}/events?limit=500"))["events"]
        screens = self.ok(alice.get(f"/api/v1/recordings/{RECORDING}/events?limit=1"))["screens"]
        first = []
        for e in every:
            if e.get("screen") not in first:
                first.append(e.get("screen"))
        self.assertEqual([s["screen"] for s in screens], first)
        self.assertEqual(sum(s["count"] for s in screens), len(every))
        one = screens[0]
        only = self.ok(alice.get(f"/api/v1/recordings/{RECORDING}/events", params={"screen": one["screen"] or "", "limit": 500}))
        self.assertEqual(only["page"]["total"], one["count"])
        self.assertTrue(all(e.get("screen") == one["screen"] for e in only["events"]))
        # removing an application's assignment from a recording (none saved: still recorded as removed)
        removed = self.ok(alice.delete(f"/api/v1/recordings/{imported['id']}/assignments/{self.casedesk}"))
        self.assertEqual((removed["removed"], removed["selected"]), (True, []))
        bad = alice.post("/api/v1/recordings", data={"name": "Broken"}, files={"log": ("s.jsonl", b"{", "text/plain")})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.as_("carol").get(f"/api/v1/recordings/{RECORDING}").json()["code"], "restricted")
        exported = alice.get(f"/api/v1/recordings/{RECORDING}/packages/download")  # every app's package
        with ZipFile(io.BytesIO(exported.content)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        self.assertEqual({a["app_id"]: a["status"] for a in manifest["applications"]},
                         {self.billing: "included", self.casedesk: "no_package"})
        self.assertEqual(self.as_("bob").get(f"/api/v1/recordings/{RECORDING}/packages/download").json()["code"],
                         "restricted")
        progress = alice.get(f"/api/recordings/{RECORDING}/upload")
        self.assertEqual(progress.json()["state"], "none")

    def test_accepting_a_partial_recording(self):
        alice = self.as_("alice")
        from test_registry import completeness_report, references_1_1

        doc = references_1_1("20261119T110000Z-0b1b2c3d", report=completeness_report(
            "partial", web={"status": "interrupted", "notes": [], "gaps": [
                {"from": "2026-11-19T10:01:00Z", "to": "2026-11-19T10:02:00Z", "reason": "browser closed"}]}))
        sent = self.client.http.post("/api/agent/references", json=doc,
                                     headers={"Authorization": f"Bearer {self.machine_token}"})
        self.assertEqual(sent.status_code, 200, sent.text)
        rid = sent.json()["recording_id"]
        self.assertTrue(self.ok(alice.get(f"/api/v1/recordings/{rid}"))["status"]["attention"])
        accepted = self.ok(alice.post(f"/api/v1/recordings/{rid}/completeness/accept"))
        self.assertTrue(accepted["status"]["accepted"])
        self.assertEqual(alice.post(f"/api/v1/recordings/{RECORDING}/completeness/accept").status_code, 404)

    def test_capture_sessions_and_machines(self):
        alice = self.as_("alice")
        page = self.ok(alice.get("/api/v1/capture-sessions"))
        machine = page["machines"][0]["id"]
        made = self.ok(alice.post("/api/v1/capture-sessions", json={
            "name": "Order to invoice", "machine_id": machine,
            "targets": [{"app_id": self.billing, "level": "full"}, {"app_id": self.casedesk, "level": "steps"}]}), 201)
        self.assertEqual([t["app_id"] for t in made["targets"]], [self.billing, self.casedesk])
        offered = self.client.http.get("/api/agent/sessions", headers={"Authorization": f"Bearer {self.machine_token}"})
        self.assertIn(made["id"], [s["id"] for s in offered.json()["sessions"]])
        self.assertEqual(self.as_("carol").post(f"/api/v1/capture-sessions/{made['id']}/withdraw").status_code, 403)
        self.assertEqual(self.ok(alice.post(f"/api/v1/capture-sessions/{made['id']}/withdraw"))["status"], "withdrawn")
        denied = self.as_("bob").post("/api/v1/capture-sessions", json={
            "name": "x", "machine_id": machine, "targets": [{"app_id": self.billing, "level": "full"}]})
        self.assertEqual(denied.status_code, 403, denied.text)
        machines = self.ok(alice.get("/api/v1/machines"))
        self.assertEqual(machines["machines"][0]["name"], "PC-1")
        self.assertEqual(self.ok(self.as_("carol").get("/api/v1/machines"))["machines"], [])
        code = self.ok(self.as_("carol").post("/api/v1/machines/pairing-codes"), 201)
        self.assertEqual(code["display"], code["code"][:5] + "-" + code["code"][5:])
        other = self.client.http.post("/api/agent/pair", json={"contract_version": CV, "code": code["code"], "name": "PC-2"})
        machine_id = other.json()["machine_id"]
        self.assertEqual(alice.post(f"/api/v1/machines/{machine_id}/revoke").status_code, 403)
        self.assertIsNotNone(self.ok(self.as_("carol").post(f"/api/v1/machines/{machine_id}/revoke"))["revoked_at"])
        apps = self.client.http.get("/api/agent/apps", headers={"Authorization": f"Bearer {other.json()['token']}"})
        self.assertEqual(apps.status_code, 401)  # revoked

    def test_automations(self):
        alice = self.as_("alice")
        made = self.ok(alice.post("/api/v1/automations", json={"name": "Nightly", "kind": "rpa",
                                                               "apps": [self.billing]}), 201)
        self.assertEqual([a["name"] for a in made["apps"]], ["Billing Desk"])
        imported = self.ok(alice.post("/api/v1/automations/import",
                                      files={"file": ("automation.yaml", AUTOMATION_YAML, "application/yaml")}), 201)
        self.assertIn("Unknown", imported["automation"]["notes"])
        self.assertIn("Nightly", [a["name"] for a in self.ok(alice.get("/api/v1/automations"))["automations"]])
        self.ok(alice.get(f"/api/v1/automations/{made['automation']['id']}"))
        self.assertEqual(alice.get("/api/v1/automations/999").status_code, 404)
        self.assertEqual(self.as_("bob").post("/api/v1/automations", json={"name": "x", "apps": [self.billing]})
                         .status_code, 403)

    def test_releases(self):
        alice, bob, vera = self.as_("alice"), self.as_("bob"), self.as_("vera")
        detail = self.ok(vera.get(f"/api/v1/releases/{self.release}"))
        self.assertEqual((detail["release"]["state"], detail["can_download"]), ("tested", False))
        self.assertEqual(detail["deployments"][0]["label"], "Verified")
        self.assertEqual(vera.get(f"/api/v1/releases/{self.release}/download").status_code, 403)
        self.ok(bob.post(f"/api/v1/releases/{self.release}/deployments/check", json={"environment": "Demo"}))
        self.assertEqual(bob.post(f"/api/v1/releases/{self.release}/deployments/check",
                                  json={"environment": "Nope"}).status_code, 404)
        self.ok(alice.post(f"/api/v1/releases/{self.release}/checks/refresh"))
        scenario = detail["scenarios"][0]["id"]
        tested = self.ok(alice.post(f"/api/v1/releases/{self.release}/tests", json={
            "commit": SHA, "platform": "Web", "launch": "python run.py", "reset_result": "pass",
            "results": {scenario: "pass"}}))
        self.assertEqual(tested["test"]["results"][0]["result"], "pass")
        scope = tested["test"]["scope"]
        self.assertEqual((scope["commit_sha"], scope["tested_by"], scope["role"], scope["method"],
                          scope["scenarios_passed"]), (SHA, "alice", "owner", "manual", 1))
        self.assertIn(SHA[:12], scope["label"])
        wrong = alice.post(f"/api/v1/releases/{self.release}/tests", json={
            "commit": "f" * 40, "platform": "Web", "launch": "x", "reset_result": "pass", "results": {}})
        self.assertEqual(wrong.status_code, 400)
        # a readable number next to the commit; reports latest first, each with its outcome
        self.assertRegex(detail["release"]["label"], r"^\d+\.1$")
        reports = detail["checks"]["reports"]
        self.assertEqual([r["latest"] for r in reports], [True] + [False] * (len(reports) - 1))
        self.assertEqual([r["number"] for r in reports], list(range(len(reports), 0, -1)))
        self.assertTrue(all(r["tested"] in (True, False, None) for r in reports))
        report = detail["checks"]["reports"][0]["id"]
        downloaded = bob.get(f"/api/v1/releases/{self.release}/reports/{report}")
        self.assertEqual(json.loads(downloaded.content)["commit"], SHA)
        self.assertIn("scope", json.loads(downloaded.content))  # the verification report states its scope
        manifest = bob.get(f"/api/v1/releases/{self.release}/openshift.yaml")
        self.assertEqual(manifest.status_code, 200, manifest.text)
        self.assertIn(f"ref: {SHA}", manifest.text)
        self.assertEqual(vera.get(f"/api/v1/releases/{self.release}/openshift.yaml").status_code, 403)
        published = self.ok(alice.post(f"/api/v1/releases/{self.release}/publish"))
        self.assertEqual(published["release"]["state"], "published")
        self.assertEqual(alice.post(f"/api/v1/releases/{self.release}/publish").status_code, 400)
        self.assertEqual(vera.get(f"/api/v1/releases/{self.release}/download").status_code, 200)
        self.assertEqual(vera.get(f"/api/v1/releases/{self.release}/openshift.yaml").status_code, 200)
        self.assertTrue(self.ok(vera.get(f"/api/v1/releases/{self.release}"))["can_download"])
        catalog = self.ok(vera.get("/api/v1/apps"))
        self.assertEqual(catalog["apps"][0]["tested_release"]["state"], "published")

    def test_workboard_triage_home_and_releases(self):
        """The overviews (routes_workboard; test_workboard has the details on the demo portal)."""
        alice, bob, vera = self.as_("alice"), self.as_("bob"), self.as_("vera")
        board = self.ok(alice.get(f"/api/v1/recordings/{RECORDING}/workboard"))
        self.assertIn(self.billing, [r["app_id"] for r in board["apps"]])
        self.assertTrue(all(r["action"] for r in board["apps"]))
        self.assertEqual(vera.get(f"/api/v1/recordings/{RECORDING}/workboard").status_code, 403)
        triage = self.ok(alice.get("/api/v1/triage", params={"recording_id": RECORDING, "status": "all"}))
        self.assertIn(self.billing, [c["app_id"] for c in triage["apps"]])
        decided = self.ok(alice.post("/api/v1/triage/decisions", json={"decisions": [
            {"app_id": self.billing, "decision": "replicate"}]}))
        self.assertEqual(decided["apps"][0]["status"], "known")
        self.assertEqual(vera.post("/api/v1/triage/decisions", json={"decisions": [
            {"app_id": self.billing, "decision": "not_replicated"}]}).status_code, 403)
        self.assertEqual(self.ok(vera.get("/api/v1/home"))["role"], "viewer")
        self.assertEqual(self.ok(bob.get("/api/v1/home"))["ready_to_build"][0]["app_id"], self.billing)
        registered = self.ok(bob.post(f"/api/v1/apps/{self.billing}/releases", json={"sha": SHA}), 201)
        self.assertEqual(registered["release"]["commit_sha"], SHA)
        self.assertEqual(vera.post(f"/api/v1/apps/{self.billing}/releases", json={"sha": SHA}).status_code, 403)

    def test_build_setup_and_users(self):
        root = self.as_("root")
        setup = self.ok(root.get("/api/v1/setup/builds"))
        self.assertEqual(setup["repos"][0]["repo"], REPO)
        saved = self.ok(root.put("/api/v1/setup/builds/settings", json={"settings": {"CLAUDE_MODEL": "claude-opus-5-5",
                                                                                    "ANTHROPIC_API_KEY": "sk-test"}}))
        self.assertEqual((saved["settings"]["CLAUDE_MODEL"], saved["saved"]["ANTHROPIC_API_KEY"]), ("claude-opus-5-5", True))
        cleared = self.ok(root.put("/api/v1/setup/builds/settings", json={"settings": {}, "clear": ["ANTHROPIC_API_KEY"]}))
        self.assertFalse(cleared["saved"]["ANTHROPIC_API_KEY"])
        bad = root.put("/api/v1/setup/builds/settings", json={"settings": {"DEFAULT_ACU_BUDGET": "-3"}})
        self.assertEqual(bad.status_code, 400)
        checked = self.ok(root.post("/api/v1/setup/builds/check"))
        self.assertEqual({r["service"] for r in checked["results"]}, {"devin", "github"})
        self.assertEqual(root.post("/api/v1/setup/builds/worker").status_code, 403)  # not in this deployment
        self.ok(root.post("/api/v1/setup/builds/repos", json={"repo": "acme/spare"}), 400)
        self.builds_github.add_repo("acme/spare")
        self.ok(root.post("/api/v1/setup/builds/repos", json={"repo": "acme/spare"}), 201)
        self.assertEqual(root.delete("/api/v1/setup/builds/repos?repo=acme/spare").status_code, 204)
        users = self.ok(root.put("/api/v1/admin/users/erin", json={"is_admin": False, "password": "s3cret"}))
        self.assertTrue(next(u for u in users["users"] if u["name"] == "erin")["has_password"])
        self.assertIn("erin", [u["name"] for u in self.ok(root.get("/api/v1/admin/users"))["users"]])

    def test_starting_the_development_worker(self):
        self.app.config["DEV_WORKER"] = True
        try:
            with mock.patch.object(builds, "start_dev_worker", return_value=(4242, True)):
                self.assertEqual(self.ok(self.as_("root").post("/api/v1/setup/builds/worker")),
                                 {"pid": 4242, "started": True})
        finally:
            self.app.config["DEV_WORKER"] = False

    # -- roles: each role's refusals -------------------------------------------------------------

    def refused(self, user, calls, code="role"):
        client = self.as_(user)
        for method, path, *body in calls:
            response = client.request(method, path, json=body[0] if body else None)
            self.assertEqual(response.status_code, 403, f"{user} {method} {path}: {response.text[:300]}")
            self.assertEqual(response.json().get("code"), code, f"{user} {method} {path}")

    def owner_calls(self):
        a = self.billing
        return [("PATCH", f"/api/v1/apps/{a}", {"purpose": "x"}), ("GET", f"/api/v1/apps/{a}/setup"),
                ("GET", f"/api/v1/apps/{a}/evidence"), ("GET", f"/api/v1/apps/{a}/scenarios"),
                ("POST", f"/api/v1/apps/{a}/scenarios", {"name": "x"}), ("GET", f"/api/v1/apps/{a}/package-draft"),
                ("POST", f"/api/v1/apps/{a}/packages", {"sha256": "0" * 64, "approve": True}),
                ("PUT", f"/api/v1/apps/{a}/roles/x", {"role": "viewer"}), ("GET", f"/api/v1/apps/{a}/history"),
                ("POST", f"/api/v1/apps/{a}/identities", {"key": "Other.exe"}),
                ("PUT", f"/api/v1/apps/{a}/verification-settings", {"required_checks": "", "target_platform": "web"}),
                ("POST", f"/api/v1/apps/{a}/verify-commit", {"sha": SHA}),
                ("GET", f"/api/v1/recordings/{RECORDING}/assignments/{a}"),
                ("PUT", f"/api/v1/recordings/{RECORDING}/assignments/{a}", {"events": []}),
                ("POST", f"/api/v1/apps/{a}/builds/compare", {"sessions": [1]}),
                ("PUT", f"/api/v1/releases/{self.release}/deployments", {"environment": "x", "url": "http://x/"}),
                ("POST", f"/api/v1/releases/{self.release}/tests", {"commit": SHA, "platform": "x", "launch": "x",
                                                                   "reset_result": "pass", "results": {}}),
                ("POST", f"/api/v1/releases/{self.release}/verify"), ("POST", f"/api/v1/releases/{self.release}/publish"),
                ("GET", f"/api/v1/apps/{a}/merge-plan?into={self.casedesk}")]

    def builder_calls(self):
        a = self.billing
        return [("GET", f"/api/v1/apps/{a}/packages"), ("GET", f"/api/v1/apps/{a}/packages/1"),
                ("GET", f"/api/v1/apps/{a}/packages/1/download"), ("GET", f"/api/v1/apps/{a}/build-pack"),
                ("GET", f"/api/v1/apps/{a}/builds"),
                ("POST", f"/api/v1/apps/{a}/builds", {"provider": "fake", "revision": "r", "repo": REPO, "budget": 1,
                                                      "request_key": "deny"}),
                ("GET", f"/api/v1/builds/{self.build_id}"), ("POST", f"/api/v1/builds/{self.build_id}/cancel"),
                ("GET", f"/api/v1/apps/{a}/builds/compare"),
                ("POST", f"/api/v1/releases/{self.release}/deployments/check", {"environment": "Demo"})]

    def admin_calls(self):
        return [("GET", "/api/v1/setup/builds"), ("PUT", "/api/v1/setup/builds/settings", {"settings": {}}),
                ("POST", "/api/v1/setup/builds/check"), ("POST", "/api/v1/setup/builds/repos", {"repo": "a/b"}),
                ("DELETE", "/api/v1/setup/builds/repos?repo=a/b"), ("POST", "/api/v1/setup/builds/worker"),
                ("GET", "/api/v1/admin/users"), ("PUT", "/api/v1/admin/users/x", {"is_admin": True})]

    def test_a_viewer_reads_and_nothing_more(self):
        self.ok(self.as_("vera").get(f"/api/v1/apps/{self.billing}"))
        self.ok(self.as_("vera").get(f"/api/v1/apps/{self.billing}/roles"))
        self.refused("vera", self.owner_calls() + self.builder_calls() + self.admin_calls())

    def test_a_builder_builds_but_never_reviews_evidence(self):
        self.ok(self.as_("bob").get(f"/api/v1/apps/{self.billing}/packages"))
        self.refused("bob", self.owner_calls() + self.admin_calls())
        self.refused("bob", [("GET", f"/api/v1/recordings/{RECORDING}")], code="restricted")

    def test_someone_without_a_role_sees_nothing_of_the_application(self):
        self.refused("carol", [("GET", f"/api/v1/apps/{self.billing}"), ("GET", f"/api/v1/apps/{self.billing}/roles"),
                               ("GET", f"/api/v1/releases/{self.release}")]
                     + self.owner_calls() + self.builder_calls() + self.admin_calls())
        self.assertEqual(self.ok(self.as_("carol").get("/api/v1/apps"))["apps"], [])
        self.assertEqual(self.ok(self.as_("carol").get("/api/v1/recordings"))["recordings"], [])

    def test_an_owner_is_not_an_admin(self):
        self.refused("alice", self.admin_calls())

    def test_an_admin_reads_but_does_not_own(self):
        root = self.as_("root")
        self.ok(root.get(f"/api/v1/apps/{self.billing}"))
        self.ok(root.get(f"/api/v1/apps/{self.billing}/history"))
        self.ok(root.put(f"/api/v1/apps/{self.casedesk}/roles/temp2", json={"role": "viewer"}))
        self.refused("root", [c for c in self.owner_calls() if "/history" not in c[1] and "/roles/" not in c[1]]
                     + self.builder_calls())
        self.refused("root", [("GET", f"/api/v1/recordings/{RECORDING}")], code="restricted")

    # -- the policies, the web app and the generated models ---------------------------------------

    def test_every_route_has_a_policy(self):
        unmarked = [route.path for route in rest.all_routes(self.app)
                    if getattr(route, "endpoint", None) is not None and rest.policy_of(route) is None
                    and route.path not in ("/openapi.json",)]
        self.assertEqual(unmarked, [])
        # and a route without one is refused before its view runs
        from twinlab2.rest import PolicyRoute
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        probe = FastAPI()
        probe.router.route_class = PolicyRoute
        rest.install_errors(probe)
        probe.add_api_route("/api/v1/open", lambda: {"open": True}, methods=["GET"])
        self.assertEqual(TestClient(probe).get("/api/v1/open").status_code, 403)

    def test_every_spec_operation_has_a_route_and_back(self):
        spec_ops = {(m.upper(), t) for m, _, t, _ in contract.operations()}
        routes = set()
        for route in rest.all_routes(self.app):
            for method in getattr(route, "methods", set()) or set():
                path = route.path.replace(":int", "").replace(":path", "")
                if path.startswith("/api/v1/") and "{rest}" not in path:
                    routes.add((method, path))
        self.assertEqual(sorted(spec_ops - routes), [])
        self.assertEqual(sorted(routes - spec_ops), [])

    def test_the_web_app_is_served_with_its_fallback(self):
        dist = os.path.join(self.work.name, "dist")
        os.makedirs(os.path.join(dist, "assets"), exist_ok=True)
        with open(os.path.join(dist, "index.html"), "w") as out:
            out.write("<!doctype html><div id=root></div>")
        with open(os.path.join(dist, "assets", "app.js"), "w") as out:
            out.write("console.log(1)")
        with mock.patch.dict(os.environ, {"TWINLAB2_WEB_DIST": dist}):
            for path in ("/", "/login", f"/apps/{self.billing}", f"/recordings/{RECORDING}", "/setup/builds"):
                response = self.client.http.get(path)
                self.assertEqual((response.status_code, response.text), (200, "<!doctype html><div id=root></div>"))
            self.assertEqual(self.client.http.get("/assets/app.js").text, "console.log(1)")
            self.assertEqual(self.client.http.get("/assets/nope.js").status_code, 404)
            missing = self.client.get("/api/v1/nothing-here")
            self.assertEqual((missing.status_code, missing.headers["content-type"]), (404, "application/problem+json"))
            self.assertEqual(self.client.http.get("/api/agent/nothing").status_code, 404)

    def test_the_models_are_generated_from_the_spec(self):
        import subprocess
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        result = subprocess.run([sys.executable, str(root / "tools" / "gen_api_models.py"), "--check"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    # -- conventions (tests/server/test_ocp_readiness.py has the rest) ------------------------

    def test_recordings_list_their_applications_as_a_list(self):
        """Reproduced: `apps` was one comma-joined text (`Billing Desk (full), CaseDesk (steps)`)."""
        listed = self.as_("alice").get("/api/v1/recordings")
        self.assertEqual(listed.headers["deprecation"], "true")  # `apps` (the text) is deprecated
        recording = next(r for r in self.ok(listed)["recordings"] if r["id"] == RECORDING)
        self.assertEqual({(a["app_id"], a["name"]) for a in recording["apps_list"]},
                         {(self.billing, "Billing Desk"), (self.casedesk, "CaseDesk")})
        for item in recording["apps_list"]:
            self.assertIn(f"{item['name']} ({item['mode']})", recording["apps"])
        schema = contract.spec()["components"]["schemas"]["RecordingSummary"]["properties"]["apps"]
        self.assertTrue(schema["deprecated"])

    def test_build_chat_polling_answers_304_while_unchanged(self):
        bob = self.as_("bob")
        for path in (f"/api/v1/builds/{self.build_id}", f"/api/v1/apps/{self.billing}/builds"):
            first = bob.get(path)
            etag = first.headers["etag"]
            self.assertEqual(first.headers["cache-control"], "no-cache")
            unchanged = bob.get(path, headers={"If-None-Match": etag})
            self.assertEqual((unchanged.status_code, unchanged.content), (304, b""), path)
            self.assertEqual(unchanged.headers["etag"], etag)
            self.assertEqual(bob.get(path, headers={"If-None-Match": '"other"'}).status_code, 200)
        # a new message changes the session: the old ETag no longer matches
        path = f"/api/v1/builds/{self.build_id}"
        etag = bob.get(path).headers["etag"]
        self.ok(bob.post(f"{path}/messages", json={"answer": "VAT after the discount."},
                         headers={"Idempotency-Key": "etag-change-1"}), 201)
        self.assertEqual(bob.get(path, headers={"If-None-Match": etag}).status_code, 200)

    def test_a_role_refusal_says_whom_to_ask(self):
        """Reproduced: a 403 said only "This needs the builder role on this application."."""
        refused = self.as_("vera").get(f"/api/v1/apps/{self.billing}/builds")  # vera is a viewer there
        body = refused.json()
        self.assertEqual((refused.status_code, body["code"], body["type"]), (403, "role", "/errors/role"))
        self.assertEqual(body["owners"], ["alice"])
        self.assertIn("alice", body["detail"])
        stranger = self.as_("carol").get(f"/api/v1/apps/{self.billing}/builds").json()  # no role: not told who owns it
        self.assertEqual(stranger["code"], "role")
        self.assertNotIn("owners", stranger)
        self.assertNotIn("alice", stranger["detail"])

    def test_the_build_request_key_is_an_alias_of_the_idempotency_key(self):
        bob = self.as_("bob")
        path = f"/api/v1/builds/{self.build_id}/messages"
        first = self.ok(bob.post(path, json={"answer": "Rounded per line."}, headers={"Idempotency-Key": "alias-1"}), 201)
        self.assertTrue(first["created"])
        again = bob.post(path, json={"answer": "Rounded per line.", "request_key": "alias-1"})
        self.assertEqual(again.status_code, 201)
        self.assertFalse(again.json()["created"])
        self.assertEqual(again.json()["message"]["id"], first["message"]["id"])
        self.assertEqual(again.headers["idempotent-replayed"], "true")
        other = bob.post(path, json={"answer": "Something else.", "request_key": "alias-1"})
        self.assertEqual((other.status_code, other.json()["code"]), (422, "idempotency_key_reused"))
        both = bob.post(path, json={"answer": "x", "request_key": "alias-2"}, headers={"Idempotency-Key": "alias-3"})
        self.assertEqual((both.status_code, both.json()["code"]), (400, "idempotency_key_mismatch"))

    def test_zz_operations(self):
        """The operations API (named to run last: it deletes from the shared world)."""
        import hashlib

        root, alice = self.as_("root"), self.as_("alice")
        summary = self.ok(root.get("/api/v1/admin/operations"))
        self.assertEqual(summary["storage"]["backend"], "local")
        policy = self.ok(root.put("/api/v1/admin/retention", json={"build_logs": 3650, "schedule": "off"}))
        self.assertEqual((policy["build_logs"], policy["updated_by"]), (3650, "root"))
        self.assertEqual(root.put("/api/v1/admin/retention", json={"audit": 5}).json()["code"], "invalid_request")
        run = self.ok(root.post("/api/v1/admin/retention/runs", json={"dry_run": True}), 202)
        worker.run(self.database, once=True, types=("retention",))
        done = self.ok(root.get(f"/api/v1/admin/retention/runs/{run['id']}"))
        self.assertEqual((done["state"], done["result"]["build_logs"]["count"]), ("done", 0))
        export = root.get("/api/v1/admin/audit-export", params={"since": "2000-01-01", "format": "csv"})
        self.assertEqual(export.status_code, 200)
        self.assertTrue(export.text.startswith("id,at,actor,app_id,action,object_type,object_id,details\n"))
        self.assertIn("package.freeze", export.text)
        # the attachment is in the frozen package too: a purge finds and deletes both
        sha = hashlib.sha256(b"# The invoice API\n").hexdigest()
        found = self.ok(root.get(f"/api/v1/admin/files/{sha}"))
        self.assertTrue(found["artifacts"] and found["inside"])
        purged = self.ok(root.post(f"/api/v1/admin/files/{sha}/purge", json={"note": "Test purge."}))
        self.assertGreaterEqual(purged["artifacts_deleted"], 1)
        gone = alice.get(f"/api/v1/apps/{self.billing}/packages/{self.package['rev']}/download")
        self.assertEqual((gone.status_code, gone.json()["code"]), (410, "gone"))
        stones = self.ok(alice.get(f"/api/v1/apps/{self.billing}/tombstones"))["tombstones"]
        self.assertIn("frozen_package" if any(s["object_type"] == "frozen_package" for s in stones) else "attachment",
                      [s["object_type"] for s in stones])
        self.assertTrue(self.ok(root.get("/api/v1/admin/tombstones"))["tombstones"])
        refused = self.as_("carol").post(f"/api/v1/recordings/{RECORDING}/deletion", json={"reason": "No."})
        self.assertEqual(refused.status_code, 403)
        deleted = self.ok(alice.post(f"/api/v1/recordings/{RECORDING}/deletion",
                                     json={"reason": "Recorded by mistake.", "purge_packages": True}))
        self.assertEqual(sorted(deleted["apps"]), sorted([self.billing, self.casedesk]))
        self.assertEqual(alice.get(f"/api/v1/recordings/{RECORDING}").status_code, 404)


class LocalSignInTest(unittest.TestCase):
    """A portal with local users (no proxy): sign in, wrong password, sign out."""

    def test_sign_in_and_out(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        with mock.patch.dict(os.environ, {}):
            for name in ("TWINLAB2_TRUST_PROXY_USER", "TWINLAB2_PASSWORD", "TWINLAB2_ADMINS"):
                os.environ.pop(name, None)
            database = new_database(work.name)
            with closing(connect(database)) as db:
                from twinlab2 import migrations

                migrations.run(db)
                with db:
                    auth.save_user(db, "erin", password="s3cret", by="test", now="2026-11-20T00:00:00+00:00")
            app = create_app(database)
            client = Client(app, remote="10.0.0.5")  # not this machine: a sign-in is needed
            before = client.get("/api/v1/session").json()
            self.assertEqual((before["signed_in"], before["auth_mode"], before["login_available"]), (False, "local", True))
            self.assertEqual(client.get("/api/v1/apps").json()["code"], "signed_out")
            wrong = client.post("/api/v1/session", json={"user": "erin", "password": "nope"})
            self.assertEqual((wrong.status_code, wrong.json()["code"]), (401, "wrong_credentials"))
            signed = client.post("/api/v1/session", json={"user": "erin", "password": "s3cret"})
            self.assertEqual((signed.status_code, signed.json()["user"]), (200, "erin"))
            self.assertEqual(client.get("/api/v1/apps").status_code, 200)
            self.assertEqual(client.delete("/api/v1/session").status_code, 204)
            self.assertEqual(client.get("/api/v1/apps").status_code, 401)


class ZzCoverage(unittest.TestCase):
    def test_every_operation_is_covered(self):
        """Every /api/v1 operation was answered with a success status by a test above (this
        class runs last in this module)."""
        succeeded = {(m, t) for m, t, status in contract.seen if status < 400}
        missing = sorted({(m.upper(), t) for m, _, t, _ in contract.operations()} - succeeded)
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
