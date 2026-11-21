import json
import os
from contextlib import closing
import tempfile
import unittest
from unittest import mock

from twinlab2.portal import create_app
from twinlab2 import event_ids
from twinlab2_contract import recorder_contract
from twinlab2 import registry
from support import Client, connect, new_database

DESK = "desktop:Twin Lab reference/CaseDesk"
WEB = "web:http://127.0.0.1:8080"
CV = recorder_contract.CONTRACT_VERSION


def eid(recording_id, n):
    """The id the portal derives for event `n` of a recording from an older recorder."""
    return event_ids.derived(recording_id, n)


def numbers(database, recording_id, ids):
    """The display numbers (seq) of stored event ids, sorted: what the tests compare."""
    with closing(connect(database)) as db:
        row = db.execute("SELECT events FROM recordings WHERE id = ?", (recording_id,)).fetchone()
    order = {e["event_id"]: e["seq"] for e in json.loads(row[0])}
    return sorted(order[i] for i in ids)


def by_number(database, recording_id, wanted):
    """The stored event ids of the given display numbers."""
    with closing(connect(database)) as db:
        row = db.execute("SELECT events FROM recordings WHERE id = ?", (recording_id,)).fetchone()
    found = {e["seq"]: e["event_id"] for e in json.loads(row[0])}
    return [found[n] for n in wanted]


