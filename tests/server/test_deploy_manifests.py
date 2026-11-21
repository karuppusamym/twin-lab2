"""deploy/: what the OpenShift manifest and the compose file promise, checked without a cluster
(the schema check is kubeconform in the `image` CI job; the image itself is smoke-tested there).

Reproduced first (review-notes ui-api-review problems 6, 8 and the Auth section): the portal
container had no readiness or liveness probe; the worker was a sidecar of the portal pod (it
scaled with it, its OOM restarted the portal, and it had no TWINLAB2_SECRET_KEY);
TWINLAB2_SECURE_COOKIES was not set; one replica with a default rolling update.
"""

from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import tempfile
import threading
import unittest

import yaml

from support import connect, new_database
from twinlab2 import jobqueue, migrations, probe

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "deploy" / "openshift-portal.yaml"
COMPOSE = ROOT / "deploy" / "docker-compose.yaml"


def manifest():
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["items"]


def of_kind(kind):
    return [item for item in manifest() if item["kind"] == kind]


def deployment(name):
    return next(d for d in of_kind("Deployment") if d["metadata"]["name"] == name)


def containers(dep):
    return {c["name"]: c for c in dep["spec"]["template"]["spec"]["containers"]}


def env(container):
    return {e["name"]: e for e in container.get("env", [])}


def documented_secrets():
    """{secret name: {keys}}: the `oc create secret generic` of the header, and the serving
    certificates the Services ask OpenShift for."""
    header = MANIFEST.read_text(encoding="utf-8").split("apiVersion:", 1)[0]
    command = re.search(r"oc create secret generic (\S+)(.*?)\n#\s*oc apply", header, re.S)
    secrets = {command.group(1): set(re.findall(r"--from-literal=([\w.-]+)=", command.group(2)))}
    for service in of_kind("Service"):
        name = service["metadata"].get("annotations", {}).get("service.beta.openshift.io/serving-cert-secret-name")
        if name:
            secrets[name] = {"tls.crt", "tls.key"}
    return secrets


