"""Access requests (routes_access), the CSRF token's rotation, the bind-address check and the
agent API's scopes and rate limit."""

from contextlib import closing
import json
import os
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from twinlab2 import auth
from twinlab2.portal import create_app
from support import Client, connect, new_database


class Base(unittest.TestCase):
    """Behind the SSO proxy: people are who X-Forwarded-User says; `root` is an admin."""

    env = {"TWINLAB2_TRUST_PROXY_USER": "1", "TWINLAB2_ADMINS": "root"}

    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.database = new_database(self.work.name)
        with mock.patch.dict(os.environ, self.env):
            self.app = create_app(self.database)
        self.client = Client(self.app)

    def as_(self, who):
        return self.client.as_user(who)

    def rows(self, sql, *args):
        with closing(connect(self.database)) as db:
            return db.execute(sql, args).fetchall()

    def role(self, app_id, user):
        with closing(connect(self.database)) as db:
            return auth.role_of(db, app_id, user)

    def make_app(self, name="Billing", user="alice"):
        response = self.as_(user).post("/api/v1/apps", json={"name": name})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]


class AccessRequestTest(Base):
    def test_refused_request_approve_and_audit(self):
        app_id = self.make_app()
        bob = self.as_("bob")
        refused = bob.get(f"/api/v1/apps/{app_id}")
        self.assertEqual(refused.status_code, 403)
        problem = refused.json()
        self.assertEqual((problem["code"], problem["app_id"], problem["requested_role"]), ("role", app_id, "viewer"))
        self.assertNotIn("owners", problem)  # bob holds no role: whom to ask, not who owns it

        missing = bob.post(f"/api/v1/apps/{app_id}/access-requests", json={"role": "builder", "reason": "  "})
        self.assertEqual(missing.status_code, 400)
        sent = bob.post(f"/api/v1/apps/{app_id}/access-requests", json={"role": "builder", "reason": "Demo week."})
        self.assertEqual(sent.status_code, 202, sent.text)
        again = bob.post(f"/api/v1/apps/{app_id}/access-requests", json={"role": "viewer", "reason": "Only to look."})
        self.assertEqual(again.status_code, 202)
        # one pending request, with the latest role and reason
        self.assertEqual([tuple(r) for r in self.rows("SELECT role, reason, state FROM access_requests")],
                         [("viewer", "Only to look.", "pending")])
        mine = bob.get("/api/v1/access-requests").json()
        self.assertEqual(mine["to_decide"], [])
        self.assertEqual((mine["mine"][0]["state"], mine["mine"][0]["app_name"]), ("pending", None))

        listed = self.as_("alice").get("/api/v1/access-requests").json()
        self.assertEqual([(r["user"], r["app_name"], r["role"]) for r in listed["to_decide"]],
                         [("bob", "Billing", "viewer")])
        request_id = listed["to_decide"][0]["id"]
        self.assertEqual(self.as_("carol").post(f"/api/v1/access-requests/{request_id}/decision",
                                                json={"decision": "approve"}).status_code, 404)
        self.assertEqual(bob.post(f"/api/v1/access-requests/{request_id}/decision",
                                  json={"decision": "approve"}).status_code, 403)  # not his own
        approved = self.as_("alice").post(f"/api/v1/access-requests/{request_id}/decision",
                                          json={"decision": "approve", "role": "builder", "note": "Welcome."})
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual((approved.json()["state"], approved.json()["role"], approved.json()["decided_by"]),
                         ("approved", "builder", "alice"))
        self.assertEqual(bob.get(f"/api/v1/apps/{app_id}").status_code, 200)
        self.assertEqual(self.role(app_id, "bob"), "builder")
        outcome = bob.get("/api/v1/access-requests").json()["mine"][0]
        self.assertEqual((outcome["state"], outcome["app_name"], outcome["note"]), ("approved", "Billing", "Welcome."))
        twice = self.as_("alice").post(f"/api/v1/access-requests/{request_id}/decision", json={"decision": "decline"})
        self.assertEqual((twice.status_code, twice.json()["code"]), (409, "state_conflict"))
        actions = [r[0] for r in self.rows("SELECT action FROM audit_log WHERE app_id = ? ORDER BY id", app_id)]
        self.assertEqual(actions[-4:], ["access.request", "access.request", "role.assign", "access.approve"])
        details = json.loads(self.rows("SELECT details FROM audit_log WHERE action = 'access.approve'")[0][0])
        self.assertEqual((details["user"], details["role"], details["requested"]), ("bob", "builder", "viewer"))

        # a builder refused an owner page learns who the owners are
        problem = bob.get(f"/api/v1/apps/{app_id}/package-draft").json()
        self.assertEqual((problem["owners"], problem["requested_role"]), (["alice"], "owner"))
        held = bob.post(f"/api/v1/apps/{app_id}/access-requests", json={"role": "builder", "reason": "x"})
        self.assertEqual(held.status_code, 400)

    def test_decline_and_admin_decides(self):
        app_id = self.make_app()
        self.as_("dave").post(f"/api/v1/apps/{app_id}/access-requests", json={"role": "owner", "reason": "Take over."})
        listed = self.as_("root").get("/api/v1/access-requests").json()["to_decide"]
        self.assertEqual([r["user"] for r in listed], ["dave"])
        declined = self.as_("root").post(f"/api/v1/access-requests/{listed[0]['id']}/decision",
                                         json={"decision": "decline", "note": "Ask Alice first."})
        self.assertEqual(declined.json()["state"], "declined")
        self.assertIsNone(self.role(app_id, "dave"))
        outcome = self.as_("dave").get("/api/v1/access-requests").json()["mine"][0]
        self.assertEqual((outcome["state"], outcome["app_name"], outcome["decided_by"]), ("declined", None, "root"))
        self.assertIn("access.decline", [r[0] for r in self.rows("SELECT action FROM audit_log")])

    def test_unknown_app_answers_the_same(self):
        self.make_app()
        known = self.as_("bob").post("/api/v1/apps/1/access-requests", json={"role": "viewer", "reason": "r"})
        unknown = self.as_("bob").post("/api/v1/apps/99999/access-requests", json={"role": "viewer", "reason": "r"})
        self.assertEqual((known.status_code, known.json()), (unknown.status_code, unknown.json()))
        self.assertEqual(self.as_("bob").get("/api/v1/apps/99999").json()["app_id"], 99999)
        self.assertEqual(len(self.rows("SELECT id FROM access_requests")), 1)


