"""The external review's server findings, each reproduced and then held fixed:

1. auth modes: the shared password signed in as any name (an admin's, too); a loopback request
   was the admin `local` even with password-protected users; proxy headers were believed from
   any peer;
2. the body limit read only Content-Length: a chunked body passed it;
3. a missing TWINLAB2_SECRET_KEY made a per-process key in every mode; sessions never expired
   and a copy of the cookie outlived sign-out and password changes; local downloads read whole
   objects into memory;
4. generated OpenShift files of a release built `main` and followed `:latest`;
5. "Tested" did not say what was verified (scenarios, platform, role, commit, deployment).
"""

from contextlib import closing
import io
import json
import logging
import os
import tempfile
import unittest
from unittest import mock

import yaml
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from support import Client, connect, new_database
from test_release_states import ReleaseStatesTest
from twinlab2 import artifacts, auth, demo_target, migrations, rest
from twinlab2.portal import create_app

NOW = "2026-11-21T00:00:00+00:00"
AUTH_ENV = ("TWINLAB2_AUTH_MODE", "TWINLAB2_TRUST_PROXY_USER", "TWINLAB2_PASSWORD", "TWINLAB2_ADMINS",
            "TWINLAB2_SHARED_USER", "TWINLAB2_TRUSTED_PROXIES", "TWINLAB2_LOCAL_DEV_FORCE",
            "TWINLAB2_SESSION_HOURS", "TWINLAB2_ADMIN_GROUP")


class Portal(unittest.TestCase):
    """A fresh catalog per test; `users` are saved before the portal starts."""

    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.database = new_database(self.work.name)
        env = mock.patch.dict(os.environ, {"TWINLAB2_SECRET_KEY": "test-only"})
        env.start()
        self.addCleanup(env.stop)
        for name in AUTH_ENV:
            os.environ.pop(name, None)

    def users(self, *people):
        """(name, password, is_admin) rows of the local user store."""
        with closing(connect(self.database)) as db:
            migrations.run(db)
            with db:
                for name, password, admin in people:
                    auth.save_user(db, name, password=password, is_admin=admin, by="test", now=NOW)

    def portal(self, remote="127.0.0.1", **env):
        with mock.patch.dict(os.environ, env):
            app = create_app(self.database)
        return Client(app, remote=remote)

    def sign_in(self, client, name, password):
        return client.post("/api/v1/session", json={"user": name, "password": password})


# -- 1: auth modes -------------------------------------------------------------------------------


class SharedPasswordTest(Portal):
    def test_the_shared_password_does_not_sign_in_an_admin_name(self):
        """Reproduced: `ops` in TWINLAB2_ADMINS signed in with the shared password as an admin."""
        client = self.portal(TWINLAB2_PASSWORD="shared-pw", TWINLAB2_ADMINS="ops")
        refused = self.sign_in(client, "ops", "shared-pw")
        self.assertEqual((refused.status_code, refused.json()["code"]), (401, "wrong_credentials"))
        self.assertEqual(client.get("/api/v1/admin/users").status_code, 401)

    def test_the_shared_password_signs_in_only_as_the_demo_identity(self):
        client = self.portal(TWINLAB2_PASSWORD="shared-pw")
        for name in ("ana", "alice", "local"):
            self.assertEqual(self.sign_in(client, name, "shared-pw").status_code, 401, name)
        signed = self.sign_in(client, "demo", "shared-pw")
        self.assertEqual(signed.status_code, 200, signed.text)
        self.assertEqual((signed.json()["user"], signed.json()["is_admin"], signed.json()["access_mode"]),
                         ("demo", False, "individual"))
        self.assertEqual(client.get("/api/v1/admin/users").status_code, 403)

    def test_a_named_user_needs_their_own_password(self):
        self.users(("alice", "alice-pw", False))
        client = self.portal(TWINLAB2_PASSWORD="shared-pw", TWINLAB2_SHARED_USER="alice")
        # alice has her own password: the shared one never signs her in, even named as the demo identity
        self.assertEqual(self.sign_in(client, "alice", "shared-pw").status_code, 401)
        self.assertEqual(self.sign_in(client, "alice", "alice-pw").json()["user"], "alice")

    def test_a_shared_identity_that_is_an_admin_refuses_to_start(self):
        with self.assertRaises(auth.ConfigError):
            self.portal(TWINLAB2_PASSWORD="shared-pw", TWINLAB2_SHARED_USER="ops", TWINLAB2_ADMINS="ops")


