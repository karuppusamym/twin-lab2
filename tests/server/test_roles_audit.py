"""Server-enforced roles, the audit history, immutable builder packages and redaction."""

from collections import Counter
from contextlib import closing
import hashlib
import io
import json
import os
from pathlib import Path
import re
import struct
import tempfile
import unittest
from unittest import mock
import zlib
from zipfile import ZipFile

from twinlab2 import auth
from twinlab2.portal import create_app
from twinlab2 import redact
from test_registry import eid
from twinlab2.db import Error as database_errors
from support import connect, new_database

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
        self.app.testing = True
        self.client = self.app.test_client()
        self.assertEqual(self.get("/", "alice").status_code, 200)
        with self.client.session_transaction() as session:
            self.token = session["form_token"]

    def get(self, path, who):
        return self.client.get(path, headers={"X-Forwarded-User": who})

    def post(self, path, who, **values):
        return self.client.post(path, data={"token": self.token, **values}, headers={"X-Forwarded-User": who})

    def rows(self, sql, *args):
        with closing(connect(self.database)) as db:
            return db.execute(sql, args).fetchall()

    def actions(self, app_id):
        return [r[0] for r in self.rows("SELECT action FROM audit_log WHERE app_id = ? ORDER BY id", app_id)]

    def make_app(self, name="Billing", user="alice"):
        location = self.post("/apps", user, name=name).location
        return int(location.rsplit("/", 1)[1])

    def as_owner_freeze(self, app_id, user="alice", summary=""):
        headers = {"X-Forwarded-User": user}
        page = self.client.get(f"/apps/{app_id}/build-pack/preview", headers=headers)
        sha = re.search(r'id="archive-sha256">([0-9a-f]{64})<', page.data.decode()).group(1)
        return self.client.post(f"/apps/{app_id}/packages", headers=headers, data={
            "token": self.token, "approve": "yes", "sha256": sha, "summary": summary})


class EveryRouteHasAPolicyTest(Base):
    def test_every_endpoint_is_marked(self):
        unmarked = [rule.rule for rule in self.app.url_map.iter_rules()
                    if rule.endpoint != "static" and auth.policy_of(self.app.view_functions[rule.endpoint]) is None]
        self.assertEqual(unmarked, [])

    def test_a_route_without_a_policy_is_refused(self):
        with mock.patch.dict(os.environ, {"TWINLAB2_TRUST_PROXY_USER": "1", "TWINLAB2_ADMINS": "root"}):
            fresh = create_app(Path(self.work.name) / "other.db")
        fresh.add_url_rule("/unmarked", "unmarked", lambda: "open")
        response = fresh.test_client().get("/unmarked", headers={"X-Forwarded-User": "root"})
        self.assertEqual(response.status_code, 403)

    def test_without_the_proxy_header_nobody_is_signed_in(self):
        self.assertEqual(self.client.get("/").status_code, 401)
        self.assertEqual(self.client.get("/healthz").status_code, 200)