class CsrfRotationTest(unittest.TestCase):
    """Individual mode: the form token changes at sign-in and at sign-out."""

    def test_rotates(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        database = new_database(work.name)
        with mock.patch.dict(os.environ, {"TWINLAB2_AUTH_MODE": "individual"}):
            app = create_app(database)
        with closing(connect(database)) as db, db:
            auth.save_user(db, "erin", password="a long password", by="test", now="")
        http = TestClient(app, base_url="http://127.0.0.1")
        before = http.get("/api/v1/session").json()["csrf_token"]
        signed = http.post("/api/v1/session", json={"user": "erin", "password": "a long password"},
                           headers={"X-CSRF-Token": before})
        self.assertEqual(signed.status_code, 200, signed.text)
        during = signed.json()["csrf_token"]
        self.assertNotEqual(before, during)
        self.assertEqual(http.get("/api/v1/session").json()["csrf_token"], during)
        stale = http.post("/api/v1/machines/pairing-codes", headers={"X-CSRF-Token": before})
        self.assertEqual((stale.status_code, stale.json()["code"]), (403, "csrf"))
        self.assertEqual(http.delete("/api/v1/session", headers={"X-CSRF-Token": during}).status_code, 204)
        after = http.get("/api/v1/session").json()["csrf_token"]
        self.assertNotIn(after, (before, during))


if __name__ == "__main__":
    unittest.main()
