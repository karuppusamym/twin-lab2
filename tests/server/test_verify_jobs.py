"""Verify as a job (verifications.py): 202 and a verification resource, worked by the worker; its
lifecycle, a worker that stops mid-verify, duplicate requests, and a verification that cannot run."""

from contextlib import closing
import json
import time
import unittest

from twinlab2 import builds, jobqueue, verifications, worker
import test_verification as tv

V1 = "/api/v1"


class Base(tv.VerificationWorld):
    """The release-verification world of test_verification, with the in-process worker off:
    each test works the queue itself (as `python -m twinlab2.worker` does)."""

    def setUp(self):
        super().setUp()
        self.app.config.pop("TWINLAB_RUNNER")  # nothing runs in-process: a worker takes the jobs

    def ctx(self, runner=None):
        ctx = builds.Context()
        ctx.verify_github, ctx.verify_runner = self.app.config["GITHUB"], runner or self.twin
        return ctx

    def run_worker(self, runner=None, lease=5.0):
        return worker.run(self.database, once=True, ctx=self.ctx(runner), types=(verifications.JOB,), lease=lease)

    def get(self, verification_id):
        return self.ok(self.client.get(f"{V1}/verifications/{verification_id}"))

    def a_release(self):
        """A Packaged release of SHA with a deployment reporting it (verified once, by a worker)."""
        self.green()
        queued = self.ok(self.client.post(f"{V1}/apps/1/verify-commit", json={"repo": tv.REPO, "sha": tv.SHA}), 202)
        self.run_worker()
        release_id = self.get(queued["id"])["release_id"]
        self.deploy(release_id)
        return release_id

    def jobs(self):
        return self.row("SELECT id, type, state, attempts FROM jobs WHERE type = 'verify' ORDER BY created_at")


