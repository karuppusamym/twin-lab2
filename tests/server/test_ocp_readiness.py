"""OpenShift readiness and the API's conventions (review-notes ui-api-review problems 6, 8, 10 and
the Auth section), each reproduced first and then held fixed:

1. health: `/healthz` was one database query, and a kubelet's probe (`Host: <pod-ip>:8080`) got
   400 from the host check; now `/livez`, `/readyz` (database at the current migration, artifact
   store answers; 503 with Retry-After otherwise) and `/healthz` as its alias, all answered
   whatever the Host;
2. cookies and CSRF: a refused request (401) set a session cookie; the CSRF token lived across
   sign-in and sign-out;
3. errors: `type` was `about:blank` and most codes were in no list; no Request-Id; a 403 said
   nothing about how to get access;
4. conventions: Idempotency-Key on the create-type POSTs (request_key kept as an alias).

The deploy manifests are checked in test_deploy_manifests.py.
"""

import ast
from contextlib import closing
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import contract
from support import Client, connect, new_database
from twinlab2 import artifacts, auth, idempotency, migrations, rest
from twinlab2.portal import create_app

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "server" / "twinlab2"
NOW = "2026-11-21T00:00:00+00:00"
AUTH_ENV = ("TWINLAB2_AUTH_MODE", "TWINLAB2_TRUST_PROXY_USER", "TWINLAB2_PASSWORD", "TWINLAB2_ADMINS",
            "TWINLAB2_SHARED_USER", "TWINLAB2_TRUSTED_PROXIES", "TWINLAB2_LOCAL_DEV_FORCE", "TWINLAB2_HOSTS")
POD = {"Host": "10.1.2.3:8080"}


class Portal(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.database = new_database(self.work.name)
        env = mock.patch.dict(os.environ, {"TWINLAB2_SECRET_KEY": "test-only", "TWINLAB2_DEV_WORKER": "0"})
        env.start()
        self.addCleanup(env.stop)
        for name in AUTH_ENV:
            os.environ.pop(name, None)

    def portal(self, remote="127.0.0.1", user=None, **env):
        with mock.patch.dict(os.environ, env):
            self.app = create_app(self.database)
        return Client(self.app, user, remote=remote)

    def users(self, *people):
        with closing(connect(self.database)) as db:
            migrations.run(db)
            with db:
                for name, password, admin in people:
                    auth.save_user(db, name, password=password, is_admin=admin, by="test", now=NOW)


# -- 1: health -------------------------------------------------------------------------------------


class HealthTest(Portal):
    def test_a_kubelet_probe_with_the_pod_ip_as_host_gets_200(self):
        """Reproduced: GET /healthz with Host: 10.1.2.3:8080 was 400 Invalid host header."""
        client = self.portal()
        for path in ("/livez", "/readyz", "/healthz"):
            response = client.http.get(path, headers=POD)
            self.assertEqual(response.status_code, 200, (path, response.text))
            self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(client.http.head("/readyz", headers=POD).status_code, 200)
        # the host check still guards everything else
        self.assertEqual(client.http.get("/api/v1/session", headers=POD).status_code, 400)
        self.assertEqual(client.http.get("/", headers=POD).status_code, 400)
        self.assertEqual(client.http.post("/livez", headers=POD).status_code, 400)

    def test_readyz_reports_the_database_and_the_artifact_store(self):
        client = self.portal()
        body = client.http.get("/readyz").json()
        self.assertEqual(body, {"ok": True, "checks": {"database": "ok", "artifacts": "ok"}})
        self.assertEqual(client.http.get("/healthz").json(), body)  # the alias
        self.assertEqual(client.http.get("/livez").json(), {"ok": True})

    def test_readyz_is_503_with_retry_after_while_the_database_is_behind(self):
        client = self.portal()
        with mock.patch.object(migrations, "latest", lambda: migrations.MIGRATIONS[-1][0] + 1):
            response = client.http.get("/readyz", headers=POD)
            self.assertEqual(client.http.get("/livez", headers=POD).status_code, 200)  # alive: not restarted
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["retry-after"], "5")
        self.assertFalse(response.json()["ok"])
        self.assertTrue(response.json()["checks"]["database"].startswith("migrating"))

    def test_readyz_is_503_when_the_database_or_the_store_does_not_answer(self):
        client = self.portal()
        with mock.patch.object(artifacts.LocalStore, "ping", side_effect=artifacts.ArtifactError("gone")):
            response = client.http.get("/readyz")
        self.assertEqual((response.status_code, response.json()["checks"]["artifacts"]),
                         (503, "unreachable (ArtifactError)"))
        with mock.patch.object(migrations, "applied", side_effect=OSError("connection refused")):
            response = client.http.get("/readyz")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["checks"]["database"], "unreachable (OSError)")

    def test_the_s3_store_pings_its_bucket(self):
        calls = []

        class Client3:
            def head_bucket(self, **kw):
                calls.append(kw)

        artifacts.S3Store("bucket", client=Client3()).ping()
        self.assertEqual(calls, [{"Bucket": "bucket"}])

    def test_probes_are_not_logged_other_requests_are_with_their_id(self):
        client = self.portal()
        with self.assertLogs("twinlab2.http", logging.INFO) as logs:
            client.http.get("/livez")
            client.http.get("/readyz")
            response = client.http.get("/api/v1/session")
        lines = [r for r in logs.records if r.name == "twinlab2.http"]
        self.assertEqual(len(lines), 1, [r.getMessage() for r in lines])
        self.assertIn("GET /api/v1/session 200", lines[0].getMessage())
        self.assertEqual(lines[0].request_id, response.headers["request-id"])


