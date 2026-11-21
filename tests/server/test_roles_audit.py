"""Server-enforced roles, the audit history, immutable builder packages and redaction."""

from collections import Counter
from contextlib import closing
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock
import zlib
from zipfile import ZipFile

from fastapi.testclient import TestClient

from twinlab2 import rest
from twinlab2.portal import create_app
from twinlab2 import redact
from test_registry import eid
from twinlab2.db import Error as database_errors
from support import Client, connect, new_database, preview

RECORDING_LOG = b'{"seq":1,"action":"submit","url":"http://billing.local/?customer=jane@corp.com"}\n' \
                b'{"seq":2,"action":"click","url":"http://billing.local/"}\n'
SECRET = "hunter2-SECRET"
EMAIL = "jane.doe@corp-mail.com"
CARD = "4111 1111 1111 1111"


def png(text_chunk=b"Author\x00Jane Doe jane.doe@corp-mail.com"):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (redact.PNG_SIGNATURE + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
            + chunk(b"tEXt", text_chunk) + chunk(b"IDAT", zlib.compress(b"\x00\x00")) + chunk(b"IEND", b""))


def har():
    return json.dumps({"log": {"version": "1.2", "entries": [{
        "request": {"method": "POST", "url": f"https://billing.example.test/api/login?user={EMAIL}&ref=7",
                    "headers": [{"name": "Cookie", "value": f"sid={SECRET}"},
                                {"name": "Authorization", "value": f"Bearer {SECRET}abcdefgh"},
                                {"name": "Accept", "value": "application/json"}],
                    "cookies": [{"name": "sid", "value": SECRET}],
                    "queryString": [{"name": "user", "value": EMAIL}],
                    "postData": {"mimeType": "application/json",
                                 "text": json.dumps({"username": "jane", "password": SECRET})}},
        "response": {"status": 200, "headers": [{"name": "Set-Cookie", "value": f"sid={SECRET}; HttpOnly"}],
                     "cookies": [{"name": "sid", "value": SECRET}],
                     "content": {"mimeType": "application/json", "text": json.dumps({
                         "customer": {"name": "Jane", "email": EMAIL, "card": CARD, "note": f"call +44 20 7946 0958"},
                         "orders": [{"id": 1001, "total": 12.5}]})}}},
        {"request": {"method": "GET", "url": "https://billing.example.test/logo.png", "headers": []},
         "response": {"status": 200, "headers": [],
                      "content": {"mimeType": "image/png", "encoding": "base64", "text": "iVBORw0KGgo="}}}]}}).encode()


class Base(unittest.TestCase):
    """The portal behind an SSO proxy: people are who X-Forwarded-User says; `root` is an admin."""

    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.database = new_database(self.work.name)
        with mock.patch.dict(os.environ, {"TWINLAB2_TRUST_PROXY_USER": "1", "TWINLAB2_ADMINS": "root"}):
            self.app = create_app(self.database)
        self.client = Client(self.app)
        self.assertEqual(self.get("/api/v1/apps", "alice").status_code, 200)

    def as_(self, who):
        return self.client.as_user(who)

    def get(self, path, who):
        return self.as_(who).get(path)

    def post(self, path, who, **values):
        return self.as_(who).post(path, json=values)

    def rows(self, sql, *args):
        with closing(connect(self.database)) as db:
            return db.execute(sql, args).fetchall()

    def actions(self, app_id):
        return [r[0] for r in self.rows("SELECT action FROM audit_log WHERE app_id = ? ORDER BY id", app_id)]

    def make_app(self, name="Billing", user="alice"):
        response = self.post("/api/v1/apps", user, name=name)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def import_recording(self, name, user="alice"):
        response = self.as_(user).post("/api/v1/recordings", data={"name": name},
                                       files={"log": ("steps.jsonl", RECORDING_LOG, "application/x-ndjson")})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def scenario(self, app_id, user="alice", **values):
        response = self.post(f"/api/v1/apps/{app_id}/scenarios", user, **values)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def attach(self, app_id, title, kind, name, content, share=None, user="alice"):
        data = {"title": title, "kind": kind}
        if share:
            data["share"] = share
        response = self.as_(user).post(f"/api/v1/apps/{app_id}/attachments", data=data,
                                       files={"file": (name, content, "application/octet-stream")})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def draft(self, app_id, user="alice"):
        response = self.get(f"/api/v1/apps/{app_id}/package-draft", user)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def as_owner_freeze(self, app_id, user="alice", summary=""):
        draft, extra = preview(self.as_(user), app_id)
        return self.as_(user).post(f"/api/v1/apps/{app_id}/packages",
                                   json={"approve": True, "sha256": draft.json()["sha256"], "summary": summary, **extra})


