import io
import json
import os
from pathlib import Path
import re
import sqlite3
from contextlib import closing
import tempfile
import unittest
from unittest import mock

from portal import create_app
import recorder_contract
import registry

DESK = "desktop:Twin Lab reference/CaseDesk"
WEB = "web:http://127.0.0.1:8080"
CV = recorder_contract.CONTRACT_VERSION


def references(session_id="20261119T100000Z-0a1b2c3d", portal_session_id=None, apps=None):
    session = {"id": session_id, "name": "Order to invoice", "started": "2026-11-19T10:00:00.000Z",
               "ended": "2026-11-19T10:05:00.000Z", "state": "stopped", "twinlab_commit": "5bb142a"}
    if portal_session_id:
        session["portal_session_id"] = portal_session_id
    return {
        "contract_version": recorder_contract.CONTRACT_VERSION,
        "format": "twinlab-references/1", "session": session,
        "apps": apps or [
            {"key": DESK, "surface": "desktop", "level": "full", "display": "CaseDesk",
             "exe": "CaseDesk.exe", "company": "Twin Lab reference", "product": "CaseDesk"},
            {"key": WEB, "surface": "web", "level": "full", "display": "127.0.0.1:8080"},
        ],
        "flows": {
            DESK: [{"n": 1, "action": "type", "control": "Edit", "label": "txtSearch", "screen": "MainForm"},
                   {"n": 2, "action": "click", "control": "Button", "label": "btnOpen", "screen": "MainForm"}],
            WEB: [{"n": 5, "action": "click", "control": "button", "label": "btnSave",
                   "screen": "http://127.0.0.1:8080/cases/{id}", "extra": "dropped"}],
        },
        "timeline": [{"app": DESK, "steps": 2}, {"app": "presence", "steps": 1}, {"app": WEB, "steps": 1}],
    }


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.database = Path(self.work.name) / "catalog.db"
        self.app = create_app(self.database)
        self.app.testing = True
        self.client = self.app.test_client()
        self.assertEqual(self.client.get("/").status_code, 200)
        with self.client.session_transaction() as session:
            self.token = session["form_token"]

    def post(self, path, **values):
        return self.client.post(path, data={"token": self.token, **values})

    def pair(self, name="Workstation 7"):
        self.assertEqual(self.post("/machines/pair").status_code, 302)
        page = self.client.get("/machines").data.decode()
        code = re.search(r'id="pair-code">([A-Z0-9-]+)<', page).group(1)
        self.assertNotIn(code, self.client.get("/machines").data.decode())  # shown once
        response = self.client.post("/api/agent/pair", json={"contract_version": CV, "code": code.lower(), "name": name})
        self.assertEqual(response.status_code, 200, response.data)
        return code, response.json["token"]

    def agent(self, token):
        return {"Authorization": f"Bearer {token}"}

    def test_pairing_codes_are_single_use_and_tokens_are_stored_hashed(self):
        code, token = self.pair()
        again = self.client.post("/api/agent/pair", json={"contract_version": CV, "code": code, "name": "Other"})
        self.assertEqual(again.status_code, 403)
        self.assertEqual(self.client.post("/api/agent/pair", json={"contract_version": CV, "code": "AAAAAAAAAA", "name": "X"}).status_code, 403)
        with closing(sqlite3.connect(self.database)) as db, db:
            stored = db.execute("SELECT token_sha FROM machines").fetchone()[0]
            codes = [r[0] for r in db.execute("SELECT code_sha FROM pairing_codes")]
        self.assertEqual(stored, registry.sha(token))
        self.assertNotIn(token, stored)
        self.assertNotIn(code.replace("-", ""), " ".join(codes))
        self.assertEqual(self.client.get("/api/agent/sessions", headers=self.agent(token)).status_code, 200)
        self.assertEqual(self.client.get("/api/agent/sessions").status_code, 401)
        self.assertEqual(self.client.get("/api/agent/sessions", headers=self.agent("x" * 43)).status_code, 401)
        self.assertEqual(self.post("/machines/1/revoke").status_code, 302)
        self.assertEqual(self.client.get("/api/agent/sessions", headers=self.agent(token)).status_code, 401)

    def test_expired_codes_and_too_many_failures_are_refused(self):
        self.post("/machines/pair")
        code = re.search(r'id="pair-code">([A-Z0-9-]+)<', self.client.get("/machines").data.decode()).group(1)
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE pairing_codes SET expires_at = '2000-01-01T00:00:00+00:00'")
        self.assertEqual(self.client.post("/api/agent/pair", json={"contract_version": CV, "code": code, "name": "X"}).status_code, 403)
        for _ in range(registry.FAILURES_PER_10_MIN):
            self.client.post("/api/agent/pair", json={"contract_version": CV, "code": "BBBBBBBBBB", "name": "X"})
        self.assertEqual(self.client.post("/api/agent/pair", json={"contract_version": CV, "code": "BBBBBBBBBB", "name": "X"}).status_code, 429)

    def test_references_register_discovered_apps_and_map_the_recording(self):
        _, token = self.pair()
        response = self.client.post("/api/agent/references", json=references(), headers=self.agent(token))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.json["discovered"]), 2)
        recording = self.client.get("/recordings/ws-20261119T100000Z-0a1b2c3d").data.decode()
        self.assertIn("CaseDesk", recording)
        self.assertIn("btnOpen", recording)
        self.assertIn("Other applications", recording)
        self.assertNotIn("dropped", recording)
        app_page = self.client.get("/apps/1").data.decode()
        self.assertIn("discovered", app_page)
        self.assertIn("txtSearch", app_page)
        self.assertIn("No automation yet", app_page)
        self.assertNotIn(DESK, app_page)  # identities are on the Setup / Advanced page
        self.assertIn(DESK, self.client.get("/apps/1/setup").data.decode())
        listing = self.client.get("/recordings").data.decode()
        self.assertIn("Workstation 7", listing)
        self.assertIn("CaseDesk (full)", listing)
        # sent again (a retry): the same recording, no new application
        again = self.client.post("/api/agent/references", json=references(), headers=self.agent(token))
        self.assertEqual(again.json["discovered"], [])
        self.assertEqual(sorted(again.json["mapped"]), [1, 2])
        # the catalog's evidence links: each application's steps are selected
        with closing(sqlite3.connect(self.database)) as db, db:
            links = dict(db.execute("SELECT app_id, selection FROM evidence").fetchall())
        self.assertEqual(json.loads(links[1]), [1, 2])

    def test_offered_session_targets_known_apps_and_links_its_automation(self):
        _, token = self.pair()
        self.post("/apps", name="Case desk")
        self.assertEqual(self.post("/apps/1/identity", key="CaseDesk.exe").status_code, 302)
        self.assertEqual(self.post("/apps/1/identity", key="https://Billing.example.test:443/x").status_code, 302)
        self.assertEqual(self.post("/apps/1/identity", key="not an identity").status_code, 400)
        self.post("/automations", name="UiPath order to invoice", kind="uipath")
        self.assertEqual(self.post("/sessions", name="Trial", machine_id="1", automation_id="1",
                                   app="1", level_1="steps").status_code, 302)
        offered = self.client.get("/api/agent/sessions", headers=self.agent(token)).json
        self.assertEqual(len(offered["sessions"]), 1)
        targets = offered["sessions"][0]["targets"]
        self.assertIn({"level": "steps", "app_id": 1, "name": "Case desk", "exe": "casedesk.exe"}, targets)
        self.assertIn({"level": "steps", "app_id": 1, "name": "Case desk",
                       "origin": "https://billing.example.test", "path": "/x"}, targets)
        refs = references(portal_session_id=1, apps=[
            {"key": DESK, "level": "steps", "display": "CaseDesk", "exe": "CaseDesk.exe", "app_id": 1}])
        refs["timeline"] = [{"app": DESK, "steps": 2}, {"app": "presence", "steps": 1}]
        response = self.client.post("/api/agent/references", json=refs, headers=self.agent(token))
        self.assertEqual(response.json["mapped"], [1])
        self.assertIn("UiPath order to invoice", self.client.get("/apps/1").data.decode())
        self.assertIn("recorded", self.client.get("/sessions").data.decode())
        self.assertEqual(self.client.get("/api/agent/sessions", headers=self.agent(token)).json["sessions"], [])
        self.assertIn("Order to invoice", self.client.get("/automations/1").data.decode())

    def test_not_replicated_apps_are_sent_as_deny_keys(self):
        _, token = self.pair()
        self.client.post("/api/agent/references", json=references(), headers=self.agent(token))
        self.assertEqual(self.post("/apps/2/status", status="not_replicated").status_code, 302)
        offered = self.client.get("/api/agent/sessions", headers=self.agent(token)).json
        self.assertEqual(offered["deny_keys"], [WEB])

    def test_invalid_references_are_refused(self):
        _, token = self.pair()
        for broken in ({"format": "x", "contract_version": recorder_contract.CONTRACT_VERSION},
                       dict(references(), session={"id": "../../etc"}),
                       dict(references(), apps=[{"key": "desktop:\nX", "level": "full"}]),
                       dict(references(), timeline=[{"app": "web:http://elsewhere", "steps": 1}])):
            response = self.client.post("/api/agent/references", json=broken, headers=self.agent(token))
            self.assertEqual(response.status_code, 400, broken)

    def test_another_machine_cannot_overwrite_a_recording(self):
        _, first = self.pair("A")
        _, second = self.pair("B")
        self.client.post("/api/agent/references", json=references(), headers=self.agent(first))
        response = self.client.post("/api/agent/references", json=references(), headers=self.agent(second))
        self.assertEqual(response.status_code, 409)

    def test_merge_and_import_automation_yaml(self):
        _, token = self.pair()
        self.client.post("/api/agent/references", json=references(), headers=self.agent(token))
        self.post("/apps", name="Showcase")
        unconfirmed = self.post("/apps/2/merge", into="3")
        self.assertEqual(unconfirmed.location, "/apps/2/merge?into=3")  # the preview first
        preview = self.client.get(unconfirmed.location).data.decode()
        fingerprint = re.search(r'name="confirm" value="([0-9a-f]+)"', preview).group(1)
        self.assertEqual(self.post("/apps/2/merge", into="3", confirm=fingerprint).location, "/apps/3")
        self.assertIn(WEB, self.client.get("/apps/3/setup").data.decode())
        yaml_doc = b"id: case-update\nname: Case update\ntype: uipath\ntouches:\n- app: showcase\n- app: missing\n"
        response = self.client.post("/automations/import", data={"token": self.token,
                                    "file": (io.BytesIO(yaml_doc), "automation.yaml")})
        self.assertEqual(response.status_code, 302)
        automation = self.client.get(response.location).data.decode()
        self.assertIn("Showcase", automation)
        self.assertIn("missing", automation)


class SignInTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.database = Path(self.work.name) / "catalog.db"

    def make(self, **env):
        with mock.patch.dict(os.environ, env):
            app = create_app(self.database)
        app.testing = True
        return app.test_client()

    def test_only_this_machine_without_a_password_or_proxy(self):
        client = self.make()
        self.assertEqual(client.get("/", environ_base={"REMOTE_ADDR": "10.1.2.3"}).status_code, 403)
        self.assertEqual(client.get("/healthz", environ_base={"REMOTE_ADDR": "10.1.2.3"}).status_code, 200)

    def test_password_sign_in(self):
        client = self.make(TWINLAB2_PASSWORD="s3cret-demo")
        self.assertEqual(client.get("/machines").status_code, 302)
        client.get("/login")
        with client.session_transaction() as session:
            token = session["form_token"]
        bad = client.post("/login", data={"token": token, "user": "ana", "password": "nope"})
        self.assertEqual(bad.status_code, 401)
        good = client.post("/login?next=/machines", data={"token": token, "user": "ana", "password": "s3cret-demo"})
        self.assertEqual(good.location, "/machines")
        self.assertIn("ana", client.get("/machines").data.decode())

    def test_proxy_user(self):
        client = self.make(TWINLAB2_TRUST_PROXY_USER="1")
        self.assertEqual(client.get("/").status_code, 401)
        page = client.get("/", headers={"X-Forwarded-User": "ana@example.test"})
        self.assertEqual(page.status_code, 200)
        self.assertIn("ana@example.test", page.data.decode())


if __name__ == "__main__":
    unittest.main()
