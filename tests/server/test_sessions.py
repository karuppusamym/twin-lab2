"""Build sessions: several attempts per package, the session chat, isolation and coordination.

The provider is tests/fake_provider.py (in memory); GitHub is the local fake in fakes.py. Nothing
here talks to Devin, Anthropic, GitHub or Copilot, and nothing spends money.
"""

from contextlib import closing
import hashlib
import json
import os
import re
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
from support import connect, freeze as freeze_package, new_database
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
        self.app.testing = True
        self.client = self.app.test_client()
        self.client.get("/")
        with self.client.session_transaction() as session:
            self.token = session["form_token"]
        self.app_id = int(self.post("/apps", name="Orders").location.rstrip("/").rsplit("/", 1)[-1])
        self.post(f"/apps/{self.app_id}/scenarios", name="Show the order screen.")
        self.post("/setup/builds/repos", repo=REPO)
        self.assertEqual(freeze_package(self.client, self.token, f"/apps/{self.app_id}").status_code, 200)
        with closing(self.db()) as db:
            self.revision = packages.latest(db, self.app_id)["revision"]

    # --- helpers ---

    def post(self, path, **values):
        return self.client.post(path, data={"token": self.token, **values})

    def page(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, response.data[:500])
        return response.data.decode()

    def db(self):
        return builds.connect(self.database)

    def build(self, build_id=1):
        with closing(self.db()) as db:
            return dict(builds.get(db, build_id))

    def messages(self, build_id=1):
        with closing(self.db()) as db:
            return [dict(m) for m in builds.messages(db, build_id)]

    def start(self, key, **values):
        values = {"revision": self.revision, "repo": REPO, "provider": "fake", "budget": "5", "request_key": key,
                  **values}
        return self.post(f"/apps/{self.app_id}/build", **values)

    def run_worker(self, jobs=1):
        return worker.run(self.database, once=True, ctx=self.ctx, max_jobs=jobs, lease=5)

    def running(self, key="one"):
        location = self.start(key).location
        build_id = int(location.rsplit("/", 1)[-1])
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
        self.assertEqual(first.location, second.location)
        self.assertIn("already started session #1", self.page(second.location))
        self.assertEqual(self.start("new-session").location, "/builds/2")  # New session: a fresh key
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
        self.post(f"/builds/{a}/answer", answer="Yes, only A.", request_key="m1")
        self.run_worker(jobs=3)
        self.assertEqual([r for r in self.world.replies], [(sid_a, "clarification", "Yes, only A.")])
        self.assertEqual(self.messages(b), [])  # separate conversations
        page = self.page(f"/apps/{self.app_id}/build")
        self.assertIn(one["branch"], page)
        self.assertIn(two["branch"], page)

    def test_an_improvement_session_starts_from_another_sessions_commit(self):
        a, sid = self.running("a")
        head = "d" * 40
        self.github.repos[REPO]["commits"] = {head}
        self.world.finish(sid, head)
        self.run_worker()
        self.assertEqual(self.build(a)["state"], "ready_for_verification")
        self.assertIn(f'value="session:{a}"', self.page(f"/apps/{self.app_id}/build"))
        response = self.start("improve", start_commit=f"session:{a}", provider="fake")
        build = self.build(int(response.location.rsplit("/", 1)[-1]))
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
        self.assertIn("Zero quantity allowed?", self.page(f"/builds/{build_id}"))
        self.world.reply_plan = ["lost"]
        self.post(f"/builds/{build_id}/answer", answer="No.", request_key="k1")
        self.post(f"/builds/{build_id}/answer", answer="No.", request_key="k1")  # a double click
        self.run_worker(jobs=5)
        sent = [m for m in self.messages(build_id) if m["direction"] == "to_provider"]
        self.assertEqual([m["state"] for m in sent], ["delivery_uncertain"])
        self.assertEqual(len(self.world.replies), 1)
        self.assertIn("Delivery uncertain", self.page(f"/builds/{build_id}"))

    def test_a_worker_that_died_mid_send_leaves_delivery_uncertain(self):
        build_id, sid = self.running()
        self.world.ask(sid, "Which currency?")
        self.run_worker()
        self.post(f"/builds/{build_id}/answer", answer="EUR", request_key="k")
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
        self.post(f"/builds/{build_id}/answer", answer="first", request_key="k1")
        self.run_worker(jobs=2)
        self.assertEqual(self.messages(build_id)[-1]["state"], "sent")
        with mock.patch.object(builds, "ACK_TIMEOUT_SECONDS", -1):
            self.run_worker()
        self.assertEqual(self.messages(build_id)[-1]["state"], "delivery_uncertain")
        self.world.ask(sid, "Q2?")
        self.run_worker()
        self.post(f"/builds/{build_id}/answer", answer="second", request_key="k2")
        self.run_worker(jobs=3)
        self.assertEqual(self.messages(build_id)[-1]["state"], "acknowledged")

    def test_a_scope_change_proposes_a_new_revision_and_is_not_sent(self):
        build_id, sid = self.running()
        self.world.ask(sid, "Should cancelled orders be listed?")
        self.run_worker()
        self.post(f"/builds/{build_id}/answer", answer="Yes, and they can be restored.", scope_change="1",
                  request_key="scope")
        self.run_worker(jobs=2)
        message = self.messages(build_id)[-1]
        self.assertEqual((message["kind"], message["state"]), ("scope_change", "held_for_review"))
        self.assertEqual(self.world.replies, [])
        self.assertEqual(self.build(build_id)["revision"], self.revision)  # the session's revision is untouched
        preview = self.page(f"/apps/{self.app_id}/build-pack/preview")
        self.assertIn(f"Agreed in build session #{build_id}", preview)
        self.assertEqual(freeze_package(self.client, self.token, f"/apps/{self.app_id}").status_code, 200)
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
        page = self.page(f"/builds/{build_id}")
        self.assertIn("Approve", page)
        asked = [m for m in self.messages(build_id) if m["kind"] == "tool_approval"][0]
        self.post(f"/builds/{build_id}/tools/{asked['id']}", decision="approve", request_key=f"tool:{asked['id']}")
        self.post(f"/builds/{build_id}/tools/{asked['id']}", decision="deny")  # already decided: ignored
        self.run_worker(jobs=3)
        self.assertEqual(self.world.replies, [(sid, "tool_approval", "approve")])
        self.assertIn("You chose approve", self.page(f"/builds/{build_id}"))

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
        self.post(f"/builds/{build_id}/interrupt")
        self.run_worker(jobs=2)
        self.assertTrue(self.world.sessions[sid]["interrupted"])
        self.post(f"/builds/{build_id}/cancel")
        self.run_worker(jobs=3)
        self.assertTrue(self.world.sessions[sid]["cancelled"])
        self.assertEqual(self.build(build_id)["state"], "cancelled")
        self.assertIn("Retry", self.page(f"/builds/{build_id}"))

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
        response = self.post("/releases/1/publish")
        self.assertEqual(response.status_code, 409)
        self.assertIn(b"being published", response.data)
        with closing(self.db()) as db:
            self.assertEqual(db.execute("SELECT state FROM releases WHERE id = 1").fetchone()[0], "tested")
            db.execute("UPDATE publish_locks SET expires_at = ?", (jobqueue.later(-1),))  # the holder died
            db.commit()
        self.assertEqual(self.post("/releases/1/publish").status_code, 302)
        with closing(self.db()) as db:
            self.assertEqual(db.execute("SELECT state FROM releases WHERE id = 1").fetchone()[0], "published")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM publish_locks").fetchone()[0], 0)
        self.assertEqual(self.post("/releases/1/publish").status_code, 400)  # final

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
        self.gh = test_verification.FakeGitHub()
        self.addCleanup(self.gh.close)
        self.database = new_database(self.work.name)
        self.app = create_app(self.database)
        self.app.testing = True
        self.app.config.update(GITHUB=verification.GitHub(token="test-token", api=self.gh.url),
                               TWINLAB_RUNNER=test_verification.FakeTwinLab())
        self.client = self.app.test_client()
        self.client.get("/")
        with self.client.session_transaction() as session:
            self.token = session["form_token"]
        post = lambda path, **v: self.client.post(path, data={"token": self.token, **v})  # noqa: E731
        self.post = post
        self.app_id = int(post("/apps", name="Billing").location.rstrip("/").rsplit("/", 1)[-1])
        post(f"/apps/{self.app_id}/scenarios", name="Save an invoice.")
        freeze_package(self.client, self.token, f"/apps/{self.app_id}")
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
        page = self.client.get(f"/apps/{self.app_id}/sessions/compare?ids=1,2").data.decode()
        self.assertIn("not verified yet", page)
        response = self.post(f"/apps/{self.app_id}/sessions/compare", session=["1", "2"])
        self.assertEqual(response.status_code, 302, response.data[:400])
        with closing(connect(self.database)) as db:
            rows = db.execute("SELECT build_id, commit_sha, state FROM releases ORDER BY build_id").fetchall()
            self.assertEqual([(r[0], r[1]) for r in rows], [(1, "1" * 40), (2, "2" * 40)])
            reports = db.execute("SELECT COUNT(*) FROM release_artifacts WHERE kind = 'verification_report'").fetchone()[0]
        self.assertEqual(reports, 2)
        page = self.client.get(response.location).data.decode()
        self.assertIn("candidate-1", page)
        self.assertIn("candidate-2", page)
        self.assertIn("build: success", page)
        self.assertIn("No deployment reports", page)  # no deployment of either commit yet: both stay Packaged


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