class EveryRouteHasAPolicyTest(Base):
    def test_every_endpoint_is_marked(self):
        unmarked = [route.path for route in rest.all_routes(self.app)
                    if getattr(route, "endpoint", None) is not None and rest.policy_of(route) is None
                    and route.path not in ("/openapi.json",)]
        self.assertEqual(unmarked, [])

    def test_a_route_without_a_policy_is_refused(self):
        with mock.patch.dict(os.environ, {"TWINLAB2_TRUST_PROXY_USER": "1", "TWINLAB2_ADMINS": "root"}):
            fresh = create_app(Path(self.work.name) / "other.db")
        # ahead of the portal's fallbacks, as a route of the portal's own route class
        fresh.router.routes.insert(0, rest.PolicyRoute("/api/v1/unmarked", lambda: {"open": True}, methods=["GET"]))
        response = TestClient(fresh, base_url="http://localhost").get(
            "/api/v1/unmarked", headers={"X-Forwarded-User": "root"})
        self.assertEqual((response.status_code, response.json()["code"]), (403, "role"))

    def test_without_the_proxy_header_nobody_is_signed_in(self):
        response = self.client.get("/api/v1/apps")
        self.assertEqual((response.status_code, response.json()["code"]), (401, "signed_out"))
        self.assertEqual(self.client.get("/healthz").status_code, 200)


