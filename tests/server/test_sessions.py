"""Build sessions: several attempts per package, the session chat, isolation and coordination.

The provider is tests/fake_provider.py (in memory); GitHub is the local fake in fakes.py. Nothing
here talks to Devin, Anthropic, GitHub or Copilot, and nothing spends money.
"""

from contextlib import closing
import hashlib
import os
import tempfile
import unittest
from unittest import mock

from twinlab2 import builds
import fake_provider
from fakes import FakeGitHub
from twinlab2 import jobqueue
from twinlab2 import migrations
from twinlab2 import packages
from twinlab2.portal import create_app
from twinlab2 import portal
from support import Client, connect, freeze as freeze_package, new_database, wait_verification
import test_verification
from twinlab2 import verification
from twinlab2 import worker

REPO = "acme/orders-demo"
BASE = "b" * 40


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.database = new_database(self.work.name)
        self.github = FakeGitHub().start()
        self.addCleanup(self.github.stop)
        self.github.add_repo(REPO, sha=BASE)
        patch = mock.patch.dict(os.environ, {"TWINLAB2_GITHUB_TOKEN": self.github.token,
                                             "TWINLAB2_GITHUB_API": self.github.url,
                                             "TWINLAB2_WORKSPACES": os.path.join(self.work.name, "workspaces")})
        patch.start()
        self.addCleanup(patch.stop)
        for name in ("TWINLAB2_PASSWORD", "TWINLAB2_TRUST_PROXY_USER"):
            os.environ.pop(name, None)
        self.world = fake_provider.reset()
        self.ctx = builds.Context(poll_seconds=0, reconcile_delay=0, reconcile_checks=1, timeout=2)
        self.app = create_app(self.database)
        self.client = Client(self.app)
        response = self.post("/api/v1/apps", name="Orders")
        self.assertEqual(response.status_code, 201, response.text)
        self.app_id = response.json()["id"]
        self.post(f"/api/v1/apps/{self.app_id}/scenarios", name="Show the order screen.")
        self.post("/api/v1/setup/builds/repos", repo=REPO)
        self.assertEqual(freeze_package(self.client, self.app_id).status_code, 200)
        with closing(self.db()) as db:
            self.revision = packages.latest(db, self.app_id)["revision"]

    # --- helpers ---

    def post(self, path, **values):
        return self.client.post(path, json=values)

    def page(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, response.text[:500])
        return response.json()

    def db(self):
        return builds.connect(self.database)

    def build(self, build_id=1):
        with closing(self.db()) as db:
            return dict(builds.get(db, build_id))

    def messages(self, build_id=1):
        with closing(self.db()) as db:
            return [dict(m) for m in builds.messages(db, build_id)]

    def start(self, key, **values):
        values = {"revision": self.revision, "repo": REPO, "provider": "fake", "budget": 5, "request_key": key,
                  **values}
        return self.post(f"/api/v1/apps/{self.app_id}/builds", **values)

    def started(self, response):
        """The session id a start answered."""
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["build"]["id"]

    def answer(self, build_id, answer, request_key, **values):
        return self.post(f"/api/v1/builds/{build_id}/messages", answer=answer, request_key=request_key, **values)

    def run_worker(self, jobs=1):
        return worker.run(self.database, once=True, ctx=self.ctx, max_jobs=jobs, lease=5)

    def running(self, key="one"):
        build_id = self.started(self.start(key))
        for _ in range(6):  # other sessions' polls are due too
            self.run_worker()
            if self.build(build_id)["state"] != "queued":
                break
        build = self.build(build_id)
        self.assertEqual(build["state"], "running")
        return build_id, build["provider_session_id"]

    # --- idempotency and isolation ---

    def test_duplicate_clicks_return_the_request_and_new_session_starts_another(self):
        first, second = self.start("click"), self.start("click")
        self.assertEqual(self.started(first), self.started(second))
        self.assertFalse(second.json()["created"])
        self.assertIn("already started session #1", second.json()["notice"])
        self.assertEqual(self.started(self.start("new-session")), 2)  # New session: a fresh key
        self.run_worker(jobs=4)
        self.assertEqual(self.world.starts, 2)

    def test_two_parallel_sessions_on_one_package_are_isolated(self):
        a, sid_a = self.running("a")
        b, sid_b = self.running("b")
        one, two = self.build(a), self.build(b)
        self.assertEqual(one["revision"], two["revision"])
        self.assertEqual((one["state"], two["state"]), ("running", "running"))
        self.assertNotEqual(one["branch"], two["branch"])
        self.assertNotEqual(one["workspace"], two["workspace"])
        self.assertEqual(self.world.sessions[sid_a]["branch"], one["branch"])
        self.assertIn(one["branch"], self.world.sessions[sid_a]["prompt"])
        self.world.ask(sid_a, "Only for A?")
        self.run_worker(jobs=2)
        self.assertEqual(self.answer(a, "Yes, only A.", "m1").status_code, 201)
        self.run_worker(jobs=3)
        self.assertEqual([r for r in self.world.replies], [(sid_a, "clarification", "Yes, only A.")])
        self.assertEqual(self.messages(b), [])  # separate conversations
        sessions = self.page(f"/api/v1/apps/{self.app_id}/builds")["sessions"]
        self.assertEqual({s["id"]: s["branch"] for s in sessions}, {a: one["branch"], b: two["branch"]})

    def test_an_improvement_session_starts_from_another_sessions_commit(self):
        a, sid = self.running("a")
        head = "d" * 40
        self.github.repos[REPO]["commits"] = {head}
        self.world.finish(sid, head)
        self.run_worker()
        self.assertEqual(self.build(a)["state"], "ready_for_verification")
        points = self.page(f"/api/v1/apps/{self.app_id}/builds")["starting_points"]
        self.assertEqual([(p["id"], p["head_sha"]) for p in points], [(a, head)])
        build = self.build(self.started(self.start("improve", start_from_build=a, provider="fake")))
        self.assertEqual((build["base_sha"], build["parent_build"]), (head, a))
        self.run_worker()
        prompt = [s for s in self.world.sessions.values() if s["id"] != sid][0]["prompt"]
        self.assertIn(f"build session #{a}", prompt)

    # --- the chat ---

    def test_delivery_uncertain_is_shown_and_never_resent(self):
        build_id, sid = self.running()
        self.world.ask(sid, "Zero quantity allowed?")
        self.run_worker()
        self.assertEqual(self.build(build_id)["state"], "waiting_for_answer")
        detail = self.page(f"/api/v1/builds/{build_id}")
        self.assertIn("Zero quantity allowed?", detail["questions"] + [m["body"] for m in detail["messages"]])
        self.world.reply_plan = ["lost"]
        first = self.answer(build_id, "No.", "k1")
        second = self.answer(build_id, "No.", "k1")  # a double click
        self.assertEqual((first.json()["created"], second.json()["created"]), (True, False))
        self.run_worker(jobs=5)
        sent = [m for m in self.messages(build_id) if m["direction"] == "to_provider"]
        self.assertEqual([m["state"] for m in sent], ["delivery_uncertain"])
        self.assertEqual(len(self.world.replies), 1)
        shown = [m for m in self.page(f"/api/v1/builds/{build_id}")["messages"] if m["direction"] == "to_provider"]
        self.assertEqual([m["state_label"] for m in shown], ["Delivery uncertain"])

    def test_a_worker_that_died_mid_send_leaves_delivery_uncertain(self):
        build_id, sid = self.running()
        self.world.ask(sid, "Which currency?")
        self.run_worker()
        self.answer(build_id, "EUR", "k")
        with closing(self.db()) as db:  # the worker marked it sending, then died before the answer
            db.execute("UPDATE build_messages SET state = 'sending', sent_at = ? WHERE direction = 'to_provider'",
                       (builds.now(),))
            db.commit()
        self.run_worker(jobs=3)
        self.assertEqual(self.messages(build_id)[-1]["state"], "delivery_uncertain")
        self.assertEqual(self.world.replies, [])

    def test_a_reply_never_acknowledged_becomes_uncertain_after_the_timeout(self):
        build_id, sid = self.running()
        self.world.ask(sid, "Q?")
        self.run_worker()
        self.world.reply_plan = ["silent", "ok"]
        self.answer(build_id, "first", "k1")
        self.run_worker(jobs=2)
        self.assertEqual(self.messages(build_id)[-1]["state"], "sent")
        with mock.patch.object(builds, "ACK_TIMEOUT_SECONDS", -1):
            self.run_worker()
        self.assertEqual(self.messages(build_id)[-1]["state"], "delivery_uncertain")
        self.world.ask(sid, "Q2?")
        self.run_worker()
        self.answer(build_id, "second", "k2")
        self.run_worker(jobs=3)
        self.assertEqual(self.messages(build_id)[-1]["state"], "acknowledged")

    def test_a_scope_change_proposes_a_new_revision_and_is_not_sent(self):
        build_id, sid = self.running()
        self.world.ask(sid, "Should cancelled orders be listed?")
        self.run_worker()
        response = self.answer(build_id, "Yes, and they can be restored.", "scope", scope_change=True)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertIn("Not sent", response.json()["notice"])
        self.run_worker(jobs=2)
        message = self.messages(build_id)[-1]
        self.assertEqual((message["kind"], message["state"]), ("scope_change", "held_for_review"))
        self.assertEqual(self.world.replies, [])
        self.assertEqual(self.build(build_id)["revision"], self.revision)  # the session's revision is untouched
        preview = self.client.get(f"/api/v1/apps/{self.app_id}/package-draft")
        self.assertEqual(preview.status_code, 200)
        self.assertIn(f"Agreed in build session #{build_id}", preview.text)
        self.assertEqual(freeze_package(self.client, self.app_id).status_code, 200)
        with closing(self.db()) as db:
            revs = packages.revisions(db, self.app_id)
            self.assertEqual([r["rev"] for r in revs], [2, 1])
            self.assertEqual(revs[1]["revision"], self.revision)
            self.assertIn(f"Agreed in build session #{build_id}", revs[0]["summary"])
            actions = [r["action"] for r in db.execute("SELECT action FROM audit_log ORDER BY id")]
        self.assertIn("build.scope_change", actions)

    def test_tool_approvals_are_a_separate_control(self):
        build_id, sid = self.running()
        self.world.ask_tool(sid, "Bash", {"command": "npm test"})
        self.run_worker()
        asked = [m for m in self.messages(build_id) if m["kind"] == "tool_approval"][0]
        detail = self.page(f"/api/v1/builds/{build_id}")
        self.assertIn({"id": asked["id"], "kind": "tool_approval"}, detail["outstanding"])  # Approve / Deny
        path = f"/api/v1/builds/{build_id}/tool-decisions/{asked['id']}"
        self.assertEqual(self.post(path, decision="approve", request_key=f"tool:{asked['id']}").status_code, 200)
        self.post(path, decision="deny", request_key="")  # already decided: ignored
        self.run_worker(jobs=3)
        self.assertEqual(self.world.replies, [(sid, "tool_approval", "approve")])
        shown = [m for m in self.page(f"/api/v1/builds/{build_id}")["messages"] if m["id"] == asked["id"]][0]
        self.assertEqual(shown["decision"], "approve")

    # --- start, cancel, restart ---

    def test_an_uncertain_start_is_looked_up_not_repeated(self):
        self.world.start_plan = ["uncertain"]
        self.start("k")
        self.run_worker()
        self.assertEqual(self.build()["state"], "checking_started")
        self.run_worker()
        self.assertEqual((self.build()["state"], self.world.starts), ("running", 1))

    def test_a_lost_start_is_made_again_only_after_checking(self):
        self.world.start_plan = ["lost"]
        self.start("k")
        self.run_worker()
        self.assertEqual((self.build()["state"], self.world.starts), ("checking_started", 1))
        self.run_worker(jobs=2)
        self.assertEqual((self.build()["state"], self.world.starts, len(self.world.sessions)), ("running", 2, 1))

    def test_a_worker_restart_picks_the_session_up_without_a_second_start(self):
        self.start("k")
        with closing(self.db()) as db:
            build = builds.get(db, 1)
            spec = builds.spec_of(db, build)
            # the dead worker held the create token and its start reached the provider
            fake_provider.FakeProvider().start(spec, {})
            db.execute("UPDATE builds SET create_token = 'dead-worker', state = 'starting' WHERE id = 1")
            db.execute("UPDATE jobs SET state = 'running', lease_owner = 'dead', lease_until = ? WHERE type = 'build'",
                       (jobqueue.later(-1),))
            db.commit()
        self.run_worker()
        build = self.build()
        self.assertEqual((build["state"], self.world.starts), ("running", 1))
        self.assertEqual(build["provider_session_id"], next(iter(self.world.sessions)))

    def test_cancel_and_interrupt(self):
        build_id, sid = self.running()
        self.assertEqual(self.post(f"/api/v1/builds/{build_id}/interrupt").json()["notice"], "Interrupt requested.")
        self.run_worker(jobs=2)
        self.assertTrue(self.world.sessions[sid]["interrupted"])
        self.assertEqual(self.post(f"/api/v1/builds/{build_id}/cancel").json()["notice"], "Cancel requested.")
        self.run_worker(jobs=3)
        self.assertTrue(self.world.sessions[sid]["cancelled"])
        self.assertEqual(self.build(build_id)["state"], "cancelled")
        shown = self.page(f"/api/v1/builds/{build_id}")["build"]
        self.assertEqual((shown["state"], shown["final"]), ("cancelled", True))  # it can be retried

    # --- publication stays coordinated ---

    def tested_release(self, release_id=1, sha="a" * 40, build_id=None):
        with closing(self.db()) as db:
            db.execute("""INSERT INTO releases(id, app_id, version, revision, commit_sha, archive, archive_sha, state,
                          build_id) VALUES (?, ?, ?, ?, ?, ?, ?, 'tested', ?)""",
                       (release_id, self.app_id, sha[:12], self.revision, sha, b"zip",
                        hashlib.sha256(b"zip").hexdigest(), build_id))
            db.commit()

    def test_one_publish_per_application_at_a_time(self):
        self.tested_release(1, "a" * 40)
        self.tested_release(2, "e" * 40)
        with closing(self.db()) as db:
            portal.acquire_publish(db, self.app_id, 2, "someone-else")
            with self.assertRaises(portal.PublishConflict):
                portal.acquire_publish(db, self.app_id, 1, "me")
        response = self.post("/api/v1/releases/1/publish")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "publish_locked")
        self.assertIn("being published", response.json()["detail"])
        with closing(self.db()) as db:
            self.assertEqual(db.execute("SELECT state FROM releases WHERE id = 1").fetchone()[0], "tested")
            db.execute("UPDATE publish_locks SET expires_at = ?", (jobqueue.later(-1),))  # the holder died
            db.commit()
        response = self.post("/api/v1/releases/1/publish")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["release"]["state"], "published")
        with closing(self.db()) as db:
            self.assertEqual(db.execute("SELECT state FROM releases WHERE id = 1").fetchone()[0], "published")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM publish_locks").fetchone()[0], 0)
        self.assertEqual(self.post("/api/v1/releases/1/publish").status_code, 400)  # final

    def test_a_session_cannot_take_over_another_sessions_release(self):
        a, _ = self.running("a")
        b, _ = self.running("b")
        self.tested_release(1, "a" * 40, build_id=a)
        with closing(self.db()) as db:
            with self.assertRaises(ValueError) as caught:
                verification.package_commit(db, self.app_id, REPO, "a" * 40, None, self.revision, 5, "x", build_id=b)
            self.assertIn(f"build session #{a}", str(caught.exception))
            self.assertEqual(verification.package_commit(db, self.app_id, REPO, "a" * 40, None, self.revision,
                                                         build_id=a), 1)