class RoleRefusalTest(Base):
    """Every sensitive route refuses (403) a user below its role and admits the role."""

    def setUp(self):
        super().setUp()
        self.app_id = self.make_app()  # alice owns it
        self.other = self.make_app("Other")
        for user, role in (("bob", "builder"), ("carol", "viewer")):
            self.assertEqual(self.post(f"/apps/{self.app_id}/roles", "alice", user=user, role=role).status_code, 302)
        rec = self.client.post("/recordings", headers={"X-Forwarded-User": "alice"}, data={
            "token": self.token, "name": "Invoice run", "log": (io.BytesIO(RECORDING_LOG), "steps.jsonl")})
        self.recording = rec.location.rsplit("/", 1)[1]
        self.event = eid(self.recording, 1)
        self.assertEqual(self.post(rec.location, "alice", app_id=str(self.app_id), event=self.event).status_code, 302)
        self.post(f"/apps/{self.app_id}/scenarios", "alice", name="Save an invoice", event=f"{self.recording}#{self.event}")
        upload = self.client.post(f"/apps/{self.app_id}/attachments", headers={"X-Forwarded-User": "alice"}, data={
            "token": self.token, "title": "Contract", "kind": "API",
            "file": (io.BytesIO(b'{"endpoint": "/invoice"}'), "contract.json")})
        self.assertEqual(upload.status_code, 302)
        self.assertEqual(self.as_owner_freeze(self.app_id).status_code, 302)
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
        a, r = self.app_id, self.recording
        return [
            ("viewer", "GET", f"/apps/{a}", {}),
            ("viewer", "GET", "/releases/1", {}),
            ("builder", "GET", f"/apps/{a}/build-pack", {}),
            ("builder", "GET", f"/apps/{a}/packages/1", {}),
            ("builder", "GET", f"/apps/{a}/packages/1/download", {}),
            ("builder", "GET", f"/apps/{a}/briefs/{self.revision}", {}),
            ("builder", "GET", "/releases/1/download", {}),
            ("builder", "POST", "/releases/1/deployments/check", {"environment": "Demo"}),
            ("builder", "GET", "/releases/1/reports/1", {}),
            ("builder", "GET", f"/apps/{a}/build", {}),
            ("builder", "POST", f"/apps/{a}/build", {}),
            ("builder", "GET", "/builds/1", {}),
            ("builder", "POST", "/builds/1/answer", {"answer": ""}),
            ("builder*", "POST", "/builds/1/cancel", {}),
            ("builder*", "POST", "/builds/1/retry", {}),
            ("builder*", "POST", "/builds/1/interrupt", {}),
            ("builder", "POST", "/builds/1/tools/1", {"decision": "approve"}),
            ("builder", "GET", f"/apps/{a}/sessions/compare", {}),
            ("owner", "POST", f"/apps/{a}/sessions/compare", {}),
            ("owner", "POST", f"/apps/{a}/build/freeze", {}),
            ("owner", "POST", "/releases/1/checks", {}),
            ("owner*", "POST", "/releases/1/verify", {}),  # * refusals only: admitting it runs Twin Lab
            ("owner", "POST", f"/apps/{a}/verify-commit", {"repo": "octo/billing", "sha": "not-a-sha"}),
            ("owner", "POST", f"/apps/{a}/verification", {"target_platform": "web"}),
            ("owner", "GET", f"/recordings/{r}", {}),
            ("owner", "POST", f"/recordings/{r}", {"app_id": str(a), "event": self.event}),
            ("owner", "POST", f"/recordings/{r}/completeness/accept", {}),
            ("owner", "GET", f"/api/recordings/{r}/upload", {}),
            ("owner", "GET", f"/apps/{a}/setup", {}),
            ("owner", "GET", f"/apps/{a}/history", {}),
            ("owner", "GET", f"/apps/{a}/build-pack/preview", {}),
            ("owner", "POST", f"/apps/{a}/packages", {"approve": "yes", "sha256": "0" * 64}),
            ("owner", "POST", f"/apps/{a}", {"purpose": "Demo"}),
            ("owner", "POST", f"/apps/{a}/roles", {"user": "zed", "role": "viewer"}),
            ("owner", "POST", f"/apps/{a}/scenarios", {"name": "Another"}),
            ("owner", "POST", f"/apps/{a}/scenarios/1", {"name": "Save an invoice", "event": f"{r}#{self.event}"}),
            ("owner", "POST", f"/apps/{a}/attachments/1/share", {"share": "reviewed"}),
            ("owner", "POST", f"/apps/{a}/identity", {"key": "Billing.exe"}),
            ("owner", "POST", f"/apps/{a}/status", {"status": "known"}),
            ("owner", "POST", f"/apps/{a}/url-match", {"url_match": "path"}),
            ("owner", "GET", f"/apps/{a}/merge?into={self.other}", {}),
            ("owner", "POST", f"/apps/{a}/merge", {"into": str(self.other)}),
            ("owner", "POST", "/releases/1", {"environment": "Demo", "url": "http://127.0.0.1:9/"}),
            ("owner", "POST", "/releases/1/test", {"commit": "a" * 40}),
            ("owner", "POST", "/automations", {"name": "Bot", "app": str(a)}),
            ("owner", "POST", f"/apps/{a}/attachments/1/remove", {}),
            ("owner", "POST", f"/apps/{a}/scenarios/1/remove", {}),
            ("owner", "POST", "/releases/1/publish", {}),
        ]

    def call(self, method, path, user, values):
        return self.get(path, user) if method == "GET" else self.post(path, user, **values)

    def test_each_role_is_refused_below_and_admitted_at_its_level(self):
        people = {"dave": 0, "carol": 1, "bob": 2, "alice": 3}
        rank = {"viewer": 1, "builder": 2, "owner": 3}
        for needed, method, path, values in self.routes():
            needed = needed.rstrip("*")
            for user, level in people.items():
                if level < rank[needed]:
                    response = self.call(method, path, user, values)
                    self.assertEqual(response.status_code, 403, f"{user} {method} {path}")
        for needed, method, path, values in self.routes():
            if needed.endswith("*"):
                continue
            user = {"viewer": "carol", "builder": "bob", "owner": "alice"}[needed]
            response = self.call(method, path, user, values)
            self.assertNotEqual(response.status_code, 403, f"{user} {method} {path}: {response.data[:300]}")

    def test_a_published_release_downloads_for_viewers(self):
        self.assertEqual(self.get("/releases/1/download", "carol").status_code, 403)
        self.assertEqual(self.post("/releases/1/publish", "alice").status_code, 302)
        self.assertEqual(self.get("/releases/1/download", "carol").status_code, 200)
        self.assertEqual(self.get("/releases/1/download", "dave").status_code, 403)

    def test_viewers_see_no_restricted_evidence(self):
        page = self.get(f"/apps/{self.app_id}", "carol").data.decode()
        self.assertNotIn("Invoice run", page)
        self.assertNotIn("Contract", page)
        self.assertNotIn("Save an invoice", page)
        self.assertNotIn("Invoice run", self.get("/recordings", "carol").data.decode())
        self.assertNotIn("Billing", self.get("/", "dave").data.decode())
        self.assertIn("Billing", self.get("/", "carol").data.decode())
        owner_page = self.get(f"/apps/{self.app_id}", "alice").data.decode()
        self.assertIn("Invoice run", owner_page)

    def test_the_admin_manages_roles_but_does_not_read_evidence(self):
        self.assertEqual(self.get(f"/apps/{self.app_id}", "root").status_code, 200)
        self.assertEqual(self.get("/admin/users", "root").status_code, 200)
        self.assertEqual(self.get("/admin/users", "alice").status_code, 403)
        for path in ("/setup/builds",):
            self.assertEqual(self.get(path, "alice").status_code, 403)
            self.assertEqual(self.get(path, "root").status_code, 200)
        for path in ("/setup/builds/settings", "/setup/builds/check", "/setup/builds/repos",
                     "/setup/builds/repos/remove", "/setup/builds/worker"):
            self.assertEqual(self.post(path, "alice").status_code, 403, path)
        self.assertEqual(self.post("/admin/users", "alice", name="x").status_code, 403)
        self.assertEqual(self.get(f"/apps/{self.app_id}/history", "root").status_code, 200)
        for path in (f"/recordings/{self.recording}", f"/apps/{self.app_id}/build-pack/preview",
                     f"/apps/{self.app_id}/build-pack"):
            self.assertEqual(self.get(path, "root").status_code, 403, path)
        self.assertEqual(self.post(f"/apps/{self.app_id}/roles", "root", user="root", role="owner").status_code, 302)
        self.assertEqual(self.get(f"/recordings/{self.recording}", "root").status_code, 200)
        self.assertIn("role.assign", self.actions(self.app_id))

    def test_an_application_keeps_an_owner(self):
        response = self.post(f"/apps/{self.app_id}/roles", "alice", user="alice", remove="yes")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.post(f"/apps/{self.app_id}/roles", "alice", user="alice", role="viewer").status_code, 400)

    def test_machines_and_sessions_belong_to_their_user(self):
        self.post("/machines/pair", "dave")
        self.assertIn("pair-code", self.get("/machines", "dave").data.decode())
        code = re.search(r'id="pair-code">([A-Z0-9-]+)<', self.get("/machines", "dave").data.decode())
        self.assertIsNone(code)  # shown once
        with closing(connect(self.database)) as db, db:
            db.execute("INSERT INTO machines(id, name, token_sha, paired_by, paired_at) VALUES (9, 'PC', 'x', 'dave', '')")
            db.execute("INSERT INTO app_identities VALUES ('exe:billing.exe', ?, 'desktop', '{}')", (self.app_id,))
        self.assertEqual(self.post("/machines/9/revoke", "carol").status_code, 403)
        # dave may not request a recording of an application he does not own
        response = self.post("/sessions", "dave", name="Run", machine_id="9", app=str(self.app_id))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.post("/machines/9/revoke", "dave").status_code, 302)


