"""Build runner isolation: the allowlisted environment, lease generations, the OpenShift Job and
its NetworkPolicy. No cluster, no provider, no network beyond 127.0.0.1."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import yaml

from twinlab2.providers import base, local_runner, openshift_job, runner, sandbox

ROOT = Path(__file__).resolve().parents[2]
PLANTED = {"AWS_SECRET_ACCESS_KEY": "planted-aws", "GITHUB_TOKEN": "planted-gh", "DATABASE_URL": "postgresql://x",
           "SOME_NEW_CLOUD_TOKEN": "planted-new"}


class EnvironmentTest(unittest.TestCase):
    def test_a_planted_secret_in_the_parent_is_not_visible_to_the_child(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, PLANTED):
            env = local_runner.LocalRunnerProvider({"anthropic_api_key": "k"}).runner_env(Path(folder))
            seen = subprocess.run([sys.executable, "-c", "import os, json; print(json.dumps(dict(os.environ)))"],
                                  env=env, capture_output=True, text=True, check=True,
                                  **sandbox.popen_options() if sys.platform == "win32" else {}).stdout
        child = json.loads(seen)
        for name in PLANTED:
            self.assertNotIn(name, child)
        self.assertNotIn("planted", seen)
        self.assertNotIn("ANTHROPIC_API_KEY", child)  # not even the provider key outside its phase
        self.assertTrue(Path(child.get("HOME") or child["USERPROFILE"]).is_relative_to(Path(folder).resolve().parent)
                        or folder in (child.get("HOME") or child["USERPROFILE"]))

    def test_only_the_implement_phase_gets_the_provider_key(self):
        secrets = {"anthropic_api_key": "sk-test", "github_token": "gh-test"}
        self.assertNotIn("ANTHROPIC_API_KEY", sandbox.with_credentials({}, "setup", secrets))
        self.assertNotIn("ANTHROPIC_API_KEY", sandbox.with_credentials({}, "clarify", secrets))
        keyed = sandbox.with_credentials({"PATH": "p"}, "implement", secrets)
        self.assertEqual(keyed["ANTHROPIC_API_KEY"], "sk-test")
        self.assertNotIn("gh-test", json.dumps(keyed))

    def test_the_runner_never_gets_the_github_token(self):
        adapter = local_runner.LocalRunnerProvider({"anthropic_api_key": "k", "github_token": "gh-secret"})
        self.assertEqual(adapter.github_token, "gh-secret")
        self.assertNotIn("github_token", adapter.secrets)

    def test_a_workspace_of_another_session_is_not_reused(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / "session-1"
            sandbox.fresh_workspace(folder, "tag-a")
            (folder / "left-over.txt").write_text("x", encoding="utf-8")
            sandbox.fresh_workspace(folder, "tag-a")
            self.assertTrue((folder / "left-over.txt").exists())
            sandbox.fresh_workspace(folder, "tag-b")
            self.assertFalse((folder / "left-over.txt").exists())


class LeaseTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.folder = Path(self.work.name)
        (self.folder / "spec.json").write_text(json.dumps({"tag": "t1"}), encoding="utf-8")

    def test_a_superseded_runner_writes_nothing(self):
        local_runner.LocalRunnerProvider.grant(self.folder, 1)
        session = runner.Session(self.folder, {}, generation=1)
        session.emit("status", "from generation 1")
        local_runner.LocalRunnerProvider.grant(self.folder, 2)
        before = (self.folder / "state.json").read_bytes(), (self.folder / "events.jsonl").read_bytes()
        with self.assertRaises(runner.Superseded):
            session.save(phase="done")
        with self.assertRaises(runner.Superseded):
            session.emit("status", "late")
        with self.assertRaises(runner.Superseded):
            session._check_controls()
        self.assertEqual(before, ((self.folder / "state.json").read_bytes(), (self.folder / "events.jsonl").read_bytes()))

    def test_the_adapter_rejects_updates_of_another_generation(self):
        lines = [{"id": "e0", "gen": 1, "kind": "status", "text": "ok"},
                 {"id": "e1", "gen": 1, "kind": "status", "text": "obsolete"},
                 {"id": "g2e2", "gen": 2, "kind": "status", "text": "new"}]
        (self.folder / "events.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
        import time

        (self.folder / "state.json").write_text(json.dumps({"generation": 1, "phase": "done", "head_sha": "a" * 40,
                                                            "heartbeat": time.time()}), encoding="utf-8")
        adapter = local_runner.LocalRunnerProvider({})
        adapter.label = "Fake"
        build = {"workspace": str(self.folder), "runner_generation": 2, "event_cursor": "1", "provider_state": "{}",
                 "branch": "b", "base_sha": "0" * 40}
        snap = adapter.read(build)
        self.assertEqual([e.text for e in snap.events], ["new"])
        self.assertEqual(snap.rejected, 1)
        self.assertEqual(snap.state, base.RUNNING)  # generation 1's "done" is not taken as the result
        self.assertIsNone(snap.head_sha)

    def test_a_commit_report_of_another_generation_is_not_published(self):
        adapter = local_runner.LocalRunnerProvider({})
        with self.assertRaises(local_runner.PublishRefused):
            adapter.publish({"workspace": str(self.folder), "branch": "b", "base_sha": "0" * 40},
                            {"bundle": "out/g1.bundle", "head_sha": "a" * 40}, 2)


class Kube(BaseHTTPRequestHandler):
    calls = []

    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}") if length else None
        Kube.calls.append((self.command, self.path, self.headers.get("Authorization"), body))
        doc = {"metadata": {"uid": "uid-1"}} if self.path.endswith("/jobs") else {}
        out = json.dumps(doc).encode()
        self.send_response(201 if self.command == "POST" else 200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_POST = do_DELETE = _answer

    def log_message(self, *args):
        pass


class OpenShiftJobTest(unittest.TestCase):
    def settings(self):
        return openshift_job.JobSettings(namespace="tl2", image="registry/twinlab2:1")

    def test_the_job_is_locked_down(self):
        job = openshift_job.job_manifest(self.settings(), 7, 2, "claude", resume=True)
        self.assertEqual(job["metadata"]["name"], "twinlab2-build-7-g2")
        spec = job["spec"]
        self.assertEqual(spec["backoffLimit"], 0)
        pod = spec["template"]["spec"]
        self.assertIs(pod["automountServiceAccountToken"], False)
        self.assertEqual(pod["serviceAccountName"], "twinlab2-build-runner")
        self.assertEqual(pod["securityContext"]["seccompProfile"]["type"], "RuntimeDefault")
        container = pod["containers"][0]
        sc = container["securityContext"]
        self.assertTrue(sc["readOnlyRootFilesystem"])
        self.assertFalse(sc["allowPrivilegeEscalation"])
        self.assertEqual(sc["capabilities"]["drop"], ["ALL"])
        self.assertIn("--generation", container["args"])
        self.assertEqual(container["args"][container["args"].index("--generation") + 1], "2")
        self.assertIn("--resume", container["args"])
        mount = [m for m in container["volumeMounts"] if m["name"] == "workspace"][0]
        self.assertEqual(mount["subPath"], "session-7")  # its own session folder only
        self.assertEqual(spec["template"]["metadata"]["labels"]["app.kubernetes.io/component"], "build-runner")
        names = {e["name"] for e in container["env"]}
        self.assertFalse(names & {"ANTHROPIC_API_KEY", "GITHUB_TOKEN", "DATABASE_URL"})

    def test_the_secret_holds_only_the_provider_key(self):
        doc = openshift_job.secret_manifest(self.settings(), 7, 1, {"anthropic_api_key": "k", "github_token": "g"}, "u")
        import base64

        self.assertEqual(json.loads(base64.b64decode(doc["data"]["secrets.json"])), {"anthropic_api_key": "k"})
        self.assertEqual(doc["metadata"]["ownerReferences"][0]["uid"], "u")

    def test_the_launcher_creates_the_job_then_its_secret_and_removes_the_old_generation(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Kube)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        Kube.calls = []
        launcher = openshift_job.JobLauncher(f"http://127.0.0.1:{server.server_port}", "sa-token", self.settings())
        launcher.launch(7, 2, "claude", {"anthropic_api_key": "k"}, resume=True)
        launcher.stop(7, 1)
        (m1, p1, a1, b1), (m2, p2, _, b2), (m3, p3, _, _) = Kube.calls
        self.assertEqual((m1, p1), ("POST", "/apis/batch/v1/namespaces/tl2/jobs"))
        self.assertEqual(a1, "Bearer sa-token")
        self.assertEqual((m2, p2), ("POST", "/api/v1/namespaces/tl2/secrets"))
        self.assertEqual(b2["metadata"]["ownerReferences"][0]["uid"], "uid-1")
        self.assertEqual((m3, p3), ("DELETE", "/apis/batch/v1/namespaces/tl2/jobs/twinlab2-build-7-g1"))

    def test_the_network_policy_allows_no_ingress_and_only_dns_and_the_provider(self):
        items = yaml.safe_load((ROOT / "deploy" / "openshift-builder.yaml").read_text(encoding="utf-8"))["items"]
        policy = [i for i in items if i["kind"] == "NetworkPolicy"][0]["spec"]
        self.assertEqual(policy["podSelector"]["matchLabels"], {"app.kubernetes.io/component": "build-runner"})
        self.assertEqual(policy["ingress"], [])
        self.assertEqual(sorted(policy["policyTypes"]), ["Egress", "Ingress"])
        ports = {p["port"] for rule in policy["egress"] for p in rule["ports"]}
        self.assertEqual(ports, {53, 5353, 443})
        sa = [i for i in items if i["kind"] == "ServiceAccount"][0]
        self.assertIs(sa["automountServiceAccountToken"], False)
        role = [i for i in items if i["kind"] == "Role"][0]
        self.assertEqual({r for rule in role["rules"] for r in rule["resources"]}, {"jobs", "secrets"})


if __name__ == "__main__":
    unittest.main()