class ManifestTest(unittest.TestCase):
    def test_every_container_has_probes(self):
        for dep in of_kind("Deployment"):
            for name, c in containers(dep).items():
                where = f"{dep['metadata']['name']}/{name}"
                self.assertIn("livenessProbe", c, where)
                self.assertIn("readinessProbe", c, where)
                ports = {p["name"] for p in c.get("ports", [])} | {
                    p["name"] for other in containers(dep).values() for p in other.get("ports", [])}
                for kind in ("startupProbe", "livenessProbe", "readinessProbe"):
                    check = c.get(kind)
                    if check and "httpGet" in check:
                        self.assertIn(check["httpGet"]["port"], ports, where)
                    if check and "exec" in check:
                        command = check["exec"]["command"]
                        self.assertEqual(command[:3], ["python", "-m", "twinlab2.probe"], where)

    def test_the_portal_probes_reach_what_they_mean(self):
        portal = containers(deployment("twinlab2"))
        self.assertEqual(portal["portal"]["readinessProbe"]["httpGet"]["path"], "/readyz")
        self.assertIn("http://127.0.0.1:8080/livez", portal["portal"]["livenessProbe"]["exec"]["command"])
        self.assertGreaterEqual(portal["portal"]["startupProbe"]["failureThreshold"]
                                * portal["portal"]["startupProbe"]["periodSeconds"], 300)  # migrations at start
        # the proxy lets the kubelet's /readyz through without a sign-in
        skips = [a.split("=", 1)[1] for a in portal["oauth-proxy"]["args"] if a.startswith("--skip-auth-regex=")]
        for path in ("/readyz", "/livez", "/healthz"):
            self.assertTrue(any(re.search(s, path) for s in skips), path)
        self.assertFalse(any(re.search(s, "/readyz-not") or re.search(s, "/api/v1/apps") for s in skips))

    def test_every_secret_reference_exists(self):
        known = documented_secrets()
        self.assertEqual(known["twinlab2"], {"secret-key", "cookie-secret", "database-url"})
        refs = []
        for dep in of_kind("Deployment"):
            spec = dep["spec"]["template"]["spec"]
            for c in spec["containers"]:
                for e in c.get("env", []):
                    ref = e.get("valueFrom", {}).get("secretKeyRef")
                    if ref:
                        refs.append((ref["name"], ref["key"]))
            for volume in spec.get("volumes", []):
                if "secret" in volume:
                    items = volume["secret"].get("items") or [{"key": None}]
                    refs += [(volume["secret"]["secretName"], i["key"]) for i in items]
        self.assertTrue(refs)
        for name, key in refs:
            self.assertIn(name, known, (name, key))
            if key is not None:
                self.assertIn(key, known[name], (name, key))

    def test_the_worker_is_its_own_deployment_with_the_secret_key(self):
        """Reproduced: the worker was a container of the portal's pod, without TWINLAB2_SECRET_KEY."""
        self.assertEqual(set(containers(deployment("twinlab2"))), {"portal", "oauth-proxy"})
        worker = deployment("twinlab2-worker")
        (only,) = containers(worker).values()
        self.assertEqual(only["command"], ["python", "-m", "twinlab2.worker"])
        self.assertEqual(only["image"], containers(deployment("twinlab2"))["portal"]["image"])
        for name in ("TWINLAB2_SECRET_KEY", "DATABASE_URL"):
            self.assertIn("secretKeyRef", env(only)[name]["valueFrom"], name)
        self.assertIn("limits", only["resources"])
        # the Service and the PodDisruptionBudget select portal pods only
        portal_labels = deployment("twinlab2")["spec"]["template"]["metadata"]["labels"]
        worker_labels = worker["spec"]["template"]["metadata"]["labels"]
        for selector in [of_kind("Service")[0]["spec"]["selector"],
                         of_kind("PodDisruptionBudget")[0]["spec"]["selector"]["matchLabels"]]:
            self.assertTrue(selector.items() <= portal_labels.items())
            self.assertFalse(selector.items() <= worker_labels.items())

    def test_secure_cookies_and_the_trusted_proxy_mode(self):
        portal = env(containers(deployment("twinlab2"))["portal"])
        self.assertEqual(portal["TWINLAB2_SECURE_COOKIES"]["value"], "1")
        self.assertEqual(portal["TWINLAB2_AUTH_MODE"]["value"], "trusted-proxy")
        self.assertEqual(portal["TWINLAB2_TRUSTED_PROXIES"]["value"], "127.0.0.1,::1")
        dockerfile = (ROOT / "deploy" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("TWINLAB2_AUTH_MODE=trusted-proxy", dockerfile)
        self.assertIn("TWINLAB2_SECURE_COOKIES=1", dockerfile)

    def test_a_rollout_keeps_a_ready_portal(self):
        spec = deployment("twinlab2")["spec"]
        self.assertGreaterEqual(spec["replicas"], 2)
        self.assertEqual(spec["strategy"]["rollingUpdate"]["maxUnavailable"], 0)
        self.assertGreaterEqual(spec["strategy"]["rollingUpdate"]["maxSurge"], 1)
        portal = containers(deployment("twinlab2"))
        self.assertIn("preStop", portal["portal"]["lifecycle"])
        self.assertIn("preStop", portal["oauth-proxy"]["lifecycle"])
        drain = int(portal["portal"]["lifecycle"]["preStop"]["exec"]["command"][1])
        self.assertGreater(spec["template"]["spec"]["terminationGracePeriodSeconds"], drain + 20)  # + gunicorn's own
        self.assertEqual(of_kind("PodDisruptionBudget")[0]["spec"]["minAvailable"], 1)

    def test_no_image_follows_latest(self):
        for dep in of_kind("Deployment"):
            for c in containers(dep).values():
                self.assertFalse(c["image"].endswith(":latest"), c["image"])

    def test_compose_runs_the_worker_apart_with_the_secret_key_and_checks_health(self):
        compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
        self.assertEqual(compose["worker"]["environment"]["TWINLAB2_SECRET_KEY"],
                         compose["portal"]["environment"]["TWINLAB2_SECRET_KEY"])
        self.assertIn("twinlab2.probe", " ".join(compose["portal"]["healthcheck"]["test"]))
        self.assertIn("twinlab2.probe", " ".join(compose["worker"]["healthcheck"]["test"]))


# -- the probe commands --------------------------------------------------------------------------------


class Answer(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200 if self.path == "/livez" else 503)
        self.end_headers()

    def log_message(self, *args):
        pass


class ProbeTest(unittest.TestCase):
    def test_http(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Answer)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        self.assertEqual(probe.http(base + "/livez"), (True, "200"))
        self.assertEqual(probe.http(base + "/readyz"), (False, "503"))
        self.assertFalse(probe.http("http://127.0.0.1:9/livez", timeout=1)[0])
        self.assertEqual(probe.main(["http", base + "/livez"]), 0)
        self.assertEqual(probe.main(["http", base + "/readyz"]), 1)

    def test_worker(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        database = new_database(work.name)
        with closing(connect(database)) as db:
            migrations.run(db)
            self.assertEqual(probe.worker(database, host="pod-a")[0], False)  # not started yet
            jobqueue.worker_seen(db, "w1", started=True)
            ok, why = probe.worker(database)
            self.assertTrue(ok, why)
            self.assertFalse(probe.worker(database, max_age=-1)[0])  # stale
            jobqueue.worker_seen(db, "w1", stopped=True)
            self.assertFalse(probe.worker(database)[0])
        ok, why = probe.worker("postgresql://nobody@127.0.0.1:9/none")
        self.assertTrue(ok, why)  # a database outage does not restart the worker
        self.assertIn("unreachable", why)


if __name__ == "__main__":
    unittest.main()