# -- 2: cookies and CSRF -------------------------------------------------------------------------


class CookieTest(Portal):
    def test_a_refused_request_sets_no_session_cookie(self):
        """Reproduced: GET /api/v1/apps signed out answered 401 with Set-Cookie: session=…."""
        self.users(("ann", "ann-password-1", False))
        client = self.portal(remote="10.0.0.9", TWINLAB2_AUTH_MODE="individual")
        refused = client.http.get("/api/v1/apps")
        self.assertEqual(refused.status_code, 401)
        self.assertNotIn("set-cookie", refused.headers)
        self.assertNotIn("set-cookie", client.http.get("/api/v1/admin/users").headers)
        # the session's form token still starts the cookie (sign-in needs it)
        self.assertIn("set-cookie", client.http.get("/api/v1/session").headers)

    def test_the_csrf_token_rotates_on_sign_in_and_sign_out(self):
        self.users(("ann", "ann-password-1", False))
        client = self.portal(remote="10.0.0.9", TWINLAB2_AUTH_MODE="individual")
        before = client.token
        signed_in = client.post("/api/v1/session", json={"user": "ann", "password": "ann-password-1"})
        self.assertEqual(signed_in.status_code, 200, signed_in.text)
        during = signed_in.json()["csrf_token"]
        self.assertNotEqual(during, before)
        # the token read before sign-in no longer works
        stale = client.post("/api/v1/apps", json={"name": "X"}, headers={"X-CSRF-Token": before})
        self.assertEqual((stale.status_code, stale.json()["code"]), (403, "csrf"))
        self.assertEqual(client.post("/api/v1/apps", json={"name": "X"}).status_code, 201)
        self.assertEqual(client.delete("/api/v1/session").status_code, 204)
        after = client.token
        self.assertNotIn(after, (before, during))
        stale = client.http.post("/api/v1/session", json={"user": "ann", "password": "ann-password-1"},
                                 headers={"X-CSRF-Token": during})
        self.assertEqual((stale.status_code, stale.json()["code"]), (403, "csrf"))


# -- 3: errors --------------------------------------------------------------------------------------