class AuditTest(Base):
    def test_actions_are_written_and_cannot_be_changed(self):
        app_id = self.make_app()
        other = self.make_app("Duplicate")
        rec = self.client.post("/recordings", headers={"X-Forwarded-User": "alice"}, data={
            "token": self.token, "name": "Run", "log": (io.BytesIO(RECORDING_LOG), "steps.jsonl")})
        self.post(rec.location, "alice", app_id=str(app_id), event=eid(rec.location.rsplit("/", 1)[1], 1),
                  notes="Saves the invoice")
        self.post(f"/apps/{app_id}/scenarios", "alice", name="Save")
        self.client.post(f"/apps/{app_id}/attachments", headers={"X-Forwarded-User": "alice"}, data={
            "token": self.token, "title": "Shot", "kind": "Screen", "file": (io.BytesIO(png()), "shot.png")})
        self.post(f"/apps/{app_id}/attachments/1/share", "alice", share="reviewed")
        self.post(f"/apps/{app_id}/roles", "alice", user="bob", role="builder")
        self.assertEqual(self.as_owner_freeze(app_id).status_code, 302)
        self.get(f"/apps/{app_id}/build-pack", "bob")
        revision = self.rows("SELECT revision FROM builder_packages")[0][0]
        with closing(connect(self.database)) as db, db:
            db.execute("""INSERT INTO releases(id, app_id, version, revision, commit_sha, archive, archive_sha, state)
                          VALUES (1, ?, '1.0', ?, ?, ?, '', 'tested')""", (app_id, revision, "b" * 40, b"\x00"))
        self.assertEqual(self.post("/releases/1/publish", "alice").status_code, 302)
        # a merge is written to both applications' history
        merge = self.get(f"/apps/{other}/merge?into={app_id}", "alice").data.decode()
        fingerprint = re.search(r'name="confirm" value="([0-9a-f]+)"', merge).group(1)
        self.assertEqual(self.post(f"/apps/{other}/merge", "alice", into=str(app_id), confirm=fingerprint).status_code, 302)
        actions = self.actions(app_id)
        for action in ("app.create", "role.assign", "evidence.review", "evidence.upload", "evidence.share",
                       "package.freeze", "package.download", "release.publish", "app.merge"):
            self.assertIn(action, actions)
        self.assertIn("app.merge", self.actions(other))
        who = dict(self.rows("SELECT action, actor FROM audit_log WHERE app_id = ? AND action = 'package.download'", app_id))
        self.assertEqual(who["package.download"], "bob")
        freeze_row = self.rows("SELECT details FROM audit_log WHERE action = 'package.freeze'")[0][0]
        self.assertIn(hashlib.sha256(self.get(f"/apps/{app_id}/build-pack", "alice").data).hexdigest(), freeze_row)
        page = self.get(f"/apps/{app_id}/history", "alice").data.decode()
        self.assertIn("package.freeze", page)
        self.assertIn("evidence.review", page)
        self.assertEqual(self.get(f"/apps/{app_id}/history", "bob").status_code, 403)
        with closing(connect(self.database)) as db:
            for sql in ("UPDATE audit_log SET actor = 'mallory'", "DELETE FROM audit_log"):
                with self.assertRaises(database_errors):
                    db.execute(sql)
                db.rollback()