class LifecycleTest(Base):
    def test_verify_answers_202_and_the_worker_makes_the_release_tested(self):
        release_id = self.a_release()
        queued = self.ok(self.client.post(f"{V1}/releases/{release_id}/verify"), 202)
        self.assertEqual((queued["state"], queued["release_id"], queued["kind"]), ("queued", release_id, "release"))
        self.assertIsNone(queued["report"])
        self.assertEqual(self.state(release_id), "packaged")  # nothing ran inside the request
        self.assertEqual(self.detail(release_id)["verification"]["id"], queued["id"])
        self.assertEqual(self.run_worker(), 1)
        done = self.get(queued["id"])
        self.assertEqual(done["state"], "passed", done)
        self.assertTrue(done["tested"])
        self.assertEqual(self.state(release_id), "tested")
        self.assertEqual(done["progress"]["stage"], "passed")
        self.assertEqual([(s["name"], s["state"], s["result"]) for s in done["progress"]["scenarios"]],
                         [("Invoice is saved", "pass", "pass")])
        self.assertEqual((done["progress"]["total"], done["progress"]["done"]), (1, 1))
        self.assertEqual(set(done["dimensions"]), {"behavioural", "structural", "visual", "deployment"})
        self.assertEqual(done["dimensions"]["deployment"]["status"], "pass")
        self.assertEqual(done["scope"]["scenarios_passed"], 1)
        report = self.client.get(done["report"]["url"])
        self.assertEqual(report.status_code, 200)
        self.assertEqual(json.loads(report.content)["dimensions"]["behavioural"]["status"], "pass")
        actions = [r["action"] for r in self.row("SELECT action FROM audit_log ORDER BY id")]
        self.assertIn("release.verify", actions)
        self.assertIn("release.verified", actions)

    def test_a_failed_check_ends_failed_and_the_release_stays_packaged(self):
        release_id = self.a_release()
        self.twin.reset_result = {"result": "fail", "notes": "invoice table differs"}
        queued = self.ok(self.client.post(f"{V1}/releases/{release_id}/verify"), 202)
        self.run_worker()
        done = self.get(queued["id"])
        self.assertEqual((done["state"], done["tested"]), ("failed", False))
        self.assertIn("The reset check failed: invoice table differs.", " ".join(done["reasons"]))
        self.assertEqual(done["dimensions"]["deployment"]["status"], "fail")
        self.assertEqual(self.state(release_id), "packaged")

    def test_verify_commit_packages_the_commit_in_the_worker(self):
        self.green()
        queued = self.ok(self.client.post(f"{V1}/apps/1/verify-commit", json={"repo": tv.REPO, "sha": tv.SHA}), 202)
        self.assertEqual((queued["kind"], queued["release_id"], queued["commit_sha"]), ("commit", None, tv.SHA))
        self.assertEqual(self.row("SELECT COUNT(*) AS n FROM releases")[0]["n"], 0)
        self.run_worker()
        done = self.get(queued["id"])
        self.assertIsNotNone(done["release_id"])
        self.assertEqual(done["state"], "failed")  # no deployment yet: packaged, not Tested
        self.assertIn("No deployment reports", " ".join(done["reasons"]))

    def test_a_commit_that_cannot_be_packaged_ends_in_error(self):
        # the fake GitHub knows acme/billing only: the archive of another repository is a 404
        queued = self.ok(self.client.post(f"{V1}/apps/1/verify-commit", json={"repo": "acme/missing", "sha": tv.OTHER}), 202)
        self.run_worker()
        done = self.get(queued["id"])
        self.assertEqual(done["state"], "error", done)
        self.assertIn("Could not download commit", done["error"])
        self.assertIsNone(done["release_id"])

    def test_the_request_is_checked_before_anything_is_queued(self):
        self.assertEqual(self.client.post(f"{V1}/apps/1/verify-commit", json={"repo": tv.REPO, "sha": "abc"}).status_code, 400)
        self.assertEqual(self.jobs(), [])
        bad = self.client.post(f"{V1}/releases/1/verify", json={"targets": {"desktop": "C:/Windows/notepad.exe"}})
        self.assertEqual(bad.status_code, 404)  # no release 1 yet
        release_id = self.a_release()
        bad = self.client.post(f"{V1}/releases/{release_id}/verify", json={"targets": {"desktop": "../x.exe"}})
        self.assertEqual(bad.status_code, 400)
        bad = self.client.post(f"{V1}/releases/{release_id}/verify", json={"targets": {"mainframe": "not a host"}})
        self.assertEqual(bad.status_code, 400)

    def test_a_viewer_reads_a_verification(self):
        release_id = self.a_release()
        queued = self.ok(self.client.post(f"{V1}/releases/{release_id}/verify"), 202)
        self.assertEqual(self.client.get(f"{V1}/verifications/999").status_code, 404)
        self.assertEqual(self.get(queued["id"])["id"], queued["id"])


class DuplicateTest(Base):
    def test_the_same_request_key_is_one_verification(self):
        release_id = self.a_release()
        first = self.ok(self.client.post(f"{V1}/releases/{release_id}/verify", json={"request_key": "k1"}), 202)
        again = self.ok(self.client.post(f"{V1}/releases/{release_id}/verify", json={"request_key": "k1"}), 202)
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(len(self.jobs()), 2)  # the verify-commit's, and this one
        self.run_worker()
        after = self.ok(self.client.post(f"{V1}/releases/{release_id}/verify", json={"request_key": "k1"}), 202)
        self.assertEqual((after["id"], after["state"]), (first["id"], "passed"))  # not run again
        self.assertEqual(len([c for c in self.twin.calls if c[0] == "replay"]), 1)

    def test_the_idempotency_key_header_is_the_request_key(self):
        release_id = self.a_release()
        headers = {"Idempotency-Key": "verify-once"}
        first = self.ok(self.client.post(f"{V1}/releases/{release_id}/verify", headers=headers), 202)
        self.run_worker()
        again = self.client.post(f"{V1}/releases/{release_id}/verify", headers=headers)
        self.assertEqual((again.status_code, again.json()["id"], again.json()["state"]), (202, first["id"], "passed"))
        self.assertEqual(again.headers.get("Idempotent-Replayed"), "true")
        clash = self.client.post(f"{V1}/releases/{release_id}/verify", headers=headers, json={"request_key": "other"})
        self.assertEqual(clash.status_code, 400)

    def test_a_request_while_one_is_queued_gets_that_one(self):
        release_id = self.a_release()
        first = self.ok(self.client.post(f"{V1}/releases/{release_id}/verify"), 202)
        second = self.ok(self.client.post(f"{V1}/releases/{release_id}/verify"), 202)
        self.assertEqual(second["id"], first["id"])
        self.run_worker()
        third = self.ok(self.client.post(f"{V1}/releases/{release_id}/verify"), 202)
        self.assertNotEqual(third["id"], first["id"])  # once it ended, Verify runs again
        self.assertEqual(third["state"], "queued")

    def test_two_commit_requests_for_one_commit_are_one_verification(self):
        self.green()
        body = {"repo": tv.REPO, "sha": tv.SHA}
        first = self.ok(self.client.post(f"{V1}/apps/1/verify-commit", json=body), 202)
        second = self.ok(self.client.post(f"{V1}/apps/1/verify-commit", json=body), 202)
        self.assertEqual(first["id"], second["id"])