class RoleRefusalTest(Base):
    """Every sensitive operation refuses (403) a user below its role and admits the role."""

    def setUp(self):
        super().setUp()
        self.app_id = self.make_app()  # alice owns it
        self.other = self.make_app("Other")
        for user, role in (("bob", "builder"), ("carol", "viewer")):
            response = self.as_("alice").put(f"/api/v1/apps/{self.app_id}/roles/{user}", json={"role": role})
            self.assertEqual(response.status_code, 200, response.text)
        self.recording = self.import_recording("Invoice run")
        self.event = eid(self.recording, 1)
        assigned = self.as_("alice").put(f"/api/v1/recordings/{self.recording}/assignments/{self.app_id}",
                                         json={"events": [self.event]})
        self.assertEqual(assigned.status_code, 200, assigned.text)
        self.scenario_id = self.scenario(self.app_id, name="Save an invoice",
                                         events=[f"{self.recording}#{self.event}"])
        self.attachment = self.attach(self.app_id, "Contract", "API", "contract.json", b'{"endpoint": "/invoice"}')
        self.assertEqual(self.as_owner_freeze(self.app_id).status_code, 201)
        revision = self.rows("SELECT revision FROM builder_packages WHERE app_id = ?", self.app_id)[0][0]
        self.revision = revision
        with closing(connect(self.database)) as db, db:
            db.execute("""INSERT INTO releases(id, app_id, version, revision, commit_sha, archive, archive_sha, state)
                          VALUES (1, ?, '1.0', ?, ?, ?, ?, 'tested')""",
                       (self.app_id, revision, "a" * 40, b"zip", hashlib.sha256(b"zip").hexdigest()))
            db.execute("INSERT INTO deployments(release_id, environment, url, username, secret_ref) "
                       "VALUES (1, 'Demo', 'http://127.0.0.1:9/', '', '')")
            db.execute("""INSERT INTO builds(id, app_id, revision, pack_sha256, repo, base_branch, base_sha, attempt,
                          change_summary, acu_budget, state, idempotency_tag, requested_by, created_at, updated_at)
                          VALUES (1, ?, ?, '', 'octo/billing', 'main', '', 1, '', 5, 'failed', 'tag-1', 'bob', '', '')""",
                       (self.app_id, revision))

    def routes(self):
        a, r, s, t = self.app_id, self.recording, self.scenario_id, self.attachment
        v = "/api/v1"
        return [
            ("viewer", "GET", f"{v}/apps/{a}", None),
            ("viewer", "GET", f"{v}/releases/1", None),
            ("builder", "GET", f"{v}/apps/{a}/build-pack", None),
            ("builder", "GET", f"{v}/apps/{a}/packages/1", None),
            ("builder", "GET", f"{v}/apps/{a}/packages/1/download", None),
            ("builder", "GET", f"{v}/apps/{a}/briefs/{self.revision}/download", None),
            ("builder", "GET", f"{v}/releases/1/download", None),
            ("builder", "POST", f"{v}/releases/1/deployments/check", {"environment": "Demo"}),
            ("builder", "GET", f"{v}/releases/1/reports/1", None),
            ("builder", "GET", f"{v}/apps/{a}/builds", None),
            ("builder", "POST", f"{v}/apps/{a}/builds", {}),
            ("builder", "GET", f"{v}/builds/1", None),
            ("builder", "POST", f"{v}/builds/1/messages", {"answer": "", "request_key": "k"}),
            ("builder*", "POST", f"{v}/builds/1/cancel", None),
            ("builder*", "POST", f"{v}/builds/1/retry", {"request_key": "k-retry"}),
            ("builder*", "POST", f"{v}/builds/1/interrupt", None),
            ("builder", "POST", f"{v}/builds/1/tool-decisions/1", {"decision": "approve", "request_key": "k"}),
            ("builder", "GET", f"{v}/apps/{a}/builds/compare", None),
            ("owner", "POST", f"{v}/apps/{a}/builds/compare", {"sessions": []}),
            ("owner", "POST", f"{v}/releases/1/checks/refresh", None),
            ("owner*", "POST", f"{v}/releases/1/verify", None),  # * refusals only: admitting it runs Twin Lab
            ("owner", "POST", f"{v}/apps/{a}/verify-commit", {"repo": "octo/billing", "sha": "not-a-sha"}),
            ("owner", "PUT", f"{v}/apps/{a}/verification-settings", {"required_checks": "", "target_platform": "web"}),
            ("owner", "GET", f"{v}/recordings/{r}", None),
            ("owner", "GET", f"{v}/recordings/{r}/assignments/{a}", None),
            ("owner", "PUT", f"{v}/recordings/{r}/assignments/{a}", {"events": [self.event]}),
            ("owner", "POST", f"{v}/recordings/{r}/completeness/accept", None),
            ("owner", "GET", f"/api/recordings/{r}/upload", None),
            ("owner", "GET", f"{v}/apps/{a}/setup", None),
            ("owner", "GET", f"{v}/apps/{a}/history", None),
            ("owner", "GET", f"{v}/apps/{a}/evidence", None),
            ("owner", "GET", f"{v}/apps/{a}/scenarios", None),
            ("owner", "GET", f"{v}/apps/{a}/package-draft", None),
            ("owner", "POST", f"{v}/apps/{a}/packages", {"approve": True, "sha256": "0" * 64}),
            ("owner", "PATCH", f"{v}/apps/{a}", {"purpose": "Demo"}),
            ("owner", "PUT", f"{v}/apps/{a}/roles/zed", {"role": "viewer"}),
            ("owner", "POST", f"{v}/apps/{a}/scenarios", {"name": "Another"}),
            ("owner", "PUT", f"{v}/apps/{a}/scenarios/{s}", {"name": "Save an invoice", "events": [f"{r}#{self.event}"]}),
            ("owner", "PATCH", f"{v}/apps/{a}/attachments/{t}", {"share": "reviewed"}),
            ("owner", "POST", f"{v}/apps/{a}/identities", {"key": "Billing.exe"}),
            ("owner", "PATCH", f"{v}/apps/{a}", {"status": "known"}),
            ("owner", "PUT", f"{v}/apps/{a}/url-match", {"url_match": "path"}),
            ("owner", "GET", f"{v}/apps/{a}/merge-plan?into={self.other}", None),
            ("owner", "POST", f"{v}/apps/{a}/merge", {"into": self.other, "confirm": "0" * 16}),
            ("owner", "PUT", f"{v}/releases/1/deployments", {"environment": "Demo", "url": "http://127.0.0.1:9/"}),
            ("owner", "POST", f"{v}/releases/1/tests", {"commit": "a" * 40}),
            ("owner", "POST", f"{v}/automations", {"name": "Bot", "apps": [a]}),
            ("owner", "DELETE", f"{v}/apps/{a}/attachments/{t}", None),
            ("owner", "DELETE", f"{v}/apps/{a}/scenarios/{s}", None),
            ("owner", "POST", f"{v}/releases/1/publish", None),
        ]

    def call(self, method, path, user, body):
        return self.as_(user).request(method, path, json=body)

    def test_each_role_is_refused_below_and_admitted_at_its_level(self):
        people = {"dave": 0, "carol": 1, "bob": 2, "alice": 3}
        rank = {"viewer": 1, "builder": 2, "owner": 3}
        for needed, method, path, body in self.routes():
            needed = needed.rstrip("*")
            for user, level in people.items():
                if level < rank[needed]:
                    response = self.call(method, path, user, body)
                    self.assertEqual(response.status_code, 403, f"{user} {method} {path}")
        for needed, method, path, body in self.routes():
            if needed.endswith("*"):
                continue
            user = {"viewer": "carol", "builder": "bob", "owner": "alice"}[needed]
            response = self.call(method, path, user, body)
            self.assertNotEqual(response.status_code, 403, f"{user} {method} {path}: {response.text[:300]}")

    def test_a_published_release_downloads_for_viewers(self):
        self.assertEqual(self.get("/api/v1/releases/1/download", "carol").status_code, 403)
        published = self.post("/api/v1/releases/1/publish", "alice")
        self.assertEqual(published.status_code, 200, published.text)
        self.assertEqual(published.json()["release"]["state"], "published")
        self.assertEqual(self.get("/api/v1/releases/1/download", "carol").status_code, 200)
        self.assertEqual(self.get("/api/v1/releases/1/download", "dave").status_code, 403)

    def test_viewers_see_no_restricted_evidence(self):
        page = self.get(f"/api/v1/apps/{self.app_id}", "carol")
        self.assertEqual(page.status_code, 200)
        for text in ("Invoice run", "Contract", "Save an invoice"):
            self.assertNotIn(text, page.text)
        for path in (f"/api/v1/apps/{self.app_id}/evidence", f"/api/v1/apps/{self.app_id}/scenarios"):
            self.assertEqual(self.get(path, "carol").status_code, 403, path)
        self.assertNotIn("Invoice run", self.get("/api/v1/recordings", "carol").text)
        self.assertNotIn("Billing", self.get("/api/v1/apps", "dave").text)
        self.assertIn("Billing", [a["name"] for a in self.get("/api/v1/apps", "carol").json()["apps"]])
        evidence = self.get(f"/api/v1/apps/{self.app_id}/evidence", "alice").json()
        self.assertIn("Invoice run", [link["name"] for link in evidence["links"]])
        self.assertIn("Contract", [a["title"] for a in evidence["attachments"]])
        self.assertIn("Invoice run", self.get("/api/v1/recordings", "alice").text)

    def test_the_admin_manages_roles_but_does_not_read_evidence(self):
        self.assertEqual(self.get(f"/api/v1/apps/{self.app_id}", "root").status_code, 200)
        self.assertEqual(self.get("/api/v1/admin/users", "root").status_code, 200)
        self.assertEqual(self.get("/api/v1/admin/users", "alice").status_code, 403)
        self.assertEqual(self.get("/api/v1/setup/builds", "alice").status_code, 403)
        self.assertEqual(self.get("/api/v1/setup/builds", "root").status_code, 200)
        alice = self.as_("alice")
        for method, path, body in (("PUT", "/api/v1/setup/builds/settings", {"settings": {}}),
                                   ("POST", "/api/v1/setup/builds/check", None),
                                   ("POST", "/api/v1/setup/builds/repos", {"repo": "a/b"}),
                                   ("DELETE", "/api/v1/setup/builds/repos?repo=a/b", None),
                                   ("POST", "/api/v1/setup/builds/worker", None),
                                   ("PUT", "/api/v1/admin/users/x", {"is_admin": False})):
            self.assertEqual(alice.request(method, path, json=body).status_code, 403, path)
        self.assertEqual(self.get(f"/api/v1/apps/{self.app_id}/history", "root").status_code, 200)
        for path in (f"/api/v1/recordings/{self.recording}", f"/api/v1/apps/{self.app_id}/package-draft",
                     f"/api/v1/apps/{self.app_id}/build-pack", f"/api/v1/apps/{self.app_id}/evidence"):
            self.assertEqual(self.get(path, "root").status_code, 403, path)
        made = self.as_("root").put(f"/api/v1/apps/{self.app_id}/roles/root", json={"role": "owner"})
        self.assertEqual(made.status_code, 200, made.text)
        self.assertEqual(self.get(f"/api/v1/recordings/{self.recording}", "root").status_code, 200)
        self.assertIn("role.assign", self.actions(self.app_id))

    def test_an_application_keeps_an_owner(self):
        alice = self.as_("alice")
        self.assertEqual(alice.delete(f"/api/v1/apps/{self.app_id}/roles/alice").status_code, 400)
        self.assertEqual(alice.put(f"/api/v1/apps/{self.app_id}/roles/alice", json={"role": "viewer"}).status_code, 400)

    def test_machines_and_sessions_belong_to_their_user(self):
        code = self.post("/api/v1/machines/pairing-codes", "dave")
        self.assertEqual(code.status_code, 201)
        self.assertTrue(code.json()["code"])
        self.assertNotIn(code.json()["code"], self.get("/api/v1/machines", "dave").text)  # shown once
        with closing(connect(self.database)) as db, db:
            db.execute("INSERT INTO machines(id, name, token_sha, paired_by, paired_at) VALUES (9, 'PC', 'x', 'dave', '')")
            db.execute("INSERT INTO app_identities VALUES ('exe:billing.exe', ?, 'desktop', '{}')", (self.app_id,))
        self.assertEqual(self.post("/api/v1/machines/9/revoke", "carol").status_code, 403)
        # dave may not request a recording of an application he does not own
        response = self.post("/api/v1/capture-sessions", "dave", name="Run", machine_id=9,
                             targets=[{"app_id": self.app_id, "level": "full"}])
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.post("/api/v1/machines/9/revoke", "dave").status_code, 200)


