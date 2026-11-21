"""Release verification bound to the exact built commit, against a fake GitHub API (never the
real one) and a fake Twin Lab runner."""

from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from zipfile import ZipFile

from unittest import mock
from twinlab2 import independent
from twinlab2 import portal
from support import Client, freeze
from twinlab2.portal import create_app
from test_registry import eid
from twinlab2 import verification
from support import connect, new_database

REPO = "acme/billing"
SHA = "a" * 40
OTHER = "b" * 40


def zipped(files):
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return buffer.getvalue()


def run(name, conclusion="success", sha=SHA, status="completed", run_id=1):
    return {"id": run_id, "name": name, "head_sha": sha, "status": status,
            "conclusion": conclusion if status == "completed" else None,
            "html_url": f"https://github.example/{REPO}/runs/{run_id}",
            "completed_at": "2026-11-20T10:00:00Z" if status == "completed" else None}


class FakeGitHub:
    """The GitHub REST endpoints verification calls, on 127.0.0.1 and a free port."""

    def __init__(self):
        self.runs, self.statuses, self.status_sha = {}, {}, {}
        self.pr_heads, self.branch_heads, self.requests = {}, {}, []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                fake.requests.append((self.path, self.headers.get("Authorization")))
                path = self.path.split("?", 1)[0]
                parts = path.strip("/").split("/")
                if parts[:3] != ["repos", *REPO.split("/")]:
                    return self.reply(404, {"message": "Not Found"})
                rest = parts[3:]
                if len(rest) == 3 and rest[0] == "commits" and rest[2] == "check-runs":
                    return self.reply(200, {"total_count": len(fake.runs.get(rest[1], [])),
                                            "check_runs": fake.runs.get(rest[1], [])})
                if len(rest) == 3 and rest[0] == "commits" and rest[2] == "status":
                    return self.reply(200, {"sha": fake.status_sha.get(rest[1], rest[1]),
                                            "statuses": fake.statuses.get(rest[1], [])})
                if len(rest) == 2 and rest[0] == "commits" and rest[1] in fake.branch_heads:
                    return self.reply(200, {"sha": fake.branch_heads[rest[1]]})
                if len(rest) == 2 and rest[0] == "pulls" and rest[1] in fake.pr_heads:
                    return self.reply(200, {"number": int(rest[1]), "head": {"sha": fake.pr_heads[rest[1]]}})
                if len(rest) == 2 and rest[0] == "zipball":
                    body = zipped({f"billing-{rest[1][:7]}/run.py": 'print("ready")\n'})
                    self.send_response(200)
                    self.send_header("Content-Type", "application/zip")
                    self.end_headers()
                    return self.wfile.write(body)
                return self.reply(404, {"message": "Not Found"})

            def reply(self, status, body):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Deployment:
    """A built app that reports `commit` at /.twinlab/version."""

    def __init__(self, commit):
        self.commit = commit
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"commit": stub.commit}).encode())

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def page_reasons(test, release_id):
    """The reasons of the release's latest verification report, as one string."""
    rows = test.row("SELECT content FROM release_artifacts WHERE release_id = ? ORDER BY id DESC", release_id)
    return " ".join(json.loads(rows[0]["content"])["reasons"])


class FakeTwinLab:
    """Stands in for independent.TwinLabChecks: the replay and reset results it is told."""

    def __init__(self):
        self.replays = {}
        self.default = {"status": "pass", "verdict": "PASS WITHIN DECLARED SCOPE",
                        "cases": [{"id": "invoice-number", "check": "text", "required": True, "status": "PASS"}]}
        self.reset_result = {"result": "pass", "notes": "reset restored the baseline (3 table(s))"}
        self.calls = []

    def source(self):
        return {"commit": "430dfe538965b46ccbe824f8f2b4f415c5576483", "replay": "twinlab2.replay"}

    def replay(self, recording_id, base_url):
        self.calls.append(("replay", recording_id, base_url))
        return self.replays.get(recording_id, self.default)

    def reset(self, lab_url):
        self.calls.append(("reset", lab_url))
        return self.reset_result


class VerificationTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.github = FakeGitHub()
        self.addCleanup(self.github.close)
        self.deployment = Deployment(SHA)
        self.addCleanup(self.deployment.close)
        self.twin = FakeTwinLab()
        self.database = new_database(self.work.name)
        self.app = create_app(self.database)
        self.app.config.update(GITHUB=verification.GitHub(token="test-token", api=self.github.url),
                               TWINLAB_RUNNER=self.twin)
        self.client = Client(self.app)
        app_id = self.ok(self.client.post("/api/v1/apps", json={"name": "Billing"}), 201)["id"]
        self.assertEqual(app_id, 1)
        self.ok(self.client.patch("/api/v1/apps/1", json={"git_url": f"https://github.com/{REPO}.git"}))
        recording = self.ok(self.client.post("/api/v1/recordings", data={"name": "Invoice"}, files={"log": (
            "steps.jsonl", b'{"seq":1,"action":"submit","url":"http://billing.local/"}\n', "application/x-ndjson")}), 201)
        self.recording_id = recording["id"]
        self.ok(self.client.put(f"/api/v1/recordings/{self.recording_id}/assignments/1",
                                json={"events": [eid(self.recording_id, 1)], "notes": "Invoice is saved"}))
        self.ok(self.client.post("/api/v1/apps/1/scenarios", json={
            "name": "Invoice is saved", "expected": "The invoice has its number.",
            "events": [f"{self.recording_id}#{eid(self.recording_id, 1)}"]}), 201)
        self.assertEqual(freeze(self.client, 1).status_code, 200)
        self.settings()

    def ok(self, response, status=200):
        self.assertEqual(response.status_code, status, response.text[:800])
        return response.json() if "json" in response.headers.get("content-type", "") else response

    def post(self, path, **body):
        return self.ok(self.client.post(path, json=body or None))

    def settings(self, **changes):
        values = {"required_checks": "", "target_platform": "web", "windows_check_pattern": "",
                  "lab_url": "http://lab.test/", "launch": "python run.py", **changes}
        self.ok(self.client.put("/api/v1/apps/1/verification-settings", json=values))

    def green(self, *names, sha=SHA):
        self.github.runs[sha] = [run(name, run_id=n) for n, name in enumerate(names or ("build", "test"), 1)]

    def verify(self, sha=SHA, **body):
        if "pr_number" in body:
            body["pr_number"] = int(body["pr_number"])
        outcome = self.ok(self.client.post("/api/v1/apps/1/verify-commit", json={"repo": REPO, "sha": sha, **body}))
        return outcome["release_id"]

    def detail(self, release_id):
        return self.ok(self.client.get(f"/api/v1/releases/{release_id}"))

    def reasons(self, release_id):
        """Why the release is not Tested (the release's check summary), as one string."""
        return " ".join(self.detail(release_id)["checks"]["reasons"])

    def row(self, sql, *args):
        with closing(connect(self.database)) as db:
            return db.execute(sql, args).fetchall()

    def state(self, release_id):
        return self.row("SELECT state FROM releases WHERE id = ?", release_id)[0]["state"]

    def deploy(self, release_id, url=None):
        self.ok(self.client.put(f"/api/v1/releases/{release_id}/deployments",
                                json={"environment": "Demo", "url": url or self.deployment.url}))

    def test_green_checks_make_the_built_commit_tested(self):
        self.green("build", "test")
        self.github.statuses[SHA] = [{"context": "ci/lint", "state": "success", "target_url": "https://ci.example/1",
                                      "updated_at": "2026-11-20T09:00:00Z"}]
        release_id = self.verify()
        self.assertEqual(self.state(release_id), "packaged")  # no deployment of the commit yet
        self.deploy(release_id)
        self.post(f"/api/v1/releases/{release_id}/verify")
        self.assertEqual(self.state(release_id), "tested")
        release = self.row("SELECT commit_sha, version, repository FROM releases WHERE id = ?", release_id)[0]
        self.assertEqual((release["commit_sha"], release["version"], release["repository"]), (SHA, SHA[:12], REPO))
        checks = self.row("SELECT * FROM commit_checks WHERE commit_sha = ? ORDER BY name", SHA)
        self.assertEqual([(c["name"], c["kind"], c["conclusion"]) for c in checks],
                         [("build", "check_run", "success"), ("ci/lint", "status", "success"),
                          ("test", "check_run", "success")])
        self.assertEqual(checks[0]["url"], f"https://github.example/{REPO}/runs/1")
        self.assertEqual(checks[0]["completed_at"], "2026-11-20T10:00:00Z")
        test = self.row("SELECT * FROM release_tests WHERE release_id = ?", release_id)[0]
        self.assertEqual((test["commit_sha"], test["platform"], test["launch"], test["reset_result"]),
                         (SHA, "Web", "python run.py", "pass"))
        self.assertEqual([r["result"] for r in json.loads(test["results"])], ["pass"])
        self.assertIn(("replay", self.recording_id, self.deployment.url), self.twin.calls)
        self.assertIn(("reset", "http://lab.test/"), self.twin.calls)
        self.assertTrue(all(auth == "Bearer test-token" for _, auth in self.github.requests))
        checks = self.detail(release_id)["checks"]
        self.assertEqual(checks["reasons"], [])  # CI green
        self.assertIsNotNone(checks["checked_at"])
        self.assertEqual(checks["required"], [{"pattern": "*", "state": "success"}])  # every check reported

    def test_report_artifact_is_stored(self):
        self.green()
        release_id = self.verify()
        self.deploy(release_id)
        self.post(f"/api/v1/releases/{release_id}/verify")
        reports = self.row("SELECT id, kind, sha256 FROM release_artifacts WHERE release_id = ? ORDER BY id", release_id)
        self.assertEqual([r["kind"] for r in reports], ["verification_report"] * 2)
        response = self.client.get(f"/api/v1/releases/{release_id}/reports/{reports[-1]['id']}")
        self.assertEqual(response.status_code, 200)
        report = json.loads(response.content)
        import hashlib
        self.assertEqual(hashlib.sha256(response.content).hexdigest(), reports[-1]["sha256"])
        self.assertEqual((report["commit"], report["repository"], report["tested"]), (SHA, REPO, True))
        self.assertEqual({c["name"] for c in report["github"]["checks"]}, {"build", "test"})
        self.assertEqual(report["twinlab"]["scenarios"][0]["outcome"], "MET")
        self.assertEqual(report["twinlab"]["reset"]["result"], "pass")
        self.assertEqual(report["twinlab"]["twinlab"]["commit"], "430dfe538965b46ccbe824f8f2b4f415c5576483")
        first = json.loads(self.client.get(f"/api/v1/releases/{release_id}/reports/{reports[0]['id']}").content)
        self.assertFalse(first["tested"])
        self.assertTrue(any("No deployment reports" in reason for reason in first["reasons"]))
        self.assertEqual(self.client.get(f"/api/v1/releases/{release_id + 1}/reports/{reports[0]['id']}").status_code, 404)

    def verified_with(self, runs, statuses=None, **settings):
        if settings:
            self.settings(**settings)
        self.github.runs[SHA] = runs
        self.github.statuses[SHA] = statuses or []
        release_id = self.verify()
        self.deploy(release_id)
        self.post(f"/api/v1/releases/{release_id}/verify")
        return release_id, self.reasons(release_id)

    def test_a_red_check_is_refused(self):
        release_id, page = self.verified_with([run("build"), run("test", "failure", run_id=2)])
        self.assertEqual(self.state(release_id), "packaged")
        self.assertIn("Required check “test” is failure.", page)

    def test_a_failed_commit_status_is_refused(self):
        release_id, page = self.verified_with([run("build")], [{"context": "ci/deploy", "state": "error"}])
        self.assertEqual(self.state(release_id), "packaged")
        self.assertIn("Required check “ci/deploy” is failure.", page)

    def test_a_pending_check_is_refused(self):
        release_id, page = self.verified_with([run("build"), run("test", status="in_progress", run_id=2)])
        self.assertEqual(self.state(release_id), "packaged")
        self.assertIn("Required check “test” is pending.", page)

    def test_a_missing_required_check_is_refused(self):
        release_id, page = self.verified_with([run("build"), run("test", run_id=2)], required_checks="build\ne2e*")
        self.assertEqual(self.state(release_id), "packaged")
        self.assertIn("Required check “e2e*” is missing on this commit.", page)
        # Checks that are not required do not block: a red lint passes when only build is required.
        self.settings(required_checks="build")
        self.github.runs[SHA].append(run("lint", "failure", run_id=3))
        self.post(f"/api/v1/releases/{release_id}/verify")
        self.assertEqual(self.state(release_id), "tested")

    def test_no_checks_at_all_is_refused(self):
        release_id, page = self.verified_with([])
        self.assertEqual(self.state(release_id), "packaged")
        self.assertIn("No checks are reported on this commit.", page)

    def test_a_rerun_decides(self):
        release_id, _ = self.verified_with([run("test", "failure", run_id=1), run("test", run_id=2)])
        self.assertEqual(self.state(release_id), "tested")

    def test_checks_of_another_sha_never_count(self):
        # GitHub answering with results for another commit (or a combined status of another SHA).
        self.github.status_sha[SHA] = OTHER
        release_id, page = self.verified_with([run("build", sha=OTHER), run("test", sha=OTHER, run_id=2)],
                                              [{"context": "ci/lint", "state": "success"}])
        self.assertEqual(self.state(release_id), "packaged")
        self.assertEqual(self.row("SELECT * FROM commit_checks WHERE commit_sha = ?", SHA), [])
        self.assertIn("No checks are reported on this commit.", page)

    def test_green_checks_of_another_commit_do_not_test_this_one(self):
        self.green(sha=OTHER)
        self.github.runs[SHA] = [run("build", "failure")]
        release_id = self.verify()
        self.deploy(release_id)
        self.post(f"/api/v1/releases/{release_id}/verify")
        self.assertEqual(self.state(release_id), "packaged")

    def test_a_deployment_of_another_commit_is_not_verified_by_twin_lab(self):
        self.green()
        release_id = self.verify()
        other = Deployment(OTHER)
        self.addCleanup(other.close)
        self.deploy(release_id, other.url)
        with closing(connect(self.database)) as db, db:
            outcome = portal.verify_release(db, release_id, self.app.config["GITHUB"], self.twin,
                                            base_url=other.url)
        self.assertFalse(outcome["tested"])
        self.assertIn(f"reports commit {OTHER[:12]}, not {SHA[:12]}", " ".join(outcome["reasons"]))
        self.assertEqual([c for c in self.twin.calls if c[0] == "replay"], [])

    def test_manual_test_results_need_green_checks_and_the_packaged_sha(self):
        self.github.runs[SHA] = [run("build", "failure")]
        release_id = self.verify()
        form = {"commit": SHA, "results": {"scenario-1": "pass"}, "platform": "Web",
                "launch": "python run.py", "reset_result": "pass"}
        refused = self.client.post(f"/api/v1/releases/{release_id}/tests", json=form)
        self.assertEqual(refused.status_code, 400)
        self.assertIn("Required check “build” is failure.", refused.json()["detail"])
        self.green()
        wrong = self.client.post(f"/api/v1/releases/{release_id}/tests", json=dict(form, commit=OTHER))
        self.assertEqual(wrong.status_code, 400)
        self.assertIn("must be the packaged commit", wrong.json()["detail"])
        self.ok(self.client.post(f"/api/v1/releases/{release_id}/tests", json=form))
        self.assertEqual(self.state(release_id), "tested")

    def test_a_moved_pull_request_head_invalidates_the_verification(self):
        self.green()
        self.github.pr_heads["7"] = SHA
        release_id = self.verify(pr_number="7")
        self.deploy(release_id)
        self.post(f"/api/v1/releases/{release_id}/verify")
        self.assertEqual(self.state(release_id), "tested")
        self.github.pr_heads["7"] = OTHER  # a new push, or a force-push, to the pull request
        self.post(f"/api/v1/releases/{release_id}/checks/refresh")
        self.assertEqual(self.state(release_id), "packaged")
        self.assertIn(f"Pull request #7 now points at {OTHER[:12]}", self.reasons(release_id))
        self.post(f"/api/v1/releases/{release_id}/verify")
        self.assertEqual(self.state(release_id), "packaged")
        # The new head is a release of its own.
        self.green(sha=OTHER)
        self.assertNotEqual(self.verify(sha=OTHER, pr_number="7"), release_id)

    def test_a_moved_branch_head_is_refused(self):
        self.green()
        self.github.branch_heads["main"] = OTHER
        release_id = self.verify(ref="main")
        self.deploy(release_id)
        self.post(f"/api/v1/releases/{release_id}/verify")
        self.assertEqual(self.state(release_id), "packaged")
        self.assertIn("Branch main now points at", self.reasons(release_id))

    def test_windows_desktop_needs_a_windows_validation_check(self):
        release_id, page = self.verified_with([run("build (ubuntu-latest)")], target_platform="windows-desktop")
        self.assertEqual(self.state(release_id), "packaged")
        self.assertIn("Windows validation missing", page)
        self.github.runs[SHA].append(run("build (windows-latest)", "failure", run_id=2))
        self.post(f"/api/v1/releases/{release_id}/verify")
        self.assertIn("Windows validation is not green", self.reasons(release_id))
        self.github.runs[SHA][1] = run("build (windows-latest)", run_id=2)
        self.post(f"/api/v1/releases/{release_id}/verify")
        self.assertEqual(self.state(release_id), "tested")
        test = self.row("SELECT platform FROM release_tests WHERE release_id = ?", release_id)[0]
        self.assertEqual(test["platform"], "Windows desktop")

    def test_windows_check_pattern_is_configurable(self):
        release_id, page = self.verified_with([run("build"), run("desktop-validation", run_id=2)],
                                              target_platform="windows-desktop", windows_check_pattern="desktop-*")
        self.assertEqual(self.state(release_id), "tested")

    def test_failed_twin_lab_checks_keep_the_release_packaged(self):
        self.twin.default = {"status": "pass", "cases": [{"id": "n", "required": True, "status": "FAIL"}]}
        release_id, _ = self.verified_with([run("build")])
        test = self.row("SELECT results FROM release_tests WHERE release_id = ?", release_id)[0]
        # The results are recorded (a fail is evidence), and nothing hides the failed outcome.
        self.assertEqual(json.loads(test["results"])[0]["result"], "fail")
        # ... and the release is not Tested: all of it must hold (README, Verify this build)
        self.assertEqual(self.state(release_id), "packaged")
        self.assertIn("Not Tested: Scenario", page_reasons(self, release_id))
        self.twin.reset_result = {"result": None, "notes": "no lab"}
        self.settings(lab_url="")
        release_id, page = self.verified_with([run("build")])
        reports = self.row("SELECT content FROM release_artifacts WHERE release_id = ? ORDER BY id DESC", release_id)
        report = json.loads(reports[0]["content"])
        self.assertIn("Record whether the reset check passed or failed.", " ".join(report["reasons"]))

    def test_a_failed_reset_or_a_scenario_not_run_keeps_the_release_packaged(self):
        self.twin.reset_result = {"result": "fail", "notes": "after reset these tables differ from the baseline: invoices"}
        release_id, _ = self.verified_with([run("build")])
        self.assertEqual(self.state(release_id), "packaged")
        self.assertIn("The reset check failed", page_reasons(self, release_id))
        self.twin.reset_result = {"result": "pass", "notes": "reset restored the baseline (3 table(s))"}
        self.twin.default = {"status": "pass", "cases": []}  # replayed, but nothing judged the outcome
        self.post(f"/api/v1/releases/{release_id}/verify")
        self.assertEqual(self.state(release_id), "packaged")
        self.assertIn("not-run", page_reasons(self, release_id))
        # a later verification where all of it holds makes the same release Tested
        self.twin.default = FakeTwinLab().default
        self.post(f"/api/v1/releases/{release_id}/verify")
        self.assertEqual(self.state(release_id), "tested")
        # and one that fails again takes it back to Packaged (the latest evidence decides)
        self.twin.reset_result = {"result": "fail", "notes": "differs"}
        self.post(f"/api/v1/releases/{release_id}/verify")
        self.assertEqual(self.state(release_id), "packaged")

    def test_a_repository_without_github_has_no_ci_gate(self):
        self.ok(self.client.patch("/api/v1/apps/1", json={"git_url": ""}))
        setup = self.ok(self.client.get("/api/v1/apps/1/setup"))
        self.assertFalse(setup["github_repo"])  # no GitHub repository: no CI checks are read
        self.assertEqual(setup["app"]["target_platform"], "web")  # the release verification settings stay

    def test_verify_commit_needs_a_full_sha_and_a_repository(self):
        response = self.client.post("/api/v1/apps/1/verify-commit", json={"repo": REPO, "sha": "abc"})
        self.assertEqual(response.status_code, 400)
        response = self.client.post("/api/v1/apps/1/verify-commit", json={"repo": "x", "sha": SHA})
        self.assertEqual(response.status_code, 400)