class Dies(Exception):
    """Stands in for the worker process dying mid-replay (not an error the job handles)."""


class StoppingTwin(tv.FakeTwinLab):
    def __init__(self, stops=1):
        super().__init__()
        self.stops = stops

    def replay(self, recording_id, base_url):
        if self.stops:
            self.stops -= 1
            raise Dies("the worker process was killed")
        return super().replay(recording_id, base_url)


class RestartTest(Base):
    def test_a_worker_that_stops_mid_verify_is_taken_over(self):
        release_id = self.a_release()
        queued = self.ok(self.client.post(f"{V1}/releases/{release_id}/verify"), 202)
        with closing(tv.connect(self.database)) as db:
            job = jobqueue.claim(db, "dead-worker", lease=0.2, types=(verifications.JOB,))
            with self.assertRaises(Dies):
                verifications.handle(db, job, self.ctx(StoppingTwin()))
        running = self.get(queued["id"])
        self.assertEqual(running["state"], "running")
        self.assertEqual(running["progress"]["scenarios"][0]["state"], "running")
        self.assertEqual(self.state(release_id), "packaged")
        time.sleep(0.3)  # the dead worker's lease runs out
        self.assertEqual(self.run_worker(), 1)
        done = self.get(queued["id"])
        self.assertEqual((done["state"], done["attempts"]), ("passed", 2), done)
        self.assertEqual(self.state(release_id), "tested")
        self.assertEqual(self.jobs()[-1]["state"], "done")

    def test_a_verification_whose_worker_keeps_stopping_ends_in_error(self):
        release_id = self.a_release()
        queued = self.ok(self.client.post(f"{V1}/releases/{release_id}/verify"), 202)
        job_id = self.row("SELECT job_id FROM verifications WHERE id = ?", queued["id"])[0]["job_id"]
        with closing(tv.connect(self.database)) as db:
            db.execute("UPDATE jobs SET state = 'running', attempts = ?, lease_owner = 'dead', lease_until = ? "
                       "WHERE id = ?", (jobqueue.MAX_ATTEMPTS, jobqueue.stamp(), job_id))
            db.commit()
        time.sleep(0.01)
        self.run_worker()
        done = self.get(queued["id"])
        self.assertEqual(done["state"], "error")
        self.assertIn("The worker stopped", done["error"])
        self.assertEqual(self.state(release_id), "packaged")


class InProcessTest(tv.VerificationWorld):
    """The demo portal and tests (a fake TWINLAB_RUNNER) work verifications in a thread."""

    def test_in_process(self):
        self.green()
        done = self.wait(self.ok(self.client.post(f"{V1}/apps/1/verify-commit",
                                                  json={"repo": tv.REPO, "sha": tv.SHA}), 202))
        self.deploy(done["release_id"])
        done = self.wait(self.ok(self.client.post(f"{V1}/releases/{done['release_id']}/verify"), 202))
        self.assertEqual(done["state"], "passed")
        self.assertEqual(self.state(done["release_id"]), "tested")


if __name__ == "__main__":
    unittest.main()