class CompareTest(unittest.TestCase):
    """Compare results: the release-verification checks against each candidate's commit."""

    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        from twinlab2 import verifications
        self.addCleanup(verifications.join_in_process)
        self.gh = test_verification.FakeGitHub()
        self.addCleanup(self.gh.close)
        self.database = new_database(self.work.name)
        self.app = create_app(self.database)
        self.app.config.update(GITHUB=verification.GitHub(token="test-token", api=self.gh.url),
                               TWINLAB_RUNNER=test_verification.FakeTwinLab())
        self.client = Client(self.app)
        response = self.client.post("/api/v1/apps", json={"name": "Billing"})
        self.assertEqual(response.status_code, 201, response.text)
        self.app_id = response.json()["id"]
        self.client.post(f"/api/v1/apps/{self.app_id}/scenarios", json={"name": "Save an invoice."})
        freeze_package(self.client, self.app_id)
        with closing(connect(self.database)) as db:
            revision = packages.latest(db, self.app_id)["revision"]
            for build_id, (provider, head) in enumerate((("devin", "1" * 40), ("claude", "2" * 40)), 1):
                db.execute("""INSERT INTO builds(id, app_id, revision, pack_sha256, repo, base_branch, base_sha, attempt,
                              change_summary, acu_budget, state, idempotency_tag, requested_by, created_at, updated_at,
                              provider, head_sha, branch, request_key, cost, cost_unit)
                              VALUES (?, ?, ?, '', ?, 'main', ?, ?, '', 5, 'ready_for_verification', ?, 'local', '', '',
                              ?, ?, ?, ?, 1.0, 'USD')""",
                           (build_id, self.app_id, revision, test_verification.REPO, "0" * 40, build_id,
                            f"tag-{build_id}", provider, head, f"twinlab2/app-1/session-{build_id}-{provider}",
                            f"k{build_id}"))
            db.commit()
        for build_id, (provider, head) in enumerate((("devin", "1" * 40), ("claude", "2" * 40)), 1):
            self.gh.runs[head] = [test_verification.run("build", "success", sha=head)]
            # each session's branch still points at its commit (a moved branch is refused)
            self.gh.branch_heads[f"twinlab2%2Fapp-1%2Fsession-{build_id}-{provider}"] = head

    def test_the_same_checks_run_against_each_candidate(self):
        before = self.client.get(f"/api/v1/apps/{self.app_id}/builds/compare?session=1&session=2")
        self.assertEqual(before.status_code, 200, before.text)
        self.assertEqual([(c["build"]["id"], c["release"], c["tested"]) for c in before.json()["results"]],
                         [(1, None, False), (2, None, False)])  # not verified yet
        response = self.client.post(f"/api/v1/apps/{self.app_id}/builds/compare", json={"sessions": [1, 2]})
        self.assertEqual(response.status_code, 200, response.text[:400])
        self.assertEqual(response.json().get("errors"), [])
        # one verification per candidate, queued for the worker (here: worked in this process)
        queued = response.json()["verifications"]
        self.assertEqual([v["kind"] for v in queued], ["commit", "commit"])
        for item in queued:
            done = wait_verification(self.client, mock.Mock(status_code=202, json=lambda item=item: item))
            self.assertEqual(done["state"], "failed")  # no deployment yet
        with closing(connect(self.database)) as db:
            rows = db.execute("SELECT build_id, commit_sha, state FROM releases ORDER BY build_id").fetchall()
            self.assertEqual([(r[0], r[1]) for r in rows], [(1, "1" * 40), (2, "2" * 40)])
            reports = db.execute("SELECT COUNT(*) FROM release_artifacts WHERE kind = 'verification_report'").fetchone()[0]
        self.assertEqual(reports, 2)
        results = self.client.get(f"/api/v1/apps/{self.app_id}/builds/compare?session=1&session=2").json()["results"]
        self.assertEqual([c["build"]["id"] for c in results], [1, 2])  # both candidates
        for candidate in results:
            self.assertIn({"name": "build", "conclusion": "success"}, candidate["checks"])
            # no deployment of either commit yet: both stay Packaged
            self.assertFalse(candidate["tested"])
            self.assertEqual(candidate["release"]["state"], "packaged")
            self.assertTrue(any("No deployment reports" in r for r in candidate["reasons"]), candidate["reasons"])