def references(session_id="20261119T100000Z-0a1b2c3d", portal_session_id=None, apps=None):
    session = {"id": session_id, "name": "Order to invoice", "started": "2026-11-19T10:00:00.000Z",
               "ended": "2026-11-19T10:05:00.000Z", "state": "stopped", "twinlab_commit": "5bb142a"}
    if portal_session_id:
        session["portal_session_id"] = portal_session_id
    return {
        "contract_version": "1.0.0",  # the 1.0 shape: steps without event ids (see references_1_1)
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


# Event ids as a contract 1.1 recorder assigns them at capture time (UUIDv7).
EVENT_1 = "019a9f1c-0000-7000-8000-000000000001"
EVENT_2 = "019a9f1c-0000-7000-8000-000000000002"
EVENT_5 = "019a9f1c-0000-7000-8000-000000000005"


def completeness_report(overall="complete", **channels):
    report = {"overall": overall, "channels": {"web": {"status": "complete", "gaps": [], "notes": []},
                                               "desktop": {"status": "complete", "gaps": [], "notes": []},
                                               "mainframe": {"status": "not_requested"}},
              "network_evidence": "complete", "recovered": False, "loss_window_seconds": None}
    report["channels"].update(channels)
    return report


def references_1_1(session_id="20261119T100000Z-0a1b2c3d", **completeness):
    """The references() session as a contract 1.1 recorder sends it: every step with its event
    id, channel, capture time and display order, and the recording's completeness."""
    doc = references(session_id)
    doc["contract_version"] = "1.1.0"
    times = {1: "2026-11-19T10:01:00.000Z", 2: "2026-11-19T10:02:00.000Z", 5: "2026-11-19T10:04:00.000Z"}
    ids = {1: EVENT_1, 2: EVENT_2, 5: EVENT_5}
    for key, steps in doc["flows"].items():
        for step in steps:
            step.update(event_id=ids[step["n"]], channel=key.split(":", 1)[0], captured_at=times[step["n"]],
                        seq=step["n"])
    doc["completeness"] = completeness.get("report") or completeness_report()
    doc["preflight"] = {"checks": [{"name": "UI Automation", "ok": True, "detail": "available"},
                                   {"name": "Browser profile", "ok": True, "detail": ""}], "ready": True}
    return doc


RECORDING = "ws-20261119T100000Z-0a1b2c3d"


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.database = new_database(self.work.name)
        self.app = create_app(self.database)
        self.client = Client(self.app)
        self.assertEqual(self.client.get("/api/v1/apps").status_code, 200)

    def ok(self, response, status=200):
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def pair(self, name="Workstation 7"):
        made = self.ok(self.client.post("/api/v1/machines/pairing-codes"), 201)
        code = made["code"]
        self.assertEqual(made["display"].replace("-", ""), code)
        self.assertNotIn(code, self.client.get("/api/v1/machines").text)  # shown once
        response = self.client.http.post("/api/agent/pair", json={
            "contract_version": CV, "code": made["display"].lower(), "name": name})
        self.assertEqual(response.status_code, 200, response.text)
        return code, response.json()["token"]

    def agent(self, token):
        return {"Authorization": f"Bearer {token}"}

    def send(self, refs, token):
        return self.client.http.post("/api/agent/references", json=refs, headers=self.agent(token))

    def test_pairing_codes_are_single_use_and_tokens_are_stored_hashed(self):
        code, token = self.pair()
        http = self.client.http
        again = http.post("/api/agent/pair", json={"contract_version": CV, "code": code, "name": "Other"})
        self.assertEqual(again.status_code, 403)
        self.assertEqual(http.post("/api/agent/pair", json={"contract_version": CV, "code": "AAAAAAAAAA",
                                                            "name": "X"}).status_code, 403)
        with closing(connect(self.database)) as db, db:
            stored = db.execute("SELECT token_sha FROM machines").fetchone()[0]
            codes = [r[0] for r in db.execute("SELECT code_sha FROM pairing_codes")]
        self.assertEqual(stored, registry.sha(token))
        self.assertNotIn(token, stored)
        self.assertNotIn(code.replace("-", ""), " ".join(codes))
        self.assertEqual(http.get("/api/agent/sessions", headers=self.agent(token)).status_code, 200)
        self.assertEqual(http.get("/api/agent/sessions").status_code, 401)
        self.assertEqual(http.get("/api/agent/sessions", headers=self.agent("x" * 43)).status_code, 401)
        self.assertIsNotNone(self.ok(self.client.post("/api/v1/machines/1/revoke"))["revoked_at"])
        self.assertEqual(http.get("/api/agent/sessions", headers=self.agent(token)).status_code, 401)

    def test_expired_codes_and_too_many_failures_are_refused(self):
        code = self.ok(self.client.post("/api/v1/machines/pairing-codes"), 201)["code"]
        http = self.client.http
        with closing(connect(self.database)) as db, db:
            db.execute("UPDATE pairing_codes SET expires_at = '2000-01-01T00:00:00+00:00'")
        self.assertEqual(http.post("/api/agent/pair", json={"contract_version": CV, "code": code,
                                                            "name": "X"}).status_code, 403)
        for _ in range(registry.FAILURES_PER_10_MIN):
            http.post("/api/agent/pair", json={"contract_version": CV, "code": "BBBBBBBBBB", "name": "X"})
        self.assertEqual(http.post("/api/agent/pair", json={"contract_version": CV, "code": "BBBBBBBBBB",
                                                            "name": "X"}).status_code, 429)

    def test_references_register_discovered_apps_and_map_the_recording(self):
        _, token = self.pair()
        response = self.send(references(), token)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["discovered"]), 2)
        detail = self.client.get(f"/api/v1/recordings/{RECORDING}")
        refs = self.ok(detail)["refs"]
        [desk] = [a for a in refs["apps"] if a["key"] == DESK]
        self.assertEqual(desk["name"], "CaseDesk")
        self.assertIn("btnOpen", [s["label"] for s in desk["steps"]])
        self.assertIn("Other applications", [run["name"] for run in refs["timeline"]])
        self.assertNotIn("dropped", detail.text)
        app = self.client.get("/api/v1/apps/1")
        self.assertEqual(self.ok(app)["status"], "discovered")
        self.assertNotIn(DESK, app.text)  # identities are on the Setup page
        evidence = self.ok(self.client.get("/api/v1/apps/1/evidence"))
        self.assertIn("txtSearch", [s["label"] for f in evidence["recorded_flows"] for s in f["steps"]])
        self.assertEqual(evidence["automations"], [])  # no automation yet
        self.assertIn(DESK, [i["key"] for i in self.ok(self.client.get("/api/v1/apps/1/setup"))["identities"]])
        [listed] = self.ok(self.client.get("/api/v1/recordings"))["recordings"]
        self.assertEqual(listed["machine"], "Workstation 7")
        self.assertIn("CaseDesk (full)", listed["apps"])
        # sent again (a retry): the same recording, no new application
        again = self.send(references(), token).json()
        self.assertEqual(again["discovered"], [])
        self.assertEqual(sorted(again["mapped"]), [1, 2])
        # the catalog's evidence links: each application's steps are selected
        with closing(connect(self.database)) as db, db:
            links = dict(db.execute("SELECT app_id, selection FROM evidence").fetchall())
        self.assertEqual(json.loads(links[1]), sorted([eid(RECORDING, 1), eid(RECORDING, 2)]))

    def test_offered_session_targets_known_apps_and_links_its_automation(self):
        _, token = self.pair()
        http = self.client.http
        self.ok(self.client.post("/api/v1/apps", json={"name": "Case desk"}), 201)
        self.ok(self.client.post("/api/v1/apps/1/identities", json={"key": "CaseDesk.exe"}), 201)
        self.ok(self.client.post("/api/v1/apps/1/identities", json={"key": "https://Billing.example.test:443/x"}), 201)
        self.assertEqual(self.client.post("/api/v1/apps/1/identities", json={"key": "not an identity"}).status_code, 400)
        self.ok(self.client.post("/api/v1/automations", json={"name": "UiPath order to invoice", "kind": "uipath"}), 201)
        self.ok(self.client.post("/api/v1/capture-sessions", json={
            "name": "Trial", "machine_id": 1, "automation_id": 1, "targets": [{"app_id": 1, "level": "steps"}]}), 201)
        offered = http.get("/api/agent/sessions", headers=self.agent(token)).json()
        self.assertEqual(len(offered["sessions"]), 1)
        targets = offered["sessions"][0]["targets"]
        self.assertIn({"level": "steps", "app_id": 1, "name": "Case desk", "exe": "casedesk.exe"}, targets)
        self.assertIn({"level": "steps", "app_id": 1, "name": "Case desk",
                       "origin": "https://billing.example.test", "path": "/x"}, targets)
        refs = references(portal_session_id=1, apps=[
            {"key": DESK, "level": "steps", "display": "CaseDesk", "exe": "CaseDesk.exe", "app_id": 1}])
        refs["timeline"] = [{"app": DESK, "steps": 2}, {"app": "presence", "steps": 1}]
        response = self.send(refs, token)
        self.assertEqual(response.json()["mapped"], [1])
        evidence = self.ok(self.client.get("/api/v1/apps/1/evidence"))
        self.assertIn("UiPath order to invoice", [a["name"] for a in evidence["automations"]])
        sessions = self.ok(self.client.get("/api/v1/capture-sessions"))["sessions"]
        self.assertEqual([s["status"] for s in sessions], ["recorded"])
        self.assertEqual(http.get("/api/agent/sessions", headers=self.agent(token)).json()["sessions"], [])
        automation = self.ok(self.client.get("/api/v1/automations/1"))
        self.assertIn("Order to invoice", [r["name"] for r in automation["recordings"]])

    def test_not_replicated_apps_are_sent_as_deny_keys(self):
        _, token = self.pair()
        self.send(references(), token)
        self.ok(self.client.patch("/api/v1/apps/2", json={"status": "not_replicated"}))
        offered = self.client.http.get("/api/agent/sessions", headers=self.agent(token)).json()
        self.assertEqual(offered["deny_keys"], [WEB])

    def test_invalid_references_are_refused(self):
        _, token = self.pair()
        for broken in ({"format": "x", "contract_version": recorder_contract.CONTRACT_VERSION},
                       dict(references(), session={"id": "../../etc"}),
                       dict(references(), apps=[{"key": "desktop:\nX", "level": "full"}]),
                       dict(references(), timeline=[{"app": "web:http://elsewhere", "steps": 1}])):
            response = self.send(broken, token)
            self.assertEqual(response.status_code, 400, broken)

    def test_another_machine_cannot_overwrite_a_recording(self):
        _, first = self.pair("A")
        _, second = self.pair("B")
        self.send(references(), first)
        self.assertEqual(self.send(references(), second).status_code, 409)

    def test_merge_and_import_automation_yaml(self):
        _, token = self.pair()
        self.send(references(), token)
        self.ok(self.client.post("/api/v1/apps", json={"name": "Showcase"}), 201)
        unconfirmed = self.client.post("/api/v1/apps/2/merge", json={"into": 3, "confirm": ""})
        self.assertEqual((unconfirmed.status_code, unconfirmed.json()["code"]), (409, "plan_changed"))  # preview first
        fingerprint = self.ok(self.client.get("/api/v1/apps/2/merge-plan?into=3"))["fingerprint"]
        merged = self.ok(self.client.post("/api/v1/apps/2/merge", json={"into": 3, "confirm": fingerprint}))
        self.assertEqual(merged["id"], 3)
        self.assertIn(WEB, [i["key"] for i in self.ok(self.client.get("/api/v1/apps/3/setup"))["identities"]])
        yaml_doc = b"id: case-update\nname: Case update\ntype: uipath\ntouches:\n- app: showcase\n- app: missing\n"
        automation = self.ok(self.client.post("/api/v1/automations/import",
                                              files={"file": ("automation.yaml", yaml_doc, "application/yaml")}), 201)
        self.assertIn("Showcase", [a["name"] for a in automation["apps"]])
        self.assertIn("missing", automation["automation"]["notes"])
        self.assertEqual(self.ok(self.client.get(f"/api/v1/automations/{automation['automation']['id']}")), automation)


class SignInTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.database = new_database(self.work.name)

    def make(self, remote="127.0.0.1", **env):
        with mock.patch.dict(os.environ, env):
            app = create_app(self.database)
        return Client(app, remote=remote)

    def test_only_this_machine_without_a_password_or_proxy(self):
        client = self.make(remote="10.1.2.3")
        self.assertEqual(client.get("/api/v1/apps").status_code, 403)
        self.assertEqual(client.http.get("/healthz").status_code, 200)

    def test_password_sign_in(self):
        client = self.make(TWINLAB2_PASSWORD="s3cret-demo")
        signed_out = client.get("/api/v1/machines")
        self.assertEqual((signed_out.status_code, signed_out.json()["code"]), (401, "signed_out"))
        bad = client.post("/api/v1/session", json={"user": "ana", "password": "nope"})
        self.assertEqual((bad.status_code, bad.json()["code"]), (401, "wrong_credentials"))
        good = client.post("/api/v1/session", json={"user": "ana", "password": "s3cret-demo"})
        self.assertEqual(good.status_code, 200, good.text)
        self.assertEqual(good.json()["user"], "ana")
        self.assertEqual(client.get("/api/v1/machines").status_code, 200)
        self.assertEqual(client.get("/api/v1/session").json()["user"], "ana")

    def test_proxy_user(self):
        client = self.make(TWINLAB2_TRUST_PROXY_USER="1")
        self.assertEqual(client.get("/api/v1/apps").status_code, 401)
        ana = client.as_user("ana@example.test")
        self.assertEqual(ana.get("/api/v1/apps").status_code, 200)
        self.assertEqual(ana.get("/api/v1/session").json()["user"], "ana@example.test")


if __name__ == "__main__":
    unittest.main()