def emitted_codes():
    """Every code literal of a fail(…) / Problem(…) call in the server: (code, file:line)."""
    out = []
    for path in sorted(SERVER.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        constants = {t.id: n.value.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
                     for t in n.targets if isinstance(t, ast.Name) and isinstance(n.value, ast.Constant)
                     and isinstance(n.value.value, str)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name not in ("fail", "Problem"):
                continue
            code = node.args[2] if len(node.args) > 2 else next((k.value for k in node.keywords if k.arg == "code"), None)
            if isinstance(code, ast.Constant) and isinstance(code.value, str):
                out.append((code.value, f"{path.name}:{node.lineno}"))
            elif isinstance(code, ast.Name) and code.id in constants:
                out.append((constants[code.id], f"{path.name}:{node.lineno}"))
    return out


class ErrorModelTest(Portal):
    def test_every_code_the_server_emits_is_registered_and_in_the_spec(self):
        spec_codes = contract.spec()["components"]["schemas"]["ErrorCode"]["enum"]
        self.assertEqual(list(rest.CODES), spec_codes)
        described = contract.spec()["components"]["schemas"]["ErrorCode"]["x-twinlab-codes"]
        self.assertEqual({c: d for c, (_, d) in rest.CODES.items()}, described)
        found = emitted_codes()
        self.assertGreater(len(found), 15)
        self.assertEqual([f for f in found if f[0] not in rest.CODES], [])
        self.assertTrue(set(rest.DEFAULT_CODE.values()) <= set(rest.CODES))

    def test_every_problem_has_a_type_per_code_and_the_request_id(self):
        """Reproduced: every problem was `type: about:blank`, a 404 had no code."""
        client = self.portal()
        cases = [(client.get("/api/v1/apps/999"), 404, "not_found"),
                 (client.get("/api/v1/nothing-here"), 404, "not_found"),
                 (client.post("/api/v1/apps", json={"name": 5}), 400, "invalid_request"),
                 (client.post("/api/v1/apps", json={"name": "X"}, csrf=False), 403, "csrf")]
        for response, status, code in cases:
            body = response.json()
            self.assertEqual((response.status_code, body["code"], body["type"]), (status, code, f"/errors/{code}"))
            self.assertEqual(body["request_id"], response.headers["request-id"])
        doc = client.http.get("/errors/not_found").json()
        self.assertEqual(doc, {"type": "/errors/not_found", "code": "not_found", "status": 404,
                               "description": rest.CODES["not_found"][1]})
        self.assertEqual(client.http.get("/errors/nope").status_code, 404)

    def test_every_response_has_a_request_id_and_a_callers_id_is_kept(self):
        client = self.portal()
        ids = {client.http.get(p).headers["request-id"] for p in ("/livez", "/", "/api/v1/session", "/api/agent/nope")}
        self.assertEqual(len(ids), 4)
        kept = client.http.get("/api/v1/session", headers={"X-Request-Id": "router-abc12345"})
        self.assertEqual(kept.headers["request-id"], "router-abc12345")
        made = client.http.get("/api/v1/session", headers={"Request-Id": "bad id\twith spaces"})
        self.assertRegex(made.headers["request-id"], r"^[0-9a-f]{16}$")

    def test_a_crash_names_its_request_id(self):
        from fastapi.testclient import TestClient

        self.portal()
        client = Client(self.app, _http=TestClient(self.app, base_url="http://localhost", client=("127.0.0.1", 50000),
                                                   raise_server_exceptions=False))
        with mock.patch("twinlab2.routes_portal.session_view", side_effect=RuntimeError("boom")), \
                self.assertLogs("twinlab2.http", logging.ERROR):
            crashed = client.get("/api/v1/session")
        self.assertEqual((crashed.status_code, crashed.json()["code"]), (500, "internal"))
        self.assertIn(crashed.headers["request-id"], crashed.json()["detail"])


# -- 4: Idempotency-Key ------------------------------------------------------------------------------


class IdempotencyTest(Portal):
    def test_the_same_key_replays_the_first_answer(self):
        client = self.portal()
        key = {"Idempotency-Key": "form-0001"}
        first = client.post("/api/v1/apps", json={"name": "Desk"}, headers=key)
        again = client.post("/api/v1/apps", json={"name": "Desk"}, headers=key)
        self.assertEqual((first.status_code, again.status_code), (201, 201))
        self.assertEqual(first.json(), again.json())
        self.assertNotIn(idempotency.REPLAYED.lower(), first.headers)
        self.assertEqual(again.headers[idempotency.REPLAYED.lower()], "true")
        names = [a["name"] for a in client.get("/api/v1/apps").json()["apps"]]
        self.assertEqual(names, ["Desk"])  # created once
        other = client.post("/api/v1/apps", json={"name": "Desk"}, headers={"Idempotency-Key": "form-0002"})
        self.assertNotEqual(other.json()["id"], first.json()["id"])  # a new key is a new action
        self.assertEqual(client.post("/api/v1/apps", json={"name": "Desk 3"}).status_code, 201)  # no key: as before

    def test_the_same_key_for_another_request_is_422(self):
        client = self.portal()
        key = {"Idempotency-Key": "form-0003"}
        self.assertEqual(client.post("/api/v1/apps", json={"name": "A"}, headers=key).status_code, 201)
        reused = client.post("/api/v1/apps", json={"name": "B"}, headers=key)
        self.assertEqual((reused.status_code, reused.json()["code"]), (422, "idempotency_key_reused"))

    def test_a_failed_request_keeps_no_key_and_a_running_one_is_409(self):
        client = self.portal()
        key = {"Idempotency-Key": "form-0004"}
        self.assertEqual(client.post("/api/v1/apps", json={"name": ""}, headers=key).status_code, 400)
        self.assertEqual(client.post("/api/v1/apps", json={"name": "Fixed"}, headers=key).status_code, 201)
        with closing(connect(self.database)) as db, db:
            db.execute("INSERT INTO idempotency_keys (user_name, idem_key, fingerprint, state, created_at) "
                       "VALUES ('local', 'busy-key', 'x', 'pending', ?)", (idempotency._stamp(idempotency._now()),))
        # a pending key with another fingerprint is "another request": 422; with the same one, 409
        busy = client.post("/api/v1/apps", json={"name": "Y"}, headers={"Idempotency-Key": "busy-key"})
        self.assertEqual(busy.status_code, 422)
        with mock.patch.object(idempotency, "fingerprint", return_value="x"):
            busy = client.post("/api/v1/apps", json={"name": "Y"}, headers={"Idempotency-Key": "busy-key"})
        self.assertEqual((busy.status_code, busy.json()["code"], busy.headers["retry-after"]),
                         (409, "idempotency_in_progress", "1"))
        bad = client.post("/api/v1/apps", json={"name": "Z"}, headers={"Idempotency-Key": "has space"})
        self.assertEqual((bad.status_code, bad.json()["code"]), (400, "invalid_request"))

    def test_keys_are_the_callers_own(self):
        client = self.portal(user=None, TWINLAB2_AUTH_MODE="trusted-proxy", TWINLAB2_TRUSTED_PROXIES="127.0.0.1")
        key = {"Idempotency-Key": "shared-0001"}
        ann = client.as_user("ann").post("/api/v1/apps", json={"name": "Ann's"}, headers=key)
        bob = client.as_user("bob").post("/api/v1/apps", json={"name": "Bob's"}, headers=key)
        self.assertEqual((ann.status_code, bob.status_code), (201, 201))
        self.assertNotEqual(ann.json()["id"], bob.json()["id"])

    def test_the_spec_names_every_idempotent_operation(self):
        flagged = set()
        for route in rest.all_routes(self.portal().app.router):
            kind = getattr(getattr(route, "endpoint", None), "twinlab_idempotent", None)
            if kind:
                for method in route.methods:
                    flagged.add((method.lower(), contract_path(route.path), kind))
        in_spec = {(m, path, op["x-twinlab-idempotent"]) for path, item in contract.spec()["paths"].items()
                   for m, op in item.items() if isinstance(op, dict) and "x-twinlab-idempotent" in op}
        self.assertEqual(flagged, in_spec)
        for method, path, _ in in_spec:
            params = contract.spec()["paths"][path][method]["parameters"]
            self.assertIn({"$ref": "#/components/parameters/IdempotencyKey"}, params)


def contract_path(path):
    """A FastAPI path (`/apps/{app_id:int}`) as the spec writes it (`/apps/{app_id}`)."""
    import re

    return re.sub(r"\{(\w+):[^}]+\}", r"{\1}", path)


if __name__ == "__main__":
    unittest.main()