class IndependentChecksTest(unittest.TestCase):
    manifest = {"scenarios": [{"name": "Saved", "events": [{"recording_id": "r1", "seq": 1}]},
                              {"name": "Unseen", "events": []}]}
    scenarios = [{"id": "scenario-1", "name": "Saved"}, {"id": "scenario-2", "name": "Unseen"}]

    def test_results(self):
        twin = FakeTwinLab()
        report = independent.run(twin, self.scenarios, self.manifest, "http://app.test/", "")
        self.assertEqual([s["result"] for s in report["scenarios"]], ["pass", "not-run"])
        self.assertIsNone(report["reset"]["result"])
        twin.default = {"status": "pass", "cases": []}
        report = independent.run(twin, self.scenarios, self.manifest, "http://app.test/", "http://lab.test/")
        self.assertEqual(report["scenarios"][0]["result"], "not-run")  # nothing judged the outcome
        self.assertEqual(report["scenarios"][0]["outcome"], "CANNOT_CHECK")
        twin.default = {"status": "blocked", "cases": [{"id": "x", "status": "PASS"}]}
        report = independent.run(twin, self.scenarios, self.manifest, "http://app.test/", "http://lab.test/")
        self.assertEqual(report["scenarios"][0]["result"], "fail")
        self.assertEqual(report["reset"]["result"], "pass")

    def test_a_recording_shared_by_scenarios_is_replayed_once(self):
        # the second replay would start from the data the first one saved, not from the seed
        manifest = {"scenarios": [{"name": "Invoice", "events": [{"recording_id": "r1", "seq": 1}]},
                                  {"name": "Approval", "events": [{"recording_id": "r1", "seq": 2}]}]}
        scenarios = [{"id": "scenario-1", "name": "Invoice"}, {"id": "scenario-2", "name": "Approval"}]
        twin = FakeTwinLab()
        report = independent.run(twin, scenarios, manifest, "http://app.test/", "http://lab.test/")
        self.assertEqual([c for c in twin.calls if c[0] == "replay"], [("replay", "r1", "http://app.test/")])
        self.assertEqual([s["result"] for s in report["scenarios"]], ["pass", "pass"])

    def test_no_scenario_is_checked_without_the_replay(self):
        class NoPlaywright(FakeTwinLab):
            def source(self):
                raise independent.ReplayUnavailable("the replay needs Playwright")
        report = independent.run(NoPlaywright(), self.scenarios, self.manifest, "http://app.test/", "")
        self.assertEqual(report["scenarios"], [])
        self.assertIn("needs Playwright", report["problems"][0])

    def test_a_replay_that_reaches_a_cut_path_is_not_run_never_passed(self):
        twin = FakeTwinLab()
        twin.default = {"status": "not-run", "verdict": None, "cases": [], "problems": [
            "Twin Lab's conformance is not part of TwinLab2 (cut from the copy of Twin Lab 90bdd91)"]}
        report = independent.run(twin, self.scenarios, self.manifest, "http://app.test/", "")
        scenario = report["scenarios"][0]
        self.assertEqual(scenario["result"], "not-run")
        self.assertIn("could not finish here", scenario["why"])
        self.assertIn("conformance", scenario["why"])

    def test_cut_paths_raise_not_absorbed(self):
        from twinlab2.replay import durable, fidelity
        from twinlab2_contract.upstream import NotAbsorbed
        for call in (lambda: fidelity.save(Path("."), "app", None), lambda: durable.assess(Path("."), "app", None)):
            with self.assertRaises(NotAbsorbed):
                call()

    def test_replay_entry_point_reports_a_cut_path_as_not_run(self):
        from twinlab2.replay import __main__ as entry, fidelity
        from twinlab2_contract.upstream import NotAbsorbed
        with tempfile.TemporaryDirectory() as work:
            out = Path(work) / "report.json"
            with mock.patch.object(fidelity, "replay", side_effect=NotAbsorbed("Twin Lab's har")):
                code = entry.main([work, "--base-url", "http://app.test/", "--json", str(out)])
            self.assertEqual(code, 1)
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "not-run")
            self.assertIn("Twin Lab's har", report["problems"][0])

    def test_runner_finds_recording_folders(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "r1" / "web").mkdir(parents=True)
            (Path(root) / "r1" / "web" / "steps.jsonl").write_text("{}\n", encoding="utf-8")
            runner = independent.TwinLabChecks(recordings=root)
            self.assertEqual(runner.recording("r1"), Path(root) / "r1" / "web")
            self.assertIsNone(runner.recording("r2"))
            self.assertIsNone(runner.replay("r2", "http://app.test/"))


class GitHubRepoTest(unittest.TestCase):
    def test_parsing(self):
        for url in ("https://github.com/acme/billing", "https://github.com/acme/billing.git",
                    "git@github.com:acme/billing.git", "acme/billing"):
            self.assertEqual(verification.github_repo(url), "acme/billing", url)
        for url in ("https://git.example.test/orders.git", "", "https://github.com/acme"):
            self.assertEqual(verification.github_repo(url), "", url)


if __name__ == "__main__":
    unittest.main()