class LoopbackTest(Portal):
    def test_loopback_is_not_admin_when_users_have_passwords(self):
        """Reproduced: a request from 127.0.0.1 was the admin `local` although erin had a password."""
        self.users(("erin", "erin-pw", False))
        client = self.portal()
        self.assertEqual(client.get("/api/v1/admin/users").status_code, 401)
        self.assertEqual(client.get("/api/v1/apps").status_code, 401)
        session = client.get("/api/v1/session").json()
        self.assertEqual((session["signed_in"], session["access_mode"]), (False, "individual"))

    def test_local_dev_serves_only_this_machine(self):
        client = self.portal(TWINLAB2_AUTH_MODE="local-dev")
        session = client.get("/api/v1/session").json()
        self.assertEqual((session["user"], session["is_admin"], session["access_mode"], session["login_available"]),
                         ("local", True, "local-dev", False))
        other = self.portal(remote="10.0.0.5", TWINLAB2_AUTH_MODE="local-dev")
        self.assertEqual(other.get("/api/v1/apps").status_code, 403)
        self.assertEqual(other.post("/api/v1/session", json={"user": "local", "password": "x"}).status_code, 404)

    def test_local_dev_with_password_users_refuses_to_start_unless_forced(self):
        self.users(("erin", "erin-pw", False))
        with self.assertRaises(auth.ConfigError):
            self.portal(TWINLAB2_AUTH_MODE="local-dev")
        forced = self.portal(TWINLAB2_AUTH_MODE="local-dev", TWINLAB2_LOCAL_DEV_FORCE="1")
        self.assertEqual(forced.get("/api/v1/session").json()["user"], "local")

    def test_individual_mode_has_no_loopback_admin_and_ignores_proxy_headers(self):
        client = self.portal(TWINLAB2_AUTH_MODE="individual")
        self.assertEqual(client.get("/api/v1/apps").status_code, 401)
        self.assertEqual(client.as_user("root").get("/api/v1/apps").status_code, 401)

    def test_an_unknown_mode_refuses_to_start(self):
        with self.assertRaises(auth.ConfigError):
            self.portal(TWINLAB2_AUTH_MODE="open")

    def test_the_start_logs_the_mode(self):
        with self.assertLogs("twinlab2", logging.INFO) as logs:
            self.portal(TWINLAB2_AUTH_MODE="individual")
        self.assertTrue(any("auth mode individual" in line for line in logs.output), logs.output)


class TrustedProxyTest(Portal):
    def test_proxy_headers_count_only_from_the_proxy(self):
        """Reproduced: any peer that could reach the portal could name itself with X-Forwarded-User."""
        direct = self.portal(remote="10.0.0.9", TWINLAB2_AUTH_MODE="trusted-proxy", TWINLAB2_ADMINS="root")
        refused = direct.as_user("root").get("/api/v1/admin/users")
        self.assertEqual(refused.status_code, 403, refused.text)
        proxied = self.portal(TWINLAB2_AUTH_MODE="trusted-proxy", TWINLAB2_ADMINS="root")
        self.assertEqual(proxied.as_user("root").get("/api/v1/admin/users").status_code, 200)
        session = proxied.as_user("ana").get("/api/v1/session").json()
        self.assertEqual((session["user"], session["access_mode"], session["auth_mode"]),
                         ("ana", "trusted-proxy", "proxy"))
        # no loopback admin and no sign-in form behind the proxy
        self.assertEqual(proxied.get("/api/v1/apps").status_code, 401)
        self.assertEqual(proxied.post("/api/v1/session", json={"user": "a", "password": "b"}).status_code, 404)

    def test_trusted_proxies_are_configurable(self):
        client = self.portal(remote="10.1.2.3", TWINLAB2_AUTH_MODE="trusted-proxy",
                             TWINLAB2_TRUSTED_PROXIES="10.1.0.0/16")
        self.assertEqual(client.as_user("ana").get("/api/v1/apps").status_code, 200)

    def test_the_older_variable_derives_trusted_proxy(self):
        client = self.portal(remote="10.0.0.9", TWINLAB2_TRUST_PROXY_USER="1")
        self.assertEqual(client.as_user("ana").get("/api/v1/apps").status_code, 403)


# -- 2: the body limit -------------------------------------------------------------------------


def echo_app(limit):
    app = FastAPI()

    @app.post("/echo")
    async def echo(request: Request):
        return {"received": len(await request.body())}

    rest.install_errors(app)
    app.add_middleware(rest.BodyLimit, limit=limit)
    return TestClient(app)