class AuditTest(Base):
    def test_actions_are_written_and_cannot_be_changed(self):
        app_id = self.make_app()
        other = self.make_app("Duplicate")
        recording = self.import_recording("Run")
        assigned = self.as_("alice").put(f"/api/v1/recordings/{recording}/assignments/{app_id}",
                                         json={"events": [eid(recording, 1)], "notes": "Saves the invoice"})
        self.assertEqual(assigned.status_code, 200, assigned.text)
        self.scenario(app_id, name="Save")
        shot = self.attach(app_id, "Shot", "Screen", "shot.png", png())
        self.assertEqual(self.as_("alice").patch(f"/api/v1/apps/{app_id}/attachments/{shot}",
                                                 json={"share": "reviewed"}).status_code, 200)
        self.assertEqual(self.as_("alice").put(f"/api/v1/apps/{app_id}/roles/bob", json={"role": "builder"}).status_code, 200)
        self.assertEqual(self.as_owner_freeze(app_id).status_code, 201)
        self.assertEqual(self.get(f"/api/v1/apps/{app_id}/build-pack", "bob").status_code, 200)
        revision = self.rows("SELECT revision FROM builder_packages")[0][0]
        with closing(connect(self.database)) as db, db:
            db.execute("""INSERT INTO releases(id, app_id, version, revision, commit_sha, archive, archive_sha, state)
                          VALUES (1, ?, '1.0', ?, ?, ?, '', 'tested')""", (app_id, revision, "b" * 40, b"\x00"))
        self.assertEqual(self.post("/api/v1/releases/1/publish", "alice").status_code, 200)
        # a merge is written to both applications' history
        plan = self.get(f"/api/v1/apps/{other}/merge-plan?into={app_id}", "alice").json()
        merged = self.post(f"/api/v1/apps/{other}/merge", "alice", into=app_id, confirm=plan["fingerprint"])
        self.assertEqual(merged.status_code, 200, merged.text)
        actions = self.actions(app_id)
        for action in ("app.create", "role.assign", "evidence.review", "evidence.upload", "evidence.share",
                       "package.freeze", "package.download", "release.publish", "app.merge"):
            self.assertIn(action, actions)
        self.assertIn("app.merge", self.actions(other))
        who = dict(self.rows("SELECT action, actor FROM audit_log WHERE app_id = ? AND action = 'package.download'", app_id))
        self.assertEqual(who["package.download"], "bob")
        freeze_row = self.rows("SELECT details FROM audit_log WHERE action = 'package.freeze'")[0][0]
        self.assertIn(hashlib.sha256(self.get(f"/api/v1/apps/{app_id}/build-pack", "alice").content).hexdigest(),
                      freeze_row)
        history = self.get(f"/api/v1/apps/{app_id}/history", "alice").json()
        listed = [e["action"] for e in history["entries"]]
        self.assertIn("package.freeze", listed)
        self.assertIn("evidence.review", listed)
        self.assertEqual(self.get(f"/api/v1/apps/{app_id}/history", "bob").status_code, 403)
        with closing(connect(self.database)) as db:
            for sql in ("UPDATE audit_log SET actor = 'mallory'", "DELETE FROM audit_log"):
                with self.assertRaises(database_errors):
                    db.execute(sql)
                db.rollback()


