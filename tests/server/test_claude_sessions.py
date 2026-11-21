"""Claude and Copilot build sessions end to end, with the real runner process and git, and fakes
for everything outside this PC: a fake agent (tests/fake_agents.py) instead of the Agent / Copilot
SDK, a fake Messages API, a fake GitHub API and a local bare repository as the remote. No call
reaches Anthropic, GitHub or Copilot and nothing is spent.
"""

from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from twinlab2 import builds
from fakes import FakeAnthropic, FakeGitHub
from twinlab2 import packages
from twinlab2.portal import create_app
from twinlab2.providers import local_runner
from support import freeze as freeze_package, new_database
from twinlab2 import worker

REPO = "acme/orders-demo"
TESTS = Path(__file__).resolve().parent


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class ClaudeSessionTest(unittest.TestCase):
    provider = "claude"

    def setUp(self):
        self.work = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.work.cleanup)
        root = Path(self.work.name)
        # the remote: a bare repository with one commit on main
        remote = root / "remotes" / "acme" / "orders-demo.git"
        remote.mkdir(parents=True)
        git("init", "--bare", "--quiet", "-b", "main", cwd=remote)
        seed = root / "seed"
        seed.mkdir()
        git("init", "--quiet", "-b", "main", cwd=seed)
        (seed / "README.md").write_text("Orders demo\n", encoding="utf-8")
        git("add", "README.md", cwd=seed)
        git("-c", "user.name=t", "-c", "user.email=t@t.invalid", "commit", "--quiet", "-m", "seed", cwd=seed)
        git("push", "--quiet", str(remote), "main", cwd=seed)
        self.remote, self.base = remote, git("rev-parse", "HEAD", cwd=seed)

        self.github = FakeGitHub().start()
        self.addCleanup(self.github.stop)
        self.github.add_repo(REPO, sha=self.base)
        self.anthropic = FakeAnthropic().start()
        self.addCleanup(self.anthropic.stop)
        self.database = new_database(self.work.name)
        self.plan = {}
        self.env = {
            "TWINLAB2_GITHUB_TOKEN": self.github.token, "TWINLAB2_GITHUB_API": self.github.url,
            "TWINLAB2_WORKSPACES": str(root / "workspaces"),
            "TWINLAB2_GIT_REMOTE": str(root / "remotes") + "/{repo}.git",
            "ANTHROPIC_API_KEY": self.anthropic.key, "ANTHROPIC_BASE_URL": self.anthropic.url,
            "TWINLAB2_CLAUDE_AGENT": "fake_agents:claude", "TWINLAB2_COPILOT_AGENT": "fake_agents:copilot",
            "TWINLAB2_ENABLE_COPILOT": "1", "COPILOT_GITHUB_TOKEN": "ghu_fake_copilot",
            "TWINLAB2_CLAUDE_TIMEOUT": "3", "PYTHONPATH": str(TESTS), "TWINLAB2_FAKE_AGENT": "{}"}
        patch = mock.patch.dict(os.environ, self.env)
        patch.start()
        self.addCleanup(patch.stop)
        for name in ("TWINLAB2_PASSWORD", "TWINLAB2_TRUST_PROXY_USER", "CLAUDE_MODEL"):
            os.environ.pop(name, None)
        for url in (self.github.url, self.anthropic.url):
            self.assertTrue(url.startswith("http://127.0.0.1:"), url)
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
        freeze_package(self.client, self.token, f"/apps/{self.app_id}")
        with closing(builds.connect(self.database)) as db:
            self.revision = packages.latest(db, self.app_id)["revision"]
        self.addCleanup(self.stop_runners)

    # --- helpers ---

    def post(self, path, **values):
        return self.client.post(path, data={"token": self.token, **values})

    def page(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, response.data[:500])
        return response.data.decode()

    def build(self, build_id=1):
        with closing(builds.connect(self.database)) as db:
            return dict(builds.get(db, build_id))

    def messages(self, build_id=1):
        with closing(builds.connect(self.database)) as db:
            return [dict(m) for m in builds.messages(db, build_id)]

    def start(self, **values):
        os.environ["TWINLAB2_FAKE_AGENT"] = json.dumps(self.plan)
        values = {"revision": self.revision, "repo": REPO, "provider": self.provider, "budget": "5",
                  "request_key": "k1", **values}
        response = self.post(f"/apps/{self.app_id}/build", **values)
        self.assertEqual(response.status_code, 302)
        return int(response.location.rsplit("/", 1)[-1])

    def until(self, check, timeout=60, what="the session"):
        deadline = time.time() + timeout
        while time.time() < deadline:
            worker.run(self.database, once=True, ctx=self.ctx, max_jobs=1, lease=5)
            if check():
                return
            time.sleep(0.2)
        self.fail(f"timed out waiting for {what}: {self.build()} {self.runner_log()}")

    def state(self, build_id=1):
        try:
            return json.loads((Path(self.build(build_id)["workspace"]) / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def runner_log(self, build_id=1):
        path = Path(self.build(build_id)["workspace"]) / "runner.log"
        return path.read_text(encoding="utf-8", errors="replace")[-2000:] if path.exists() else ""

    def prompts(self, build_id=1):
        path = Path(self.build(build_id)["workspace"]) / "agent-prompts.txt"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def stop_runners(self):
        root = Path(self.work.name) / "workspaces"
        for state in root.glob("*/state.json"):
            (state.parent / "inbox").mkdir(exist_ok=True)
            (state.parent / "inbox" / "cancel").write_text("1", encoding="utf-8")
        deadline = time.time() + 15
        while time.time() < deadline:  # let them exit before the folder goes
            states = []
            for p in root.glob("*/state.json"):
                try:
                    states.append(json.loads(p.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    states.append({})
            if all(s.get("exited") for s in states):
                return
            time.sleep(0.2)

    def answer(self, text, key, build_id=1, **values):
        self.post(f"/builds/{build_id}/answer", answer=text, request_key=key, **values)

    # --- tests ---

    def test_clarification_then_code_then_branch_and_pull_request(self):
        self.anthropic.plan = ["QUESTION: Is a zero quantity allowed?", "Zero is refused.\nREADY TO BUILD"]
        build_id = self.start()
        self.until(lambda: self.build()["state"] == "waiting_for_answer", what="the review question")
        page = self.page(f"/builds/{build_id}")
        self.assertIn("Is a zero quantity allowed?", page)
        first = self.anthropic.bodies[0]
        self.assertEqual(first["model"], "claude-opus-5-5")  # the default model
        sent = json.dumps(first)
        self.assertIn("BUILD_TASK.md", sent)
        self.assertNotIn("attachments/", sent)
        self.assertEqual(self.anthropic.headers[0].get("anthropic-beta"), "server-side-fallback-2026-07-01")
        self.answer("No: refuse it and save nothing.", "a1")
        self.until(lambda: self.build()["state"] == "ready_for_verification", what="the build")
        build = self.build()
        self.assertEqual([m["state"] for m in self.messages() if m["direction"] == "to_provider"], ["acknowledged"])
        branch_head = git("rev-parse", f"refs/heads/{build['branch']}", cwd=self.remote)
        self.assertEqual(build["head_sha"], branch_head)
        self.assertNotEqual(build["head_sha"], self.base)
        self.assertEqual(build["pr_number"], 1)
        opened = self.github.repos[REPO]["opened"][1]
        self.assertEqual((opened["head"], opened["base"]), (build["branch"], "main"))
        self.assertIn("app.py", git("ls-tree", "--name-only", build["head_sha"], cwd=self.remote))
        self.assertEqual(build["cost"], 0.25)
        self.assertEqual(build["cost_unit"], "USD")
        prompt = self.prompts()[0]
        self.assertIn("Zero is refused.", prompt["prompt"])  # what clarification agreed
        self.assertIn(build["branch"], prompt["prompt"])
        self.assertTrue(prompt["env_key"])
        self.assertFalse(prompt["env_github"])  # the agent never gets the GitHub token
        self.assertEqual(len(self.anthropic.bodies), 2)
        self.assertEqual([m["role"] for m in self.anthropic.bodies[1]["messages"]], ["user", "assistant", "user"])
        self.assertIn("Ready for verification", self.page(f"/builds/{build_id}"))

    def test_a_lost_clarification_answer_is_delivery_uncertain_and_not_resent(self):
        self.anthropic.plan = ["QUESTION: Currency?", "drop", "EUR it is.\nREADY TO BUILD"]
        self.start()
        self.until(lambda: self.build()["state"] == "waiting_for_answer")
        self.answer("EUR", "a1")
        self.until(lambda: any(m["state"] == "delivery_uncertain" for m in self.messages()), what="uncertain delivery")
        self.assertEqual(len(self.anthropic.bodies), 2)
        time.sleep(1)
        self.until(lambda: True)
        self.assertEqual(len(self.anthropic.bodies), 2)  # never sent again by itself
        self.assertIn("Delivery uncertain", self.page("/builds/1"))
        self.answer("EUR, as I said.", "a2")  # the person answers again, deliberately
        self.until(lambda: self.build()["state"] == "ready_for_verification")
        self.assertEqual(len(self.anthropic.bodies), 3)

    def test_an_agent_question_and_a_tool_approval_go_through_the_chat(self):
        self.anthropic.plan = ["Nothing open.\nREADY TO BUILD"]
        self.plan = {"ask": "Keep the old order numbers?", "tool": "Bash"}
        self.start()
        self.until(lambda: "Keep the old order numbers?" in self.page("/builds/1"), what="the agent's question")
        self.answer("Yes, keep them.", "a1")
        self.until(lambda: any(m["kind"] == "tool_approval" for m in self.messages()), what="the tool request")
        asked = [m for m in self.messages() if m["kind"] == "tool_approval"][0]
        self.assertEqual(json.loads(asked["data"])["tool"], "Bash")
        self.post(f"/builds/1/tools/{asked['id']}", decision="deny", request_key=f"tool:{asked['id']}")
        self.until(lambda: self.build()["state"] == "ready_for_verification")
        head = self.build()["head_sha"]
        self.assertEqual(git("show", f"{head}:TOOL.txt", cwd=self.remote), "denied")
        prompts = self.prompts()
        self.assertEqual(prompts[1]["prompt"], "Yes, keep them.")
        self.assertEqual(prompts[1]["resume"], "fake-agent-session")

    def test_interrupt_then_continue(self):
        self.plan = {"hold": True}
        self.start()
        self.until(lambda: self.state().get("phase") == "implement", what="the agent")
        self.post("/builds/1/interrupt")
        self.until(lambda: self.build()["state"] == "waiting_for_answer", what="the interrupt")
        self.answer("Carry on, please.", "a1")
        self.until(lambda: self.build()["state"] == "ready_for_verification")

    def test_cancel_stops_the_runner(self):
        self.plan = {"hold": True}
        self.start()
        self.until(lambda: self.state().get("phase") == "implement", what="the agent")
        self.post("/builds/1/cancel")
        self.until(lambda: self.state().get("phase") == "cancelled", what="the runner to stop")
        self.until(lambda: self.state().get("exited"), what="the runner to exit")
        self.assertEqual(self.build()["state"], "cancelled")

    def test_a_runner_that_died_is_started_again_and_resumes(self):
        self.plan = {"hold": True}
        self.start()
        self.until(lambda: self.state().get("phase") == "implement", what="the agent")
        self.assertEqual(len(self.anthropic.bodies), 1)
        os.kill(self.state()["pid"], signal.SIGTERM)  # the machine, or the process, went away
        time.sleep(0.5)
        with mock.patch.object(local_runner, "STALE_SECONDS", 1.0):
            time.sleep(1.2)
            self.until(lambda: json.loads(self.build()["provider_state"]).get("restarts") == 1, what="a restart")
        self.until(lambda: self.build()["state"] == "ready_for_verification")
        self.assertEqual(len(self.anthropic.bodies), 1)  # clarification is not repeated
        self.assertTrue(self.prompts()[-1]["restarted"])
        self.assertIn("Build the TwinLab2 application", self.prompts()[-1]["prompt"])  # no agent session yet: again

    def test_budget_and_model_are_the_sessions(self):
        response = self.post(f"/apps/{self.app_id}/build", revision=self.revision, repo=REPO, provider="claude",
                             budget="0.2", request_key="k0")  # below the minimum
        self.assertEqual(response.location, f"/apps/{self.app_id}/build")
        self.assertIn("between 0.5 and 500", self.page(f"/apps/{self.app_id}/build"))
        os.environ["CLAUDE_MODEL"] = "claude-sonnet-5-5"
        self.start(budget="2.5", request_key="k2", model="")
        self.until(lambda: len(self.anthropic.bodies) == 1, what="the review")
        self.assertEqual(self.anthropic.bodies[0]["model"], "claude-sonnet-5-5")


class CopilotSessionTest(ClaudeSessionTest):
    """The same runner with the Copilot backend (a fake agent: the SDK is not exercised)."""
    provider = "copilot"

    def test_clarification_then_code_then_branch_and_pull_request(self):
        build_id = self.start(budget="10")
        self.until(lambda: self.build()["state"] == "ready_for_verification")
        build = self.build()
        self.assertEqual(self.anthropic.bodies, [])  # no Messages API clarification for Copilot
        self.assertEqual(build["head_sha"], git("rev-parse", f"refs/heads/{build['branch']}", cwd=self.remote))
        self.assertEqual((build["budget_unit"], build["pr_number"]), ("premium requests", 1))
        self.assertIn("GitHub Copilot", self.page(f"/builds/{build_id}"))

    def test_copilot_is_off_without_its_flag(self):
        os.environ.pop("TWINLAB2_ENABLE_COPILOT")
        self.post(f"/apps/{self.app_id}/build", revision=self.revision, repo=REPO, provider="copilot", budget="10",
                  request_key="k0")
        self.assertIn("not enabled", self.page(f"/apps/{self.app_id}/build"))
        with closing(builds.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM builds").fetchone()[0], 0)

    test_a_lost_clarification_answer_is_delivery_uncertain_and_not_resent = None
    test_budget_and_model_are_the_sessions = None

    def test_an_agent_question_and_a_tool_approval_go_through_the_chat(self):
        self.plan = {"ask": "Keep the old order numbers?", "tool": "shell"}
        self.start(budget="10")
        self.until(lambda: "Keep the old order numbers?" in self.page("/builds/1"), what="the agent's question")
        self.answer("Yes.", "a1")
        self.until(lambda: any(m["kind"] == "tool_approval" for m in self.messages()), what="the tool request")
        asked = [m for m in self.messages() if m["kind"] == "tool_approval"][0]
        self.post(f"/builds/1/tools/{asked['id']}", decision="approve", request_key=f"tool:{asked['id']}")
        self.until(lambda: self.build()["state"] == "ready_for_verification")
        self.assertEqual(git("show", f"{self.build()['head_sha']}:TOOL.txt", cwd=self.remote), "approved")

    def start(self, **values):
        values.setdefault("budget", "10")
        return super().start(**values)

    def test_a_runner_that_died_is_started_again_and_resumes(self):
        self.plan = {"hold": True}
        self.start()
        self.until(lambda: self.state().get("phase") == "implement", what="the agent")
        os.kill(self.state()["pid"], signal.SIGTERM)
        time.sleep(0.5)
        with mock.patch.object(local_runner, "STALE_SECONDS", 1.0):
            time.sleep(1.2)
            self.until(lambda: json.loads(self.build()["provider_state"]).get("restarts") == 1, what="a restart")
        self.until(lambda: self.build()["state"] == "ready_for_verification")


class ClarifyTest(unittest.TestCase):
    def test_a_wrong_checksum_is_refused_before_any_call(self):
        from twinlab2.providers.clarify import Clarifier
        fake = FakeAnthropic().start()
        self.addCleanup(fake.stop)
        clarifier = Clarifier(fake.key, base_url=fake.url, timeout=2)
        self.addCleanup(clarifier.close)
        with self.assertRaises(ValueError):
            clarifier.start(b"not the approved bytes", "0" * 64)
        self.assertEqual(fake.bodies, [])

    def test_questions_and_ready(self):
        from twinlab2.providers.clarify import questions_of
        self.assertEqual(questions_of("Intro\nQUESTION: A?\nquestion: B?"), (["A?", "B?"], False))
        self.assertEqual(questions_of("All agreed.\nREADY TO BUILD"), ([], True))


if __name__ == "__main__":
    unittest.main()