class BodyLimitTest(unittest.TestCase):
    def test_a_declared_body_over_the_limit_is_413(self):
        response = echo_app(100).post("/echo", content=b"x" * 101)
        self.assertEqual(response.status_code, 413)
        self.assertEqual(echo_app(100).post("/echo", content=b"x" * 100).json(), {"received": 100})

    def test_a_chunked_body_over_the_limit_is_413(self):
        """Reproduced: without Content-Length the body was not limited at all."""
        def chunks():
            for _ in range(5):
                yield b"x" * 60

        response = echo_app(100).post("/echo", content=chunks())
        self.assertEqual(response.status_code, 413, response.text)

    def test_a_chunked_body_under_the_limit_passes(self):
        response = echo_app(100).post("/echo", content=iter([b"x" * 40, b"y" * 40]))
        self.assertEqual(response.json(), {"received": 80})

    def test_an_api_body_over_the_limit_is_a_problem(self):
        app = FastAPI()

        @app.post("/api/v1/thing")
        async def thing(request: Request):
            await request.body()
            return {}

        rest.install_errors(app)
        app.add_middleware(rest.BodyLimit, limit=10)
        response = TestClient(app).post("/api/v1/thing", content=iter([b"x" * 8, b"x" * 8]))
        self.assertEqual((response.status_code, response.headers["content-type"]), (413, "application/problem+json"))


# -- 3: the secret, sessions, streamed downloads -------------------------------------------------


class SecretTest(Portal):
    def test_outside_local_dev_a_missing_secret_refuses_to_start(self):
        """Reproduced: the portal made a per-process key (sessions died with it, replicas disagreed)."""
        os.environ.pop("TWINLAB2_SECRET_KEY")
        for env in ({"TWINLAB2_AUTH_MODE": "individual"}, {"TWINLAB2_AUTH_MODE": "trusted-proxy"},
                    {"TWINLAB2_PASSWORD": "shared-pw"}):
            with self.subTest(env=env), self.assertRaises(auth.ConfigError):
                self.portal(**env)
        self.portal(TWINLAB2_AUTH_MODE="local-dev")  # this machine only: a per-process key is fine