class PackageTest(Base):
    def setUp(self):
        super().setUp()
        self.app_id = self.make_app()
        self.scenario_id = self.scenario(self.app_id, name="Save an invoice", expected="Saved.")

    def test_freezing_needs_the_owners_approval_of_the_previewed_bytes(self):
        self.assertEqual(self.get(f"/api/v1/apps/{self.app_id}/build-pack", "alice").status_code, 400)
        draft, extra = preview(self.as_("alice"), self.app_id)  # no recording: frozen over its gaps
        sha = draft.json()["sha256"]
        path = f"/api/v1/apps/{self.app_id}/packages"
        self.assertEqual(self.post(path, "alice", sha256=sha, **extra).status_code, 400)  # not approved
        self.assertEqual(self.post(path, "alice", approve=False, sha256=sha, **extra).status_code, 400)
        stale = self.post(path, "alice", approve=True, sha256="0" * 64, **extra)
        self.assertEqual((stale.status_code, stale.json()["code"]), (409, "draft_changed"))
        self.assertEqual(self.post(path, "alice", approve=True, sha256=sha, **extra).status_code, 201)
        again = self.post(path, "alice", approve=True, sha256=sha, **extra)
        self.assertEqual((again.status_code, again.json()["code"]), (409, "unchanged"))
        row = self.rows("SELECT rev, approved_by, sha256 FROM builder_packages")
        self.assertEqual(row, [(1, "alice", sha)])
        data = self.get(f"/api/v1/apps/{self.app_id}/packages/1/download", "alice").content
        self.assertEqual(hashlib.sha256(data).hexdigest(), sha)
        self.assertEqual(self.get(f"/api/v1/apps/{self.app_id}/packages/1", "alice").json()["approved_by"], "alice")

    def test_a_change_makes_a_new_revision_with_a_diff_and_the_old_one_stays(self):
        self.assertEqual(self.as_owner_freeze(self.app_id).status_code, 201)
        first = self.get(f"/api/v1/apps/{self.app_id}/packages/1/download", "alice").content
        changed = self.as_("alice").put(f"/api/v1/apps/{self.app_id}/scenarios/{self.scenario_id}",
                                        json={"name": "Save an invoice", "expected": "Saved with a number."})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.scenario(self.app_id, name="Reject an empty invoice")
        self.assertEqual(self.as_owner_freeze(self.app_id, summary="Clarified the result").status_code, 201)
        rev, summary, diff = self.rows("SELECT rev, summary, diff FROM builder_packages WHERE rev = 2")[0]
        self.assertIn("Clarified the result", summary)
        self.assertIn("Scenario changed: Save an invoice", summary)
        self.assertIn("Scenario added: Reject an empty invoice", summary)
        self.assertIn("+", diff)
        self.assertIn("Saved with a number.", diff)
        self.assertEqual(self.get(f"/api/v1/apps/{self.app_id}/packages/1/download", "alice").content, first)
        self.assertNotEqual(self.get(f"/api/v1/apps/{self.app_id}/build-pack", "alice").content, first)
        detail = self.get(f"/api/v1/apps/{self.app_id}/packages/2", "alice").json()
        self.assertEqual((detail["rev"], detail["diff"], detail["summary"]), (2, diff, summary))
        with closing(connect(self.database)) as db:
            for sql in ("UPDATE builder_packages SET approved_by = 'mallory'", "DELETE FROM builder_packages"):
                with self.assertRaises(database_errors):
                    db.execute(sql)
                db.rollback()
        # bytes swapped under a frozen revision are refused, not served
        first_rev, second_rev = (r[0] for r in self.rows("SELECT revision FROM builder_packages ORDER BY rev"))
        with closing(connect(self.database)) as db, db:
            other = db.execute("SELECT artifact_id FROM brief_packages WHERE revision = ?", (second_rev,)).fetchone()[0]
            db.execute("UPDATE brief_packages SET artifact_id = ? WHERE revision = ?", (other, first_rev))
        self.assertEqual(self.get(f"/api/v1/apps/{self.app_id}/packages/1/download", "alice").status_code, 500)