class PackageTest(Base):
    def setUp(self):
        super().setUp()
        self.app_id = self.make_app()
        self.post(f"/apps/{self.app_id}/scenarios", "alice", name="Save an invoice", expected="Saved.")

    def test_freezing_needs_the_owners_approval_of_the_previewed_bytes(self):
        self.assertEqual(self.get(f"/apps/{self.app_id}/build-pack", "alice").status_code, 400)
        page = self.get(f"/apps/{self.app_id}/build-pack/preview", "alice").data.decode()
        sha = re.search(r'id="archive-sha256">([0-9a-f]{64})<', page).group(1)
        path = f"/apps/{self.app_id}/packages"
        self.assertEqual(self.post(path, "alice", sha256=sha).status_code, 400)  # not approved
        self.assertEqual(self.post(path, "alice", approve="yes", sha256="0" * 64).status_code, 409)
        self.assertEqual(self.post(path, "alice", approve="yes", sha256=sha).status_code, 302)
        self.assertEqual(self.post(path, "alice", approve="yes", sha256=sha).status_code, 409)  # unchanged
        row = self.rows("SELECT rev, approved_by, sha256 FROM builder_packages")
        self.assertEqual(row, [(1, "alice", sha)])
        data = self.get(f"/apps/{self.app_id}/packages/1/download", "alice").data
        self.assertEqual(hashlib.sha256(data).hexdigest(), sha)
        self.assertIn("alice", self.get(f"/apps/{self.app_id}/packages/1", "alice").data.decode())

    def test_a_change_makes_a_new_revision_with_a_diff_and_the_old_one_stays(self):
        self.assertEqual(self.as_owner_freeze(self.app_id).status_code, 302)
        first = self.get(f"/apps/{self.app_id}/packages/1/download", "alice").data
        self.post(f"/apps/{self.app_id}/scenarios/1", "alice", name="Save an invoice", expected="Saved with a number.")
        self.post(f"/apps/{self.app_id}/scenarios", "alice", name="Reject an empty invoice")
        self.assertEqual(self.as_owner_freeze(self.app_id, summary="Clarified the result").status_code, 302)
        rev, summary, diff = self.rows("SELECT rev, summary, diff FROM builder_packages WHERE rev = 2")[0]
        self.assertIn("Clarified the result", summary)
        self.assertIn("Scenario changed: Save an invoice", summary)
        self.assertIn("Scenario added: Reject an empty invoice", summary)
        self.assertIn("+", diff)
        self.assertIn("Saved with a number.", diff)
        self.assertEqual(self.get(f"/apps/{self.app_id}/packages/1/download", "alice").data, first)
        self.assertNotEqual(self.get(f"/apps/{self.app_id}/build-pack", "alice").data, first)
        page = self.get(f"/apps/{self.app_id}/packages/2", "alice").data.decode()
        self.assertIn("Diff against revision 1", page)
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
        self.assertEqual(self.get(f"/apps/{self.app_id}/packages/1/download", "alice").status_code, 500)


