"""Build with Devin: durable jobs, the build binding, ambiguous creates, questions, cancel.

Every Devin and GitHub call goes to the local fakes in fakes.py (127.0.0.1): the tests set
DEVIN_BASE_URL and TWINLAB2_GITHUB_API to them and check that before anything runs.
"""

from contextlib import closing
import hashlib
import io
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from zipfile import ZipFile

from agent import twinlab
import builds
import jobqueue
from portal import create_app
import worker
from fakes import FakeDevin, FakeGitHub
from db import IntegrityError
from support import new_database

PROJECT = Path(__file__).resolve().parents[1]
REPO = "acme/orders-demo"
BASE = "b" * 40
HEAD = "c" * 40


def pinned_labctl_available():
    try:
        twinlab.verify()
        return True
    except (twinlab.PinError, OSError):
        return False


@unittest.skipUnless(pinned_labctl_available(), "the pinned Twin Lab is not vendored: python -m agent.twinlab fetch")
class BuildTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.database = new_database(self.work.name)
        self.devin = FakeDevin().start()
        self.github = FakeGitHub().start()
        self.addCleanup(self.devin.stop)
        self.addCleanup(self.github.stop)
        self.github.add_repo(REPO, sha=BASE)
        self.env = {"DEVIN_API_KEY": self.devin.key, "DEVIN_ORG_ID": self.devin.org, "DEVIN_BASE_URL": self.devin.url,
                    "TWINLAB2_GITHUB_TOKEN": self.github.token, "TWINLAB2_GITHUB_API": self.github.url}
        patch = mock.patch.dict(os.environ, self.env)
        patch.start()
        self.addCleanup(patch.stop)
        for name in ("TWINLAB2_PASSWORD", "TWINLAB2_TRUST_PROXY_USER"):
            os.environ.pop(name, None)
        for url in (self.devin.url, self.github.url):
            self.assertTrue(url.startswith("http://127.0.0.1:"), url)
        self.ctx = builds.Context(poll_seconds=0, reconcile_delay=0, reconcile_checks=1, timeout=2)
        self.client = self.portal()

    # --- helpers ---

    def portal(self):
        app = create_app(self.database)
        app.testing = True
        client = app.test_client()
        self.assertEqual(client.get("/").status_code, 200)
        with client.session_transaction() as session:
            self.token = session["form_token"]
        return client

    def post(self, path, **values):
        return self.client.post(path, data={"token": self.token, **values})

    def page(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, response.data[:500])
        return response.data.decode()

    def ready_app(self, connect=True, freeze=True):
        app_id = int(self.post("/apps", name="Orders").location.rstrip("/").rsplit("/", 1)[-1])
        self.assertEqual(self.post(f"/apps/{app_id}/scenarios", name="Show the order screen.").status_code, 302)
        if connect:
            self.post("/setup/builds/repos", repo=REPO)
        revision = self.revision(app_id)
        if freeze:
            self.assertEqual(self.post(f"/apps/{app_id}/build/freeze", revision=revision).status_code, 302)
        return app_id, revision

    def revision(self, app_id):
        return re.search(r'name="revision" value="([0-9a-f]{64})"', self.page(f"/apps/{app_id}/build")).group(1)

    def request_build(self, app_id, revision, **values):
        return self.post(f"/apps/{app_id}/build", revision=revision, repo=REPO, base_branch="", acu_budget="7", **values)

    def run_worker(self, jobs=1, **kwargs):
        return worker.run(self.database, once=True, ctx=self.ctx, max_jobs=jobs, lease=5, **kwargs)

    def db(self):
        db = builds.connect(self.database)
        self.addCleanup(db.close)
        return db

    def build(self, build_id=1):
        with closing(builds.connect(self.database)) as db:
            return dict(builds.get(db, build_id))

    # --- tests ---

    def test_a_pack_that_is_not_frozen_is_not_built(self):
        app_id, revision = self.ready_app(freeze=False)
        self.assertIn("Freeze this pack", self.page(f"/apps/{app_id}/build"))
        response = self.request_build(app_id, revision)
        self.assertEqual(response.status_code, 302)
        self.assertIn("Freeze the build pack before building", self.page(f"/apps/{app_id}/build"))
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM builds").fetchone()[0], 0)
        self.assertEqual(self.devin.requests, [])

    def test_build_binds_pack_repo_commit_session_and_pull_request(self):
        app_id, revision = self.ready_app()
        page = self.page(f"/apps/{app_id}/build")
        self.assertIn("BUILD_TASK.md", page)  # the exact pack preview
        self.assertIn("Build with Devin", page)
        response = self.request_build(app_id, revision)
        self.assertEqual(response.location, "/builds/1")
        self.assertIn("Queued", self.page("/builds/1"))
        self.run_worker()
        build = self.build()
        session = self.devin.only_session()
        frozen = self.db().execute("SELECT sha256 FROM frozen_packs").fetchone()[0]
        self.assertEqual((build["state"], build["revision"], build["pack_sha256"], build["repo"], build["base_branch"],
                          build["base_sha"], build["attempt"], build["acu_budget"]),
                         ("running", revision, frozen, REPO, "main", BASE, 1, 7))
        self.assertEqual(build["devin_session_id"], session["session_id"])
        self.assertEqual(session["max_acu_limit"], 7)
        self.assertEqual(session["repos"], [REPO])
        self.assertIn(build["idempotency_tag"], session["tags"])
        self.assertIn(BASE, session["prompt"])
        uploaded = self.devin.attachments[session["attachment_urls"][0].rsplit("/", 1)[-1]]  # the multipart body
        archive = builds.stored_archive(self.db(), build["app_id"], revision)  # the pack's artifact
        self.assertEqual(hashlib.sha256(archive).hexdigest(), frozen)
        self.assertIn(archive, uploaded)
        self.assertIn("BUILD_TASK.md", ZipFile(io.BytesIO(archive)).namelist())
        self.assertIn(session["url"], self.page("/builds/1"))

        self.devin.finish(session["session_id"], pr=12)
        self.github.add_pull(REPO, 12, HEAD)
        self.run_worker()
        build = self.build()
        self.assertEqual((build["state"], build["pr_number"], build["head_sha"], build["active_app"]),
                         ("ready_for_verification", 12, HEAD, None))
        page = self.page("/builds/1")
        self.assertIn("Ready for verification", page)
        self.assertIn(HEAD, page)
        self.assertNotIn(">Tested<", page)
        self.assertNotIn("Cancel build", page)
        verify = re.search(r'<form[^>]*id="verify-form".*?</form>', page, re.S).group(0)
        self.assertIn(f'action="/apps/{app_id}/verify-commit"', verify)
        self.assertIn(f'name="sha" value="{HEAD}"', verify)
        self.assertIn('name="pr_number" value="12"', verify)

    def test_answering_a_question_sends_it_to_the_session(self):
        app_id, revision = self.ready_app()
        self.request_build(app_id, revision)
        self.run_worker()
        sid = self.devin.only_session()["session_id"]
        self.devin.ask(sid, "Should closed orders be searchable?")
        self.run_worker()
        self.assertEqual(self.build()["state"], "waiting_for_answer")
        page = self.page("/builds/1")
        self.assertIn("Waiting for your answer", page)
        self.assertIn("Should closed orders be searchable?", page)
        self.assertIn('name="answer"', page)
        self.post("/builds/1/answer", answer="Yes, read-only.")
        self.assertIn("(sending)", self.page("/builds/1"))
        self.run_worker(jobs=2)
        self.assertEqual(self.devin.sessions[sid]["messages"][-1],
                         {"source": "user", "message": "Yes, read-only."})
        build = self.build()
        self.assertEqual(build["state"], "running")
        self.assertIn("(sent)", self.page("/builds/1"))

    def test_duplicate_build_clicks_make_one_build(self):
        app_id, revision = self.ready_app()
        first = self.request_build(app_id, revision)
        second = self.request_build(app_id, revision)
        self.assertEqual(first.location, second.location)
        self.assertIn("already in progress", self.page(second.location))
        db = self.db()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM builds").fetchone()[0], 1)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE type = 'build'").fetchone()[0], 1)
        # the database refuses a second active build even when the check is skipped
        row = dict(builds.get(db, 1))
        row.update(id=None, idempotency_tag="another")
        with self.assertRaises(IntegrityError):
            db.execute(f"INSERT INTO builds ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})", list(row.values()))
        db.rollback()
        self.run_worker(jobs=3)
        self.assertEqual(self.devin.create_calls, 1)

    def test_a_create_that_timed_out_is_found_by_its_tag_not_repeated(self):
        for plan in ("slow", "fail_after_create"):
            with self.subTest(plan):
                self.setUp()
                self.ctx.timeout = 0.5
                self.devin.slow_seconds = 2
                self.devin.create_plan = [plan]
                app_id, revision = self.ready_app()
                self.request_build(app_id, revision)
                self.run_worker()
                self.assertEqual(self.build()["state"], "checking_started")
                self.assertIn("Checking whether the build started", self.page("/builds/1"))
                self.run_worker()
                build = self.build()
                self.assertEqual(build["state"], "running")
                self.assertEqual(self.devin.create_calls, 1)
                self.assertEqual(build["devin_session_id"], self.devin.only_session()["session_id"])

    def test_a_create_that_failed_without_a_session_is_retried_once_checked(self):
        self.devin.create_plan = ["fail"]
        app_id, revision = self.ready_app()
        self.request_build(app_id, revision)
        self.run_worker()
        self.assertEqual(self.build()["state"], "checking_started")
        self.assertEqual(self.devin.sessions, {})
        listed = [r for r in self.devin.requests if r == ("GET", "/v3/organizations/org-test/sessions")]
        self.assertEqual(listed, [])  # nothing is looked up or created before the next job
        self.run_worker(jobs=2)
        build = self.build()
        self.assertEqual(build["state"], "running")
        self.assertEqual(self.devin.create_calls, 2)
        self.assertEqual(len(self.devin.sessions), 1)
        self.assertEqual(build["create_calls"], 2)
        self.assertIn(("GET", "/v3/organizations/org-test/sessions"), self.devin.requests)

    def test_several_lookups_before_a_retry(self):
        self.ctx.reconcile_checks = 3
        self.devin.create_plan = ["fail"]
        app_id, revision = self.ready_app()
        self.request_build(app_id, revision)
        self.run_worker(jobs=3)
        self.assertEqual((self.build()["state"], self.devin.create_calls), ("checking_started", 1))
        self.run_worker(jobs=2)
        self.assertEqual((self.build()["state"], self.devin.create_calls), ("running", 2))

    def test_a_worker_killed_during_the_create_is_recovered_without_a_second_session(self):
        self.devin.create_plan = ["hold"]
        app_id, revision = self.ready_app()
        self.request_build(app_id, revision)
        env = {**os.environ, **self.env, "TWINLAB2_RECONCILE_SECONDS": "0", "TWINLAB2_RECONCILE_CHECKS": "1",
               "TWINLAB2_POLL_SECONDS": "0"}
        proc = subprocess.Popen([sys.executable, str(PROJECT / "worker.py"), "--database", str(self.database),
                                 "--lease", "1", "--idle", "0.1"], cwd=PROJECT, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        self.assertTrue(self.devin.received.wait(30), "the worker never reached Devin")
        proc.kill()
        proc.wait(10)
        self.devin.release.set()
        build = self.build()
        self.assertEqual(build["state"], "starting")
        self.assertIsNotNone(build["create_token"])
        self.assertIsNone(build["devin_session_id"])
        time.sleep(1.2)  # the dead worker's lease runs out
        self.run_worker()
        build = self.build()
        self.assertEqual(build["state"], "running")
        self.assertEqual(self.devin.create_calls, 1)
        self.assertEqual(build["devin_session_id"], self.devin.only_session()["session_id"])
        job = self.db().execute("SELECT attempts FROM jobs WHERE type = 'build'").fetchone()
        self.assertEqual(job[0], 0)  # rescheduled to poll: the count starts again
        self.assertIn("stopped", self.page("/setup/builds"))

    def test_a_portal_restart_keeps_builds_and_their_jobs(self):
        app_id, revision = self.ready_app()
        self.request_build(app_id, revision)
        self.client = self.portal()  # a new portal process on the same catalog
        self.assertIn("Queued", self.page("/builds/1"))
        self.assertEqual(self.request_build(app_id, revision).location, "/builds/1")
        self.run_worker()
        self.client = self.portal()
        self.assertIn("Running", self.page("/builds/1"))
        self.assertEqual(self.devin.create_calls, 1)

    def test_an_expired_devin_key_fails_clearly_and_retry_works_after_the_fix(self):
        app_id, revision = self.ready_app()
        os.environ["DEVIN_API_KEY"] = "cog_expired"
        self.request_build(app_id, revision)
        self.run_worker()
        build = self.build()
        self.assertEqual(build["state"], "failed")
        self.assertIn("refused the key", build["error"])
        self.assertIn("DEVIN_API_KEY", build["error"])
        page = self.page("/builds/1")
        self.assertIn("refused the key", page)
        self.assertIn("Retry", page)
        self.assertEqual(self.devin.create_calls, 0)
        os.environ["DEVIN_API_KEY"] = self.devin.key
        self.assertEqual(self.post("/builds/1/retry").location, "/builds/2")
        self.run_worker()
        self.assertEqual((self.build(2)["state"], self.build(2)["attempt"]), ("running", 2))

    def test_an_expired_github_token_is_explained_and_starts_nothing(self):
        app_id, revision = self.ready_app()
        os.environ["TWINLAB2_GITHUB_TOKEN"] = "ghp_expired"
        self.request_build(app_id, revision)
        self.assertIn("GitHub refused the token", self.page(f"/apps/{app_id}/build"))
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM builds").fetchone()[0], 0)
        self.post("/setup/builds/repos", repo="acme/other")
        self.assertIn("GitHub refused the token", self.page("/setup/builds"))

    def test_setup_never_shows_secrets(self):
        os.environ.pop("DEVIN_API_KEY")
        os.environ.pop("TWINLAB2_GITHUB_TOKEN")
        self.post("/setup/builds/settings", DEVIN_API_KEY="cog_saved_secret_value", GITHUB_TOKEN="ghp_saved_secret",
                  DEVIN_ORG_ID="org-test", DEVIN_BASE_URL=self.devin.url, GITHUB_API=self.github.url,
                  DEFAULT_ACU_BUDGET="9")
        page = self.page("/setup/builds")
        self.assertNotIn("cog_saved_secret_value", page)
        self.assertNotIn("ghp_saved_secret", page)
        self.assertIn("set (Setup)", page)
        # an empty box keeps the saved secret
        self.post("/setup/builds/settings", DEVIN_API_KEY="", GITHUB_TOKEN="")
        self.assertEqual(builds.stored_settings(self.db())["DEVIN_API_KEY"], "cog_saved_secret_value")
        self.post("/setup/builds/check")
        self.assertIn("refused the key", self.page("/setup/builds"))  # the fake accepts only its own key

    def test_cancel_stops_the_session(self):
        app_id, revision = self.ready_app()
        self.request_build(app_id, revision)
        self.run_worker()
        self.post("/builds/1/cancel")
        build = self.build()
        self.assertEqual((build["state"], build["active_app"]), ("cancelled", None))
        self.run_worker(jobs=3)
        self.assertTrue(self.devin.only_session()["terminated"])
        self.assertEqual(self.build()["state"], "cancelled")
        page = self.page("/builds/1")
        self.assertIn("Cancelled", page)
        self.assertIn("Retry", page)
        self.assertEqual(self.request_build(app_id, revision).location, "/builds/2")

    def test_cancel_while_checking_stops_a_session_the_create_made(self):
        self.devin.create_plan = ["fail_after_create"]
        app_id, revision = self.ready_app()
        self.request_build(app_id, revision)
        self.run_worker()
        self.assertEqual(self.build()["state"], "checking_started")
        self.post("/builds/1/cancel")
        self.run_worker(jobs=3)
        self.assertTrue(self.devin.only_session()["terminated"])
        self.assertEqual(self.devin.create_calls, 1)

    def test_a_later_build_sends_the_new_pack_previous_revision_and_change_summary(self):
        app_id, first = self.ready_app()
        self.request_build(app_id, first)
        self.run_worker()
        sid = self.devin.only_session()["session_id"]
        self.devin.finish(sid, pr=3)
        self.github.add_pull(REPO, 3, HEAD)
        self.run_worker()
        self.post(f"/apps/{app_id}/scenarios", name="Print the invoice.")
        second = self.revision(app_id)
        self.assertNotEqual(first, second)
        self.post(f"/apps/{app_id}/build/freeze", revision=second)
        self.request_build(app_id, second)
        self.assertIn("Say what changed since the previous build", self.page(f"/apps/{app_id}/build"))
        self.request_build(app_id, second, change_summary="Added the invoice scenario.")
        self.run_worker()
        later = self.build(2)
        self.assertEqual((later["attempt"], later["previous_revision"], later["revision"]), (2, first, second))
        prompt = [s for s in self.devin.sessions.values() if s["session_id"] != sid][0]["prompt"]
        self.assertIn(first, prompt)
        self.assertIn(second, prompt)
        self.assertIn("Added the invoice scenario.", prompt)
        self.assertIn("pull request #3", prompt)
        self.assertIn("changed evidence.json", prompt)

    def test_every_build_route_names_its_role(self):
        source = (PROJECT / "builds_web.py").read_text(encoding="utf-8")
        routes = re.findall(r"@app\.(?:get|post|route)\([^\n]*\n\s+def \w+\([^)]*\):([^\n]*)", source)
        self.assertGreater(len(routes), 10)
        for comment in routes:
            self.assertRegex(comment, r"# ROLE: (owner|builder)")


class JobQueueTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.database = new_database(self.work.name)
        self.db = builds.connect(self.database)
        self.addCleanup(self.db.close)
        jobqueue.migrate(self.db)

    def test_a_dead_workers_job_is_taken_over(self):
        job_id = jobqueue.enqueue(self.db, "build", {"build_id": 1})
        self.assertEqual(jobqueue.claim(self.db, "A", lease=0.05)["id"], job_id)
        self.assertIsNone(jobqueue.claim(self.db, "B", lease=5))  # A still holds it
        time.sleep(0.1)
        job = jobqueue.claim(self.db, "B", lease=5)
        self.assertEqual((job["id"], job["attempts"], job["lease_owner"]), (job_id, 2, "B"))
        self.assertFalse(jobqueue.finish(self.db, job_id, "A"))  # A came back too late
        self.assertFalse(jobqueue.heartbeat(self.db, job_id, "A"))
        self.assertTrue(jobqueue.heartbeat(self.db, job_id, "B"))
        self.assertTrue(jobqueue.finish(self.db, job_id, "B", {"ok": True}))
        self.assertEqual(jobqueue.get(self.db, job_id)["state"], "done")

    def test_a_job_that_keeps_killing_its_worker_is_given_up(self):
        job_id = jobqueue.enqueue(self.db, "build.cancel", {"build_id": 1})
        for _ in range(jobqueue.MAX_ATTEMPTS + 1):
            self.assertIsNotNone(jobqueue.claim(self.db, "A", lease=0))
            time.sleep(0.01)
        self.db.execute("UPDATE jobs SET lease_until = ? WHERE id = ?", (jobqueue.later(-1), job_id))
        self.db.commit()
        worker.run(self.database, once=True, ctx=builds.Context(), max_jobs=1)
        self.assertEqual(jobqueue.get(self.db, job_id)["state"], "failed")


if __name__ == "__main__":
    unittest.main()