class RedactionTest(Base):
    def setUp(self):
        super().setUp()
        self.app_id = self.make_app()
        self.scenario(self.app_id, name="Log in",
                      notes=f"Customer {EMAIL} paid with {CARD}; ssn 078-05-1120; synthetic: demo@example.com")

    def upload(self, title, kind, name, content, share=None):
        return self.attach(self.app_id, title, kind, name, content, share=share)

    def package(self):
        self.assertIn(self.as_owner_freeze(self.app_id).status_code, (201, 409))
        response = self.get(f"/api/v1/apps/{self.app_id}/build-pack", "alice")
        with ZipFile(io.BytesIO(response.content)) as archive:
            return {name: archive.read(name) for name in archive.namelist()}

    def excluded(self, draft):
        return [entry for entry in draft["report"] if entry["outcome"] == "excluded"]

    def test_uploads_are_restricted_until_marked_and_the_report_says_why(self):
        self.upload("Login screen", "Screen", "login.png", png())
        self.upload("Traffic", "API", "login.har", har())
        draft = self.draft(self.app_id)
        self.assertIn("2 items excluded", draft["summary"])
        reasons = [reason for entry in self.excluded(draft) for reason in entry["reasons"]]
        self.assertEqual(len(self.excluded(draft)), 2)
        self.assertIn("restricted evidence: the owner has not marked this screenshot reviewed or synthetic", reasons)
        self.assertIn("restricted evidence: the owner has not marked this attachment reviewed or synthetic", reasons)
        files = self.package()
        self.assertEqual([p for p in files if p.startswith("attachments/")], [])

    def test_har_bodies_headers_and_screenshots_are_redacted_once_shared(self):
        self.upload("Login screen", "Screen", "login.png", png(), share="reviewed")
        self.upload("Traffic", "API", "login.har", har(), share="reviewed")
        self.upload("Customer export", "Database", "customers.json",
                    json.dumps({"rows": [{"id": 1, "ssn": "078-05-1120", "Email": EMAIL, "city": "Leeds"}]}).encode(),
                    share="synthetic")
        self.upload("Broken capture", "API", "broken.har", b"{not json", share="reviewed")
        draft = self.draft(self.app_id)
        self.assertIn("1 item excluded", draft["summary"])
        self.assertEqual(len(self.excluded(draft)), 1)
        self.assertTrue(any("could not be checked for sensitive data" in reason
                            for reason in self.excluded(draft)[0]["reasons"]))
        files = self.package()
        blob = b"\n".join(files.values())
        for secret in (SECRET.encode(), EMAIL.encode(), b"078-05-1120", CARD.encode(), b"Jane Doe",
                       b"iVBORw0KGgo=", b"7946 0958"):
            self.assertNotIn(secret, blob, secret)
        self.assertIn(b"demo@example.com", files["evidence.json"])  # synthetic example domain stays
        traffic = json.loads(files["attachments/2.har"])
        entry = traffic["log"]["entries"][0]
        self.assertEqual(entry["request"]["cookies"], [])
        self.assertEqual({h["name"]: h["value"] for h in entry["request"]["headers"]}["Accept"], "application/json")
        self.assertEqual(json.loads(entry["response"]["content"]["text"])["orders"], [{"id": 1001, "total": 12.5}])
        self.assertNotIn("text", traffic["log"]["entries"][1]["response"]["content"])
        image = files["attachments/1.png"]
        self.assertTrue(image.startswith(redact.PNG_SIGNATURE))
        self.assertNotIn(b"tEXt", image)
        self.assertIn(b"IDAT", image)
        export = json.loads(files["attachments/3.json"])
        self.assertEqual(export["rows"][0]["city"], "Leeds")
        manifest = json.loads(files["evidence.json"])
        self.assertEqual({a["share"] for a in manifest["attachments"]}, {"reviewed", "synthetic"})
        report = json.loads(self.rows("SELECT report FROM builder_packages")[0][0])
        outcomes = Counter(item["outcome"] for item in report)
        self.assertEqual(outcomes["excluded"], 1)
        self.assertGreaterEqual(outcomes["redacted"], 4)  # png metadata, har, json, scenario text

    def test_text_rules(self):
        found = Counter()
        text = redact.redact_text(
            f"mail {EMAIL} card {CARD} not-a-card 4111 1111 1111 1112 token eyJhbGciOi.eyJzdWIiOi.c2lnbmF0dXJl "
            f"password={SECRET} key AKIAABCDEFGHIJKLMNOP iban GB82 WEST 1234 5698 7654 32 "
            "https://x.test/a?q=1 example user@example.com", found)
        for secret in (EMAIL, CARD, SECRET, "AKIAABCDEFGHIJKLMNOP", "GB82 WEST", "eyJhbGciOi", "q=1"):
            self.assertNotIn(secret, text)
        self.assertIn("4111 1111 1111 1112", text)  # fails the Luhn check
        self.assertIn("user@example.com", text)
        self.assertIn("password=[REDACTED:secret]", text)
        self.assertTrue({"email", "card number", "token", "secret", "api key", "iban", "query value"} <= set(found))
        self.assertEqual(redact.redact_json({"user": {"passwordHash": "x", "displayName": "Ann"}}),
                         {"user": {"passwordHash": "[REDACTED:field]", "displayName": "Ann"}})

    def test_jpeg_metadata_is_removed(self):
        exif = b"\xff\xe1" + struct.pack(">H", 2 + 12) + b"Exif\x00\x00GPS123"
        jpeg = b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 2 + 5) + b"JFIF\x00" + exif + b"\xff\xda\x00\x02scan\xff\xd9"
        stripped = redact.strip_jpeg(jpeg, Counter())
        self.assertNotIn(b"GPS123", stripped)
        self.assertIn(b"JFIF", stripped)
        self.assertTrue(stripped.endswith(b"scan\xff\xd9"))


