"""Verify's acceptance checks are judged and shown one by one, and a build session's finishing is
never acceptance: its release becomes Tested only through Verify."""

import json
import unittest

from twinlab2 import acceptance_checks
from test_verification import SHA, VerificationWorld

KEYS = ["ci", "commit_identity", "scenarios", "fixture_reset", "persistence", "failure_scenarios", "visual_coverage"]


class AcceptanceChecksTest(VerificationWorld):
    def tested_release(self):
        self.green("build", "test")
        release_id = self.verify()
        self.deploy(release_id)
        return release_id

    def checks_of(self, release_id):
        row = self.row("SELECT checks FROM verifications WHERE release_id = ? ORDER BY id DESC", release_id)[0]
        return {c["id"]: c for c in json.loads(row["checks"])}

    def test_each_check_is_reported_on_its_own(self):
        self.twin.persistence = lambda lab_url, base_url: {"result": "pass", "notes": "same data after a restart"}
        release_id = self.tested_release()
        self.post(f"/api/v1/releases/{release_id}/verify")
        self.assertEqual(self.state(release_id), "tested")
        checks = self.checks_of(release_id)
        self.assertEqual(list(checks), KEYS)
        self.assertEqual(checks["ci"]["status"], "pass")
        self.assertEqual(checks["commit_identity"]["status"], "pass")
        self.assertIn(SHA, checks["commit_identity"]["detail"])  # the full commit, not a prefix
        self.assertEqual(checks["fixture_reset"]["status"], "pass")
        self.assertEqual(checks["persistence"]["status"], "pass")
        self.assertEqual(checks["failure_scenarios"]["status"], "not-checked")
        latest = self.ok(self.client.get(f"/api/v1/releases/{release_id}"))
        self.assertIn("release", json.dumps(latest).lower())

    def test_data_lost_across_a_restart_keeps_the_release_from_tested(self):
        self.twin.persistence = lambda lab_url, base_url: {"result": "fail", "notes": "after a restart: invoices differ"}
        release_id = self.tested_release()
        self.post(f"/api/v1/releases/{release_id}/verify")
        self.assertEqual(self.state(release_id), "packaged")
        self.assertEqual(self.checks_of(release_id)["persistence"]["status"], "fail")
        self.assertIn("Data persists across a restart", self.page_reasons(release_id))

    def test_a_verifier_that_cannot_restart_says_not_checked(self):
        release_id = self.tested_release()  # the fake has no persistence(): nothing to restart with
        self.post(f"/api/v1/releases/{release_id}/verify")
        self.assertEqual(self.state(release_id), "tested")
        self.assertEqual(self.checks_of(release_id)["persistence"]["status"], "not-checked")

    def test_a_build_sessions_release_is_tested_only_through_verify(self):
        class Row(dict):
            pass

        class Connection:  # the release row of a build session's commit
            def execute(self, sql, params=()):
                return self

            def fetchone(self):
                return Row(id=5, state="packaged", build_id=3, commit_sha=SHA, manifest="{}", app_id=1)

        from twinlab2 import portal

        with self.assertRaisesRegex(ValueError, "only through Verify"):
            portal.record_test(Connection(), 5, SHA, "Web", "python run.py", "pass", "", {}, "owner")

    def page_reasons(self, release_id):
        rows = self.row("SELECT reasons FROM verifications WHERE release_id = ? ORDER BY id DESC", release_id)
        return " ".join(json.loads(rows[0]["reasons"]))



class FailureScenarioTest(unittest.TestCase):
    def check(self, cases):
        manifest = {"scenarios": [{"id": "s1", "name": "Invoice", "failure_cases": ["A zero amount is refused."]}]}
        twin = {"scenarios": [{"id": "s1", "name": "Invoice", "result": "pass",
                               "replays": [{"cases": cases}]}], "reset": {"result": "pass"}}
        checks = acceptance_checks.build(gate={"ok": True}, sha="a" * 40, deployment={"url": "u", "reported_commit": "a" * 40},
                                         session_commit=None, twin=twin, manifest=manifest, dims={})
        return {c["id"]: c for c in checks}["failure_scenarios"]

    def test_a_failure_case_covered_by_a_passing_case_passes(self):
        self.assertEqual(self.check([{"id": "failure-zero-amount", "status": "PASS"}])["status"], "pass")

    def test_a_failure_case_that_did_not_hold_fails(self):
        self.assertEqual(self.check([{"id": "failure-zero-amount", "status": "FAIL"}])["status"], "fail")

    def test_an_uncovered_failure_case_is_not_checked(self):
        found = self.check([{"id": "invoice-number", "status": "PASS"}])
        self.assertEqual(found["status"], "not-checked")
        self.assertIn("0 of 1", found["detail"])

    def test_a_session_commit_that_differs_from_the_release_fails_identity(self):
        checks = acceptance_checks.build(gate={"ok": True}, sha="a" * 40, deployment={"url": "u", "reported_commit": "a" * 40},
                                         session_commit="b" * 40, twin=None, manifest={}, dims={})
        self.assertEqual({c["id"]: c for c in checks}["commit_identity"]["status"], "fail")


if __name__ == "__main__":
    unittest.main()
