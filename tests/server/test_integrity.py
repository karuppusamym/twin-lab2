"""Regression tests for evidence integrity: a resend keeps the owner's reviewed selection, a merge
keeps briefs and combines overlapping evidence links, and web identities are told apart by path."""

from contextlib import closing
import io
import json
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

from twinlab2 import migrations
from twinlab2 import registry
from twinlab2.portal import create_app
from support import Client, connect, freeze, new_database
from test_registry import CV, DESK, WEB, by_number, eid, numbers, references

RECORDING = "ws-20261119T100000Z-0a1b2c3d"


def pair(client):
    """Pair a recorder through a pairing code; the recorder's Authorization header."""
    code = client.post("/api/v1/machines/pairing-codes")
    assert code.status_code == 201, code.text
    paired = client.http.post("/api/agent/pair", json={
        "contract_version": CV, "code": code.json()["code"], "name": "PC"})
    assert paired.status_code == 200, paired.text
    return {"Authorization": "Bearer " + paired.json()["token"]}


class Base(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.database = new_database(self.work.name)
        self.start()

    def start(self):
        self.app = create_app(self.database)
        self.client = Client(self.app)
        self.assertEqual(self.client.get("/api/v1/apps").status_code, 200)

    def pair(self):
        self.agent = pair(self.client)

    def send(self, refs=None):
        response = self.client.http.post("/api/agent/references", json=refs or references(), headers=self.agent)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def link(self, app_id, recording_id=RECORDING):
        with closing(connect(self.database)) as db:
            row = db.execute("SELECT * FROM evidence WHERE app_id = ? AND recording_id = ?",
                             (app_id, recording_id)).fetchone()
        # stored as event ids; compared by the events' display numbers
        return row and {"selection": numbers(self.database, recording_id, json.loads(row["selection"])),
                        "suggested": numbers(self.database, recording_id, json.loads(row["suggested"])),
                        "notes": row["notes"], "reviewed": row["reviewed"]}

    def assign(self, app_id, events, notes="", recording_id=RECORDING):
        response = self.client.put(f"/api/v1/recordings/{recording_id}/assignments/{app_id}", json={
            "notes": notes, "events": by_number(self.database, recording_id, events)})
        self.assertEqual(response.status_code, 200, response.text)

    def assignment(self, app_id, recording_id=RECORDING):
        response = self.client.get(f"/api/v1/recordings/{recording_id}/assignments/{app_id}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def evidence_link(self, app_id, recording_id=RECORDING):
        response = self.client.get(f"/api/v1/apps/{app_id}/evidence")
        self.assertEqual(response.status_code, 200, response.text)
        return next(link for link in response.json()["links"] if link["recording_id"] == recording_id)

    def plan(self, source, into):
        preview = self.client.get(f"/api/v1/apps/{source}/merge-plan?into={into}")
        self.assertEqual(preview.status_code, 200, preview.text)
        return preview.json()

    def merge(self, source, into):
        """The merge plan, and the confirmed merge (None when the plan has blockers)."""
        plan = self.plan(source, into)
        if plan["blockers"]:
            return plan, None
        return plan, self.client.post(f"/api/v1/apps/{source}/merge",
                                      json={"into": into, "confirm": plan["fingerprint"]})

    def scenario(self, app_id, name):
        response = self.client.post(f"/api/v1/apps/{app_id}/scenarios", json={"name": name})
        self.assertEqual(response.status_code, 201, response.text)


class ResendTest(Base):
    def test_a_resend_keeps_the_reviewed_selection_and_suggests_new_events(self):
        self.pair()
        self.send()
        self.assertEqual(self.link(1), {"selection": [1, 2], "suggested": [], "notes": "", "reviewed": 0})
        self.assign(1, [1], notes="Only the search")
        # the repro: resending the same recording turned the selection back into [1, 2]
        self.send()
        self.assertEqual(self.link(1), {"selection": [1], "suggested": [], "notes": "Only the search", "reviewed": 1})
        # a resend with a new event: suggested, not selected
        refs = references()
        refs["flows"][DESK].append({"n": 3, "action": "click", "control": "Button", "label": "btnSave",
                                    "screen": "MainForm"})
        refs["timeline"][0]["steps"] = 3
        self.send(refs)
        self.assertEqual(self.link(1)["selection"], [1])
        self.assertEqual(self.link(1)["suggested"], [3])
        self.send(refs)  # sending it again changes nothing
        self.assertEqual(self.link(1)["suggested"], [3])
        # the recording and the application show the new, not yet reviewed event
        self.assertEqual(self.assignment(1)["suggested"], by_number(self.database, RECORDING, [3]))
        preview = {p["app_id"]: p for p in self.client.get(f"/api/v1/recordings/{RECORDING}").json()["preview"]}
        self.assertEqual(preview[1]["suggested"], 1)
        self.assertEqual(self.evidence_link(1)["suggested_count"], 1)
        # the build pack has only the reviewed selection
        self.scenario(1, "Find a case")
        with ZipFile(io.BytesIO(freeze(self.client, 1).content)) as pack:
            evidence = json.loads(pack.read("evidence.json"))["evidence"]
        self.assertEqual([e["seq"] for e in evidence[0]["events"]], [1])
        # the owner reviews the suggestion
        self.assign(1, [1, 3], notes="Search and save")
        self.assertEqual(self.link(1), {"selection": [1, 3], "suggested": [], "notes": "Search and save", "reviewed": 1})
        self.assertEqual(self.assignment(1)["suggested"], [])
        self.assertEqual(self.evidence_link(1)["suggested_count"], 0)

    def test_an_unreviewed_link_follows_the_resend(self):
        self.pair()
        refs = references()
        refs["flows"][DESK] = refs["flows"][DESK][:1]
        self.send(refs)
        self.assertEqual(self.link(1)["selection"], [1])
        self.send()
        self.assertEqual(self.link(1), {"selection": [1, 2], "suggested": [], "notes": "", "reviewed": 0})
        self.assertFalse(self.evidence_link(1)["reviewed"])  # not yet reviewed
        self.assertFalse(self.assignment(1)["reviewed"])


class MergeTest(Base):
    def setUp(self):
        super().setUp()
        self.pair()
        self.send()  # app 1 CaseDesk (events 1, 2), app 2 the web application (event 5)
        created = self.client.post("/api/v1/apps", json={"name": "Showcase"})
        self.assertEqual((created.status_code, created.json()["id"]), (201, 3))

    def test_a_merge_keeps_historical_briefs_and_their_packages(self):
        self.scenario(2, "Save a case")
        # a brief stored before builder packages were frozen (the portal stored one per export)
        package = b"PK legacy brief"
        revision = "ab" * 32
        with closing(connect(self.database)) as db, db:
            db.execute("INSERT INTO briefs VALUES (?, 2, '{}')", (revision,))
            db.execute("INSERT INTO brief_packages(revision, archive) VALUES (?, ?)", (revision, package))
        # the repro: the merge deleted the brief while its package still referred to it
        plan, done = self.merge(2, 3)
        self.assertEqual(plan["briefs"], [{"revision": revision, "package": True}])  # kept with its download
        self.assertEqual(plan["scenarios"], ["Save a case"])  # its scenario moves too
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual(done.json(), {"id": 3, "name": "Showcase"})
        self.assertEqual(self.client.get("/api/v1/apps/2").status_code, 404)
        again = self.client.get(f"/api/v1/apps/3/briefs/{revision}/download")
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.content, package)
        self.assertEqual(self.link(3)["selection"], [5])
        keys = [i["key"] for i in self.client.get("/api/v1/apps/3/setup").json()["identities"]]
        self.assertIn(WEB, keys)

    def test_a_frozen_builder_package_blocks_a_merge(self):
        self.scenario(2, "Save a case")
        self.assertEqual(freeze(self.client, 2).status_code, 200)
        self.assertIn("1 builder packages", self.plan(2, 3)["blockers"])

    def test_overlapping_reviewed_links_are_combined_not_dropped(self):
        self.assign(1, [1], notes="Search for the case")
        self.assign(3, [2, 5], notes="Open it and save")
        plan, done = self.merge(1, 3)
        # both applications link this recording: combined, shown as events 1, 2, 5
        self.assertEqual([(c["recording_id"], c["shown"]) for c in plan["combined"]], [(RECORDING, [1, 2, 5])])
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual(self.link(3), {
            "selection": [1, 2, 5], "suggested": [], "reviewed": 1,
            "notes": "Open it and save\n\nMerged from CaseDesk: Search for the case"})
        self.assertIsNone(self.link(1))

    def test_an_unreviewed_link_does_not_widen_a_reviewed_one(self):
        self.assign(3, [1])
        plan, done = self.merge(2, 3)  # app 2's link (event 5) was selected automatically
        # event 5 becomes new, not yet reviewed
        self.assertEqual([(c["shown"], c["shown_suggested"]) for c in plan["combined"]], [([1], [5])])
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual(self.link(3), {"selection": [1], "suggested": [5], "notes": "", "reviewed": 1})

    def test_a_stale_or_missing_confirmation_is_refused(self):
        fingerprint = self.plan(1, 3)["fingerprint"]
        self.assign(3, [2])  # the target changed after the preview
        response = self.client.post("/api/v1/apps/1/merge", json={"into": 3, "confirm": fingerprint})
        self.assertEqual((response.status_code, response.json()["code"]), (409, "plan_changed"))
        self.assertEqual(self.client.get("/api/v1/apps/1").status_code, 200)
        unconfirmed = self.client.post("/api/v1/apps/1/merge", json={"into": 3, "confirm": ""})
        self.assertEqual((unconfirmed.status_code, unconfirmed.json()["code"]), (409, "plan_changed"))
        self.assertEqual(self.client.post("/api/v1/apps/1/merge", json={"into": 1, "confirm": ""}).status_code, 400)

    def test_rows_the_merge_cannot_move_block_it(self):
        added = self.client.post("/api/v1/apps/1/attachments", data={
            "title": "Screen", "kind": "Screen", "share": "reviewed"},
            files={"file": ("screen.png", b"\x89PNG", "image/png")})
        self.assertEqual(added.status_code, 201, added.text)
        plan, done = self.merge(1, 3)
        self.assertEqual(plan["blockers"], ["1 attachments"])  # cannot be merged while it has 1 attachments
        self.assertIsNone(done)
        refused = self.client.post("/api/v1/apps/1/merge", json={"into": 3, "confirm": "0" * 16})
        self.assertEqual(refused.status_code, 409)
        refused = self.client.post("/api/v1/apps/1/merge", json={"into": 3, "confirm": plan["fingerprint"]})
        self.assertEqual(refused.status_code, 400)
        self.assertIn("1 attachments", refused.json()["detail"])
        with closing(connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM apps").fetchone()[0], 3)


ORDERS_REFS = {
    "key": "web:https://example.test", "level": "full", "display": "example.test",
}


def shared_host_references(screens):
    refs = references(apps=[dict(ORDERS_REFS)])
    refs["flows"] = {"web:https://example.test": [
        {"n": n, "action": "click", "control": "button", "label": f"b{n}", "screen": screen}
        for n, screen in enumerate(screens, 1)]}
    refs["timeline"] = [{"app": "web:https://example.test", "steps": len(screens)}]
    return refs


class UrlIdentityTest(Base):
    def test_web_keys_keep_their_path(self):
        self.assertEqual(registry.normalise_key("https://Example.test:443/orders/"), "web:https://example.test/orders")
        self.assertEqual(registry.normalise_key("https://example.test//billing?x=1#y"), "web:https://example.test/billing")
        self.assertEqual(registry.normalise_key("https://example.test/"), "web:https://example.test")
        self.assertNotEqual(registry.normalise_key("https://example.test/orders"),
                            registry.normalise_key("https://example.test/billing"))
        self.assertIsNone(registry.normalise_key("https://example.test:99999/x"))
        self.assertEqual(registry.web_parts("web:https://example.test/orders/new"),
                         ("web:https://example.test", "/orders/new"))

    def identity(self, app_id, key):
        return self.client.post(f"/api/v1/apps/{app_id}/identities", json={"key": key})

    def make(self, name, *keys):
        created = self.client.post("/api/v1/apps", json={"name": name})
        self.assertEqual(created.status_code, 201, created.text)
        app_id = created.json()["id"]
        for key in keys:
            self.assertEqual(self.identity(app_id, key).status_code, 201, key)
        return app_id

    def url_match(self, app_id, mode):
        return self.client.put(f"/api/v1/apps/{app_id}/url-match", json={"url_match": mode})

    def flows(self):
        with closing(connect(self.database)) as db:
            return {(app_id, key): [s["label"] for s in json.loads(flow)] for app_id, key, flow in
                    db.execute("SELECT app_id, key, flow FROM recording_apps")}

    def test_apps_on_one_host_are_told_apart_by_path(self):
        self.pair()
        orders = self.make("Orders", "https://example.test/orders")
        billing = self.make("Billing", "https://example.test/billing")
        self.assertEqual(self.identity(billing, "https://example.test/orders/").status_code, 409)
        portal_app = self.make("Portal", "https://example.test")  # a host-only identity still works
        self.make("Order lines", "https://example.test/orders/lines")
        result = self.send(shared_host_references([
            "https://example.test/orders/7", "https://example.test/billing/7",
            "https://example.test/help", "https://example.test/orders/lines/1", "https://example.test/ordersx"]))
        self.assertEqual(result["discovered"], [])
        self.assertEqual(self.flows(), {
            (orders, "web:https://example.test/orders"): ["b1"],
            (billing, "web:https://example.test/billing"): ["b2"],
            (portal_app, "web:https://example.test"): ["b3", "b5"],
            (4, "web:https://example.test/orders/lines"): ["b4"],
        })
        self.assertEqual(self.link(orders)["selection"], [1])
        self.assertEqual(self.link(billing)["selection"], [2])
        detail = self.client.get(f"/api/v1/recordings/{RECORDING}").json()
        self.assertIn("Orders", [a["name"] for a in detail["refs"]["apps"]])
        # a session targeting Orders sends the origin to the recorder, with the path
        session = self.client.post("/api/v1/capture-sessions", json={
            "name": "Trial", "machine_id": 1, "targets": [{"app_id": orders, "level": "full"}]})
        self.assertEqual(session.status_code, 201, session.text)
        target = self.client.http.get("/api/agent/sessions", headers=self.agent).json()["sessions"][0]["targets"][0]
        self.assertEqual((target["origin"], target["path"]), ("https://example.test", "/orders"))

    def test_steps_no_path_claims_go_to_a_discovered_app_not_a_path_target(self):
        self.pair()
        orders = self.make("Orders", "https://example.test/orders")
        refs = shared_host_references(["https://example.test/orders/1", "https://example.test/billing/1"])
        refs["apps"][0]["app_id"] = orders  # the recorder echoes the target's catalog id
        result = self.send(refs)
        self.assertEqual(len(result["discovered"]), 1)
        found = result["discovered"][0]
        self.assertEqual(self.flows(), {(orders, "web:https://example.test/orders"): ["b1"],
                                        (found, "web:https://example.test"): ["b2"]})

    def test_the_owner_can_make_an_app_match_its_whole_host(self):
        self.pair()
        orders = self.make("Orders", "https://example.test/orders")
        billing = self.make("Billing", "https://example.test/billing")
        self.assertEqual(self.client.get(f"/api/v1/apps/{billing}/setup").json()["app"]["url_match"], "path")
        changed = self.url_match(billing, "host")
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(changed.json()["app"]["url_match"], "host")
        self.send(shared_host_references(["https://example.test/orders/1", "https://example.test/help"]))
        self.assertEqual(self.flows(), {(orders, "web:https://example.test/orders"): ["b1"],
                                        (billing, "web:https://example.test/billing"): ["b2"]})
        # only one application can claim the whole host
        self.assertEqual(self.url_match(orders, "host").status_code, 409)
        other = self.make("Other")
        self.assertEqual(self.identity(other, "https://example.test").status_code, 409)
        self.assertEqual(self.url_match(billing, "sideways").status_code, 400)

    def test_deny_keys_include_path_identities_but_not_a_host_applications(self):
        """Contract 1.2 recorders deny steps under a path key; older ones compare whole keys, so a
        path key never denies the applications sharing its host. A `host` application's path
        key is left out: it stands for whatever no path claims."""
        self.pair()
        billing = self.make("Billing", "https://example.test/billing")
        legacy = self.make("Legacy", "https://legacy.example.test")
        catch_all = self.make("Catch-all", "https://shared.example.test/start")
        self.assertEqual(self.url_match(catch_all, "host").status_code, 200)
        for app_id in (billing, legacy, catch_all):
            response = self.client.patch(f"/api/v1/apps/{app_id}", json={"status": "not_replicated"})
            self.assertEqual(response.status_code, 200, response.text)
        deny = self.client.http.get("/api/agent/sessions", headers=self.agent).json()["deny_keys"]
        self.assertEqual(deny, ["web:https://example.test/billing", "web:https://legacy.example.test"])

    def test_a_not_replicated_path_application_gets_no_steps(self):
        """An older recorder sends every step of a shared host: the steps under a not-replicated
        application's path are not stored as its flow nor linked as its evidence."""
        self.pair()
        orders = self.make("Orders", "https://example.test/orders")
        payroll = self.make("Payroll", "https://example.test/payroll")
        response = self.client.patch(f"/api/v1/apps/{payroll}", json={"status": "not_replicated"})
        self.assertEqual(response.status_code, 200, response.text)
        self.send(shared_host_references(["https://example.test/orders/{id}", "https://example.test/payroll/{id}"]))
        self.assertEqual(self.flows(), {(orders, "web:https://example.test/orders"): ["b1"]})
        self.assertIsNone(self.link(payroll))
        self.assertEqual(self.link(orders)["selection"], [1])


class MigrationTest(unittest.TestCase):
    def test_a_catalog_from_before_these_columns_is_migrated(self):
        with tempfile.TemporaryDirectory() as work:
            database = Path(work) / "catalog.db"
            with closing(connect(database)) as db, db:
                for sql in migrations.BASELINE_TABLES:  # apps and evidence as they were
                    db.ddl(sql)
                db.execute("ALTER TABLE apps ADD COLUMN status TEXT NOT NULL DEFAULT 'known'")
                db.execute("ALTER TABLE apps ADD COLUMN surface TEXT NOT NULL DEFAULT ''")
                db.execute("INSERT INTO apps(id, name, git_url, requirements) VALUES (1, 'Shop', '', '')")
                db.execute("INSERT INTO recordings VALUES (?, 'Old', ?)",
                           (RECORDING, json.dumps([{"seq": 5, "source": "shop", "action": "click"}])))
                db.execute("INSERT INTO evidence VALUES (1, ?, '[5]', 'kept')", (RECORDING,))
                db.execute("INSERT INTO app_identities VALUES ('web:http://127.0.0.1:8080', 1, 'web', '{}')")
            for _ in range(2):  # migrating twice is harmless
                app = create_app(database)
            with closing(connect(database)) as db:
                row = db.execute("SELECT selection, notes, reviewed, suggested FROM evidence").fetchone()
                mode = db.execute("SELECT url_match FROM apps").fetchone()[0]
            # the old selection by number is now by the event's id
            self.assertEqual(row, (json.dumps([eid(RECORDING, 5)]), "kept", 1, "[]"))
            self.assertEqual(mode, "path")
            # the old host-only identity still maps the recorder's references
            client = Client(app)
            agent = pair(client)
            refs = references(apps=[{"key": WEB, "level": "full"}])
            refs["flows"] = {WEB: references()["flows"][WEB]}
            refs["timeline"] = [{"app": WEB, "steps": 1}]
            result = client.http.post("/api/agent/references", json=refs, headers=agent).json()
            self.assertEqual(result["mapped"], [1])
            with closing(connect(database)) as db:
                row = db.execute("SELECT selection, notes, reviewed FROM evidence").fetchone()
            self.assertEqual(row, (json.dumps([eid(RECORDING, 5)]), "kept", 1))  # treated as reviewed: unchanged


if __name__ == "__main__":
    unittest.main()