class SessionTest(Portal):
    def setUp(self):
        super().setUp()
        self.users(("erin", "erin-pw", False), ("root", "root-pw", True))
        self.client = self.portal(TWINLAB2_AUTH_MODE="individual")
        self.assertEqual(self.sign_in(self.client, "erin", "erin-pw").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/apps").status_code, 200)

    def copy(self):
        """Another browser holding a copy of the session cookie."""
        other = Client(self.client.app)
        other.http.cookies.set("session", self.client.http.cookies.get("session"), domain="localhost.local")
        return other

    def test_the_cookie_expires(self):
        cookie = next(c for c in self.client.http.cookies.jar if c.name == "session")
        self.assertIsNotNone(cookie.expires, "the session cookie has no expiry")
        with closing(connect(self.database)) as db, db:
            db.execute("UPDATE auth_sessions SET expires_at = '2000-01-01T00:00:00.000000Z'")
        self.assertEqual(self.client.get("/api/v1/apps").status_code, 401)

    def test_sign_out_revokes_a_copy_of_the_cookie(self):
        """Reproduced: the signed cookie still signed in after sign-out (nothing on the server ended it)."""
        copy = self.copy()
        self.assertEqual(copy.get("/api/v1/apps").status_code, 200)
        self.assertEqual(self.client.delete("/api/v1/session").status_code, 204)
        self.assertEqual(copy.get("/api/v1/apps").status_code, 401)

    def test_a_password_change_revokes_the_users_sessions(self):
        copy = self.copy()
        admin = self.portal(TWINLAB2_AUTH_MODE="individual")
        self.assertEqual(self.sign_in(admin, "root", "root-pw").status_code, 200)
        changed = admin.put("/api/v1/admin/users/erin", json={"is_admin": False, "password": "new-pw"})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(copy.get("/api/v1/apps").status_code, 401)
        self.assertEqual(self.client.get("/api/v1/apps").status_code, 401)
        self.assertEqual(admin.get("/api/v1/apps").status_code, 200)  # root's own session is untouched

    def test_session_hours_are_configurable(self):
        client = self.portal(TWINLAB2_AUTH_MODE="individual", TWINLAB2_SESSION_HOURS="1")
        self.sign_in(client, "erin", "erin-pw")
        with closing(connect(self.database)) as db:
            row = db.execute("SELECT created_at, expires_at FROM auth_sessions ORDER BY created_at DESC").fetchone()
        self.assertEqual(row["expires_at"][:13], _plus_hours(row["created_at"], 1)[:13])
        with self.assertRaises(auth.ConfigError):
            self.portal(TWINLAB2_SESSION_HOURS="0")


def _plus_hours(stamp, hours):
    from datetime import datetime, timedelta

    return (datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%fZ") + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class ReadSpy(io.BytesIO):
    """A stored object that records how much each read asked for."""

    def __init__(self, data, asked):
        super().__init__(data)
        self.asked = asked

    def read(self, size=-1):
        self.asked.append(size)
        return super().read(size)


class StreamedDownloadTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.store = artifacts.LocalStore(self.work.name)
        self.data = os.urandom(3 * 1024 * 1024 + 17)
        self.ref = artifacts.put(self.data, kind="release_download", app_id=1, content_type="application/zip",
                                 target=self.store)
        self.asked = []
        original = self.store.open
        self.store.open = lambda ref: ReadSpy(original(ref).read(), self.asked)

    def test_stream_reads_in_chunks_and_checks_the_digest(self):
        chunks = list(artifacts.stream(self.ref, self.store, chunk=1024 * 1024))
        self.assertEqual(b"".join(chunks), self.data)
        self.assertGreater(len(chunks), 3)
        self.assertNotIn(-1, self.asked)
        bad = artifacts.ArtifactRef(**{**self.ref.as_dict(), "sha256": "0" * 64})
        with self.assertRaises(artifacts.ChecksumMismatch):
            list(artifacts.stream(bad, self.store))

    def test_the_signed_link_streams(self):
        """Reproduced: /artifacts/<token> read the whole object into memory first."""
        with mock.patch.dict(os.environ, {"TWINLAB2_SECRET_KEY": "test-only", "TWINLAB2_AUTH_MODE": "local-dev"}):
            database = new_database(self.work.name)
            app = create_app(database, store=self.store)
        bound = artifacts.bind(artifacts.Binding(self.store, "test-only", lambda token: f"/artifacts/{token}"))
        try:
            url = artifacts.presign_get(self.ref, 60, filename="demo.zip")
        finally:
            artifacts.unbind(bound)
        response = TestClient(app, base_url="http://localhost").get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, self.data)
        self.assertEqual(response.headers["content-length"], str(len(self.data)))
        self.assertNotIn(-1, self.asked, "the download read the whole object at once")


# -- 4 and 5: pinned release manifests, the scope of "Tested" ------------------------------------


class ReleaseScopeTest(unittest.TestCase):
    setUp = ReleaseStatesTest.setUp
    git = ReleaseStatesTest.git
    evidence = ReleaseStatesTest.evidence
    record = ReleaseStatesTest.record
    detail = ReleaseStatesTest.detail

    def test_the_release_manifest_is_pinned_to_the_verified_commit(self):
        """Reproduced: the generated BuildConfig built `main` and the Deployment followed `:latest`."""
        with closing(connect(self.database)) as db, db:
            db.execute("UPDATE apps SET target_platform = 'web', git_url = 'https://github.com/example/billing.git'")
        response = self.client.get("/api/v1/releases/1/openshift.yaml")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(self.commit, response.text.splitlines()[1])
        items = {item["kind"]: item for item in yaml.safe_load(response.text)["items"]}
        build = items["BuildConfig"]["spec"]
        self.assertEqual(build["source"]["git"]["ref"], self.commit)
        self.assertEqual(build["output"]["to"]["name"].split(":")[-1], self.commit)
        deployment = items["Deployment"]
        image = deployment["spec"]["template"]["spec"]["containers"][0]["image"]
        self.assertIn("@sha256:", image)
        self.assertNotIn("latest", response.text.replace("# Nothing here follows a branch or :latest.", ""))
        self.assertNotIn("image.openshift.io/triggers", deployment["metadata"].get("annotations", {}))
        digest = "sha256:" + "ab" * 32
        pinned = self.client.get(f"/api/v1/releases/1/openshift.yaml?image_digest={digest}")
        image = next(i for i in yaml.safe_load(pinned.text)["items"] if i["kind"] == "Deployment")[
            "spec"]["template"]["spec"]["containers"][0]["image"]
        self.assertTrue(image.endswith("@" + digest), image)
        self.assertEqual(self.client.get("/api/v1/releases/1/openshift.yaml?image_digest=latest").status_code, 400)

    def test_the_development_manifest_says_what_it_is(self):
        readme = demo_target.openshift_readme({"id": 1, "name": "Billing"}, "sqlite", "")
        self.assertIn("development manifest", readme)

    def test_the_release_names_what_was_verified(self):
        """Reproduced: a Tested release said who tested it, not what the evidence covered."""
        recorded = self.record()
        self.assertEqual(recorded.status_code, 200, recorded.text)
        scope = self.detail()["test"]["scope"]
        self.assertEqual((scope["commit_sha"], scope["platform"], scope["tested_by"], scope["role"], scope["method"]),
                         (self.commit, "Windows 11, Python 3.13", "local", "owner", "manual"))
        self.assertEqual((scope["scenarios_total"], scope["scenarios_run"], scope["scenarios_passed"]), (1, 1, 1))
        self.assertIsNone(scope["deployment_url"])
        self.assertIn(self.commit[:12], scope["label"])
        self.assertIn("1 of 1 scenario passed", scope["label"])
        self.assertIn("(owner)", scope["label"])


if __name__ == "__main__":
    unittest.main()