class Rows(list):
    def fetchall(self):
        return list(self)

    def fetchone(self):
        return self[0] if self else None


class SignInTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.database = new_database(self.work.name)

    def query(self, sql):
        with closing(connect(self.database)) as db:
            return Rows(db.execute(sql).fetchall())

    def client(self, env=None):
        with mock.patch.dict(os.environ, env or {}, clear=False):
            for name in ("TWINLAB2_TRUST_PROXY_USER", "TWINLAB2_PASSWORD", "TWINLAB2_ADMINS"):
                if name not in (env or {}):
                    os.environ.pop(name, None)
            app = create_app(self.database)
        client = Client(app)
        client.get("/api/v1/session")
        return client

    def test_local_users_sign_in_with_their_own_password(self):
        client = self.client()
        # the machine user is the portal admin when the portal serves only this machine (local-dev)
        self.assertEqual(client.post("/api/v1/apps", json={"name": "Orders"}).status_code, 201)
        made = client.put("/api/v1/admin/users/erin", json={"is_admin": False, "password": "s3cret-pass"})
        self.assertEqual(made.status_code, 200, made.text)
        # once a user has a password the mode is individual: this machine is no longer the admin
        self.assertEqual(client.get("/api/v1/session").json()["access_mode"], "individual")
        self.assertEqual(client.post("/api/v1/apps", json={"name": "Other"}).status_code, 401)
        wrong = client.post("/api/v1/session", json={"user": "erin", "password": "nope"})
        self.assertEqual((wrong.status_code, wrong.json()["code"]), (401, "wrong_credentials"))
        signed = client.post("/api/v1/session", json={"user": "erin", "password": "s3cret-pass"})
        self.assertEqual(signed.status_code, 200, signed.text)
        self.assertEqual(client.get("/api/v1/session").json()["user"], "erin")
        self.assertEqual(client.get("/api/v1/admin/users").status_code, 403)
        self.assertEqual(client.get("/api/v1/apps/1").status_code, 403)  # no role on Orders
        stored = self.query("SELECT password_hash FROM users").fetchone()[0]
        self.assertNotIn("s3cret-pass", stored)
        self.assertTrue(stored.startswith("pbkdf2_sha256$"))

    def test_the_owner_text_of_an_old_catalog_becomes_an_assignment(self):
        with closing(connect(self.database)) as db, db:
            db.executescript("""
                CREATE TABLE apps (id INTEGER PRIMARY KEY, name TEXT NOT NULL, git_url TEXT NOT NULL,
                                   requirements TEXT NOT NULL, owner TEXT NOT NULL DEFAULT '');
                INSERT INTO apps VALUES (1, 'Orders', '', '', 'Ops team');
                INSERT INTO apps VALUES (2, 'Billing', '', '', '');
            """)
        self.client({"TWINLAB2_PASSWORD": "shared"})
        rows = self.query("SELECT app_id, user_name, role FROM app_roles").fetchall()
        self.assertEqual(rows, [(1, "Ops team", "owner")])  # Billing waits for an admin
        actor = self.query("SELECT actor, action FROM audit_log").fetchall()
        self.assertEqual(actor, [("migration", "role.assign")])

    def test_a_named_owner_on_a_new_application_is_assigned(self):
        client = self.client()
        self.assertEqual(client.post("/api/v1/apps", json={"name": "Orders", "owner": "Ops team"}).status_code, 201)
        rows = self.query("SELECT user_name, role FROM app_roles ORDER BY user_name").fetchall()
        self.assertEqual(rows, [("Ops team", "owner"), ("local", "owner")])


if __name__ == "__main__":
    unittest.main()
