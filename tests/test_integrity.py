"""Regression tests for evidence integrity: a resend keeps the owner's reviewed selection, a merge
keeps briefs and combines overlapping evidence links, and web identities are told apart by path."""

from contextlib import closing
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from zipfile import ZipFile

import migrations
import portal
from portal import create_app
import registry
from support import connect, new_database
from test_registry import CV, DESK, WEB, by_number, eid, numbers, references

RECORDING = "ws-20261119T100000Z-0a1b2c3d"


class Base(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.database = new_database(self.work.name)
        self.start()

    def start(self):
        self.app = create_app(self.database)
        self.app.testing = True
        self.client = self.app.test_client()
        self.assertEqual(self.client.get("/").status_code, 200)
        with self.client.session_transaction() as session:
            self.token = session["form_token"]

    def post(self, path, **values):
        return self.client.post(path, data={"token": self.token, **values})

    def pair(self):
        self.post("/machines/pair")
        code = re.search(r'id="pair-code">([A-Z0-9-]+)<', self.client.get("/machines").data.decode()).group(1)
        self.agent = {"Authorization": "Bearer " + self.client.post(
            "/api/agent/pair", json={"contract_version": CV, "code": code, "name": "PC"}).json["token"]}

    def send(self, refs=None):
        response = self.client.post("/api/agent/references", json=refs or references(), headers=self.agent)
        self.assertEqual(response.status_code, 200, response.data)
        return response.json

    def link(self, app_id, recording_id=RECORDING):
        with closing(connect(self.database)) as db:
            row = db.execute("SELECT * FROM evidence WHERE app_id = ? AND recording_id = ?",
                             (app_id, recording_id)).fetchone()
        # stored as event ids; compared by the events' display numbers
        return row and {"selection": numbers(self.database, recording_id, json.loads(row["selection"])),
                        "suggested": numbers(self.database, recording_id, json.loads(row["suggested"])),
                        "notes": row["notes"], "reviewed": row["reviewed"]}

    def assign(self, app_id, events, notes="", recording_id=RECORDING):
        response = self.client.post(f"/recordings/{recording_id}", data={
            "token": self.token, "app_id": str(app_id), "notes": notes,
            "event": by_number(self.database, recording_id, events)})
        self.assertEqual(response.status_code, 302, response.data)

    def merge(self, source, into):
        preview = self.client.get(f"/apps/{source}/merge?into={into}")
        self.assertEqual(preview.status_code, 200)
        page = preview.data.decode()
        fingerprint = re.search(r'name="confirm" value="([0-9a-f]+)"', page)
        return page, fingerprint and self.post(f"/apps/{source}/merge", into=str(into), confirm=fingerprint.group(1))


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
        self.assertIn("new, not yet reviewed", self.client.get(f"/recordings/{RECORDING}?app_id=1").data.decode())
        self.assertIn("1 new, not yet reviewed", self.client.get("/apps/1").data.decode())
        # the build pack has only the reviewed selection
        self.post("/apps/1/scenarios", name="Find a case")
        with ZipFile(io.BytesIO(self.client.get("/apps/1/build-pack").data)) as pack:
            evidence = json.loads(pack.read("evidence.json"))["evidence"]
        self.assertEqual([e["seq"] for e in evidence[0]["events"]], [1])
        # the owner reviews the suggestion
        self.assign(1, [1, 3], notes="Search and save")
        self.assertEqual(self.link(1), {"selection": [1, 3], "suggested": [], "notes": "Search and save", "reviewed": 1})
        self.assertNotIn("new, not yet reviewed", self.client.get(f"/recordings/{RECORDING}?app_id=1").data.decode())

    def test_an_unreviewed_link_follows_the_resend(self):
        self.pair()
        refs = references()
        refs["flows"][DESK] = refs["flows"][DESK][:1]
        self.send(refs)
        self.assertEqual(self.link(1)["selection"], [1])
        self.send()
        self.assertEqual(self.link(1), {"selection": [1, 2], "suggested": [], "notes": "", "reviewed": 0})
        self.assertIn("not yet reviewed", self.client.get("/apps/1").data.decode())


class MergeTest(Base):
    def setUp(self):
        super().setUp()
        self.pair()
        self.send()  # app 1 CaseDesk (events 1, 2), app 2 the web application (event 5)
        self.post("/apps", name="Showcase")  # app 3

    def test_a_merge_keeps_historical_briefs_and_their_packages(self):
        self.post("/apps/2/scenarios", name="Save a case")
        package = self.client.get("/apps/2/build-pack")
        self.assertEqual(package.status_code, 200)
        with closing(connect(self.database)) as db:
            revision = db.execute("SELECT revision FROM briefs WHERE app_id = 2").fetchone()[0]
        # the repro: the merge deleted the brief while its package still referred to it
        page, done = self.merge(2, 3)
        self.assertIn(revision[:12], page)
        self.assertIn("kept with its download", page)
        self.assertIn("Save a case", page)  # its scenario moves too
        self.assertEqual(done.location, "/apps/3")
        self.assertEqual(self.client.get("/apps/2").status_code, 404)
        again = self.client.get(f"/apps/3/briefs/{revision}")
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.data, package.data)
        self.assertEqual(self.link(3)["selection"], [5])
        self.assertIn(WEB, self.client.get("/apps/3/setup").data.decode())

    def test_overlapping_reviewed_links_are_combined_not_dropped(self):
        self.assign(1, [1], notes="Search for the case")
        self.assign(3, [2, 5], notes="Open it and save")
        page, done = self.merge(1, 3)
        self.assertIn("Both applications link this recording", page)
        self.assertIn("1, 2, 5", page)
        self.assertEqual(done.status_code, 302)
        self.assertEqual(self.link(3), {
            "selection": [1, 2, 5], "suggested": [], "reviewed": 1,
            "notes": "Open it and save\n\nMerged from CaseDesk: Search for the case"})
        self.assertIsNone(self.link(1))

    def test_an_unreviewed_link_does_not_widen_a_reviewed_one(self):
        self.assign(3, [1])
        page, done = self.merge(2, 3)  # app 2's link (event 5) was selected automatically
        self.assertIn("new, not yet reviewed", page)
        self.assertEqual(done.status_code, 302)
        self.assertEqual(self.link(3), {"selection": [1], "suggested": [5], "notes": "", "reviewed": 1})

    def test_a_stale_or_missing_confirmation_shows_the_preview(self):
        preview = self.client.get("/apps/1/merge?into=3").data.decode()
        fingerprint = re.search(r'name="confirm" value="([0-9a-f]+)"', preview).group(1)
        self.assign(3, [2])  # the target changed after the preview
        response = self.post("/apps/1/merge", into="3", confirm=fingerprint)
        self.assertEqual(response.location, "/apps/1/merge?into=3")
        self.assertEqual(self.client.get("/apps/1").status_code, 200)
        self.assertEqual(self.post("/apps/1/merge", into="3").location, "/apps/1/merge?into=3")
        self.assertEqual(self.post("/apps/1/merge", into="1").status_code, 400)

    def test_rows_the_merge_cannot_move_block_it(self):
        self.client.post("/apps/1/attachments", data={
            "token": self.token, "title": "Screen", "kind": "Screen", "reviewed": "yes",
            "file": (io.BytesIO(b"\x89PNG"), "screen.png")})
        page, done = self.merge(1, 3)
        self.assertIn("cannot be merged while it has 1 attachments", page)
        self.assertIsNone(done)  # no confirm button
        self.assertEqual(self.post("/apps/1/merge", into="3", confirm="0" * 16).location, "/apps/1/merge?into=3")
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

    def make(self, name, *keys):
        self.post("/apps", name=name)
        with closing(connect(self.database)) as db:
            app_id = db.execute("SELECT MAX(id) FROM apps").fetchone()[0]
        for key in keys:
            self.assertEqual(self.post(f"/apps/{app_id}/identity", key=key).status_code, 302, key)
        return app_id

    def flows(self):
        with closing(connect(self.database)) as db:
            return {(app_id, key): [s["label"] for s in json.loads(flow)] for app_id, key, flow in
                    db.execute("SELECT app_id, key, flow FROM recording_apps")}

    def test_apps_on_one_host_are_told_apart_by_path(self):
        self.pair()
        orders = self.make("Orders", "https://example.test/orders")
        billing = self.make("Billing", "https://example.test/billing")
        self.assertEqual(self.post(f"/apps/{billing}/identity", key="https://example.test/orders/").status_code, 409)
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
        page = self.client.get(f"/recordings/{RECORDING}").data.decode()
        self.assertIn("Orders", page)
        # a session targeting Orders sends the origin to the recorder, with the path
        self.assertEqual(self.post("/sessions", name="Trial", machine_id="1", app=str(orders)).status_code, 302)
        target = self.client.get("/api/agent/sessions", headers=self.agent).json["sessions"][0]["targets"][0]
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
        self.assertIn("Web addresses match", self.client.get(f"/apps/{billing}/setup").data.decode())
        self.assertEqual(self.post(f"/apps/{billing}/url-match", url_match="host").status_code, 302)
        self.send(shared_host_references(["https://example.test/orders/1", "https://example.test/help"]))
        self.assertEqual(self.flows(), {(orders, "web:https://example.test/orders"): ["b1"],
                                        (billing, "web:https://example.test/billing"): ["b2"]})
        # only one application can claim the whole host
        self.assertEqual(self.post(f"/apps/{orders}/url-match", url_match="host").status_code, 409)
        other = self.make("Other")
        self.assertEqual(self.post(f"/apps/{other}/identity", key="https://example.test").status_code, 409)
        self.assertEqual(self.post(f"/apps/{billing}/url-match", url_match="sideways").status_code, 400)

    def test_deny_keys_leave_out_path_identities(self):
        self.pair()
        billing = self.make("Billing", "https://example.test/billing")
        legacy = self.make("Legacy", "https://legacy.example.test")
        self.post(f"/apps/{billing}/status", status="not_replicated")
        self.post(f"/apps/{legacy}/status", status="not_replicated")
        deny = self.client.get("/api/agent/sessions", headers=self.agent).json["deny_keys"]
        self.assertEqual(deny, ["web:https://legacy.example.test"])


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
            app.testing = True
            client = app.test_client()
            client.get("/")
            with client.session_transaction() as session:
                token = session["form_token"]
            client.post("/machines/pair", data={"token": token})
            code = re.search(r'id="pair-code">([A-Z0-9-]+)<', client.get("/machines").data.decode()).group(1)
            agent = {"Authorization": "Bearer " + client.post(
                "/api/agent/pair", json={"contract_version": CV, "code": code, "name": "PC"}).json["token"]}
            refs = references(apps=[{"key": WEB, "level": "full"}])
            refs["flows"] = {WEB: references()["flows"][WEB]}
            refs["timeline"] = [{"app": WEB, "steps": 1}]
            result = client.post("/api/agent/references", json=refs, headers=agent).json
            self.assertEqual(result["mapped"], [1])
            with closing(connect(database)) as db:
                row = db.execute("SELECT selection, notes, reviewed FROM evidence").fetchone()
            self.assertEqual(row, (json.dumps([eid(RECORDING, 5)]), "kept", 1))  # treated as reviewed: unchanged


if __name__ == "__main__":
    unittest.main()