class RedactionTest(Base):
    def setUp(self):
        super().setUp()
        self.app_id = self.make_app()
        self.post(f"/apps/{self.app_id}/scenarios", "alice", name="Log in",
                  notes=f"Customer {EMAIL} paid with {CARD}; ssn 078-05-1120; synthetic: demo@example.com")

    def upload(self, title, kind, name, content, share=None):
        data = {"token": self.token, "title": title, "kind": kind, "file": (io.BytesIO(content), name)}
        if share:
            data["share"] = share
        response = self.client.post(f"/apps/{self.app_id}/attachments", headers={"X-Forwarded-User": "alice"},
                                    data=data)
        self.assertEqual(response.status_code, 302, response.data)

    def package(self):
        self.assertIn(self.as_owner_freeze(self.app_id).status_code, (302, 409))
        response = self.get(f"/apps/{self.app_id}/build-pack", "alice")
        with ZipFile(io.BytesIO(response.data)) as archive:
            return {name: archive.read(name) for name in archive.namelist()}

    def test_uploads_are_restricted_until_marked_and_the_report_says_why(self):
        self.upload("Login screen", "Screen", "login.png", png())
        self.upload("Traffic", "API", "login.har", har())
        page = self.get(f"/apps/{self.app_id}/build-pack/preview", "alice").data.decode()
        self.assertIn("2 items excluded", page)
        self.assertIn("restricted evidence: the owner has not marked this screenshot reviewed or synthetic", page)
        self.assertIn("restricted evidence: the owner has not marked this attachment reviewed or synthetic", page)
        files = self.package()
        self.assertEqual([p for p in files if p.startswith("attachments/")], [])

    def test_har_bodies_headers_and_screenshots_are_redacted_once_shared(self):
        self.upload("Login screen", "Screen", "login.png", png(), share="reviewed")
        self.upload("Traffic", "API", "login.har", har(), share="reviewed")
        self.upload("Customer export", "Database", "customers.json",
                    json.dumps({"rows": [{"id": 1, "ssn": "078-05-1120", "Email": EMAIL, "city": "Leeds"}]}).encode(),
                    share="synthetic")
        self.upload("Broken capture", "API", "broken.har", b"{not json", share="reviewed")
        page = self.get(f"/apps/{self.app_id}/build-pack/preview", "alice").data.decode()
        self.assertIn("1 item excluded", page)
        self.assertIn("could not be checked for sensitive data", page)
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
        app.testing = True
        client = app.test_client()
        client.get("/")
        with client.session_transaction() as session:
            token = session.get("form_token")
        return client, token

    def test_local_users_sign_in_with_their_own_password(self):
        client, token = self.client()
        # the machine user is the portal admin when the portal serves only this machine
        self.assertEqual(client.post("/admin/users", data={"token": token, "name": "erin", "password": "s3cret-pass"}).status_code, 302)
        self.assertEqual(client.post("/apps", data={"token": token, "name": "Orders"}).status_code, 302)
        wrong = client.post("/login", data={"token": token, "user": "erin", "password": "nope"})
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(client.post("/login", data={"token": token, "user": "erin", "password": "s3cret-pass"}).status_code, 302)
        self.assertIn("erin", client.get("/").data.decode())
        self.assertEqual(client.get("/admin/users").status_code, 403)
        self.assertEqual(client.get("/apps/1").status_code, 403)  # no role on Orders
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
        client, token = self.client({"TWINLAB2_PASSWORD": "shared"})
        rows = self.query("SELECT app_id, user_name, role FROM app_roles").fetchall()
        self.assertEqual(rows, [(1, "Ops team", "owner")])  # Billing waits for an admin
        actor = self.query("SELECT actor, action FROM audit_log").fetchall()
        self.assertEqual(actor, [("migration", "role.assign")])

    def test_a_named_owner_on_a_new_application_is_assigned(self):
        client, token = self.client()
        client.post("/apps", data={"token": token, "name": "Orders", "owner": "Ops team"})
        rows = self.query("SELECT user_name, role FROM app_roles ORDER BY user_name").fetchall()
        self.assertEqual(rows, [("Ops team", "owner"), ("local", "owner")])


if __name__ == "__main__":
    unittest.main()