class MigrationTest(unittest.TestCase):
    def test_devin_builds_become_devin_sessions(self):
        with tempfile.TemporaryDirectory() as work:
            with closing(connect(new_database(work))) as db:
                migrations.run(db)
                db.execute("INSERT INTO apps(id, name, git_url, requirements) VALUES (1, 'A', '', '')")
                db.execute("INSERT INTO briefs(revision, app_id, manifest) VALUES ('r', 1, '{}')")
                db.execute("INSERT INTO frozen_packs(revision, app_id, sha256, frozen_by, frozen_at) VALUES ('r', 1, 's', 'o', '')")
                db.execute("""INSERT INTO builds(id, app_id, revision, pack_sha256, repo, base_branch, base_sha, attempt,
                              change_summary, acu_budget, state, active_app, idempotency_tag, requested_by, created_at,
                              updated_at, devin_session_id, devin_url, acus_consumed)
                              VALUES (1, 1, 'r', 's', 'o/r', 'main', '', 1, '', 9, 'running', 1, 't', 'u', '', '',
                              'devin-1', 'https://app.devin.ai/sessions/1', 2.5)""")
                db.execute("INSERT INTO build_messages(build_id, author, body, state, created_at, updated_at) "
                           "VALUES (1, 'u', 'hi', 'uncertain', '', '')")
                migrations.BUILD_SESSIONS[-1](db)
                db.commit()
                row = db.execute("SELECT * FROM builds").fetchone()
                self.assertEqual((row["provider"], row["provider_session_id"], row["budget"], row["cost"], row["active_app"]),
                                 ("devin", "devin-1", 9, 2.5, None))
                self.assertEqual(db.execute("SELECT state FROM build_messages").fetchone()[0], "delivery_uncertain")


if __name__ == "__main__":
    unittest.main()
