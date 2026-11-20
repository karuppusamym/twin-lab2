"""Recorder contract 1.1: event ids that never change, the migration of references by number, and
how a recording's completeness is shown (a stopped recording is not complete by itself)."""

from contextlib import closing
import copy
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from zipfile import ZipFile

import completeness
import event_ids
import journey
import portal
from portal import create_app
import registry
import scenarios
from test_registry import (DESK, EVENT_1, EVENT_2, EVENT_5, WEB, CV, completeness_report, eid, references,
                           references_1_1)
from support import connect, new_database

RECORDING = "ws-20261119T100000Z-0a1b2c3d"
PARTIAL = completeness_report(
    "partial",
    desktop={"status": "interrupted", "notes": ["UI Automation stopped answering"],
             "gaps": [{"from": "2026-11-19T10:02:00.000Z", "to": "2026-11-19T10:05:00.000Z",
                       "reason": "collector restarted"}]},
)
PARTIAL["network_evidence"] = "incomplete"


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

    def send(self, refs, status=200):
        response = self.client.post("/api/agent/references", json=refs, headers=self.agent)
        self.assertEqual(response.status_code, status, response.data)
        return response.json

    def query(self, sql, *args):
        with closing(connect(self.database)) as db:
            return db.execute(sql, args).fetchall()

    def selection(self, app_id):
        row = self.query("SELECT selection, suggested FROM evidence WHERE app_id = ? AND recording_id = ?",
                         app_id, RECORDING)[0]  # fmt: skip
        return json.loads(row[0]), json.loads(row[1])

    def pack(self, app_id):
        response = self.client.get(f"/apps/{app_id}/build-pack")
        self.assertEqual(response.status_code, 200, response.data)
        with ZipFile(io.BytesIO(response.data)) as archive:
            return json.loads(archive.read("evidence.json")), archive.read("BUILD_TASK.md").decode()


class EventIdTest(Base):
    def test_a_1_1_recording_keeps_the_recorders_event_ids(self):
        self.pair()
        self.send(references_1_1())
        self.assertEqual(self.selection(1), (sorted([EVENT_1, EVENT_2]), []))
        events = json.loads(self.query("SELECT events FROM recordings")[0][0])
        self.assertEqual([(e["event_id"], e["seq"], e["channel"]) for e in events],
                         [(EVENT_1, 1, "desktop"), (EVENT_2, 2, "desktop"), (EVENT_5, 5, "web")])
        self.assertEqual(events[0]["time"], "2026-11-19T10:01:00.000Z")

    def test_ids_survive_a_resend_that_recomputes_seq(self):
        self.pair()
        self.send(references_1_1())
        self.post(f"/recordings/{RECORDING}", app_id="1", event=EVENT_1, notes="Only the search")
        self.post("/apps/1/scenarios", name="Find a case", expected="The case is shown.",
                  event=f"{RECORDING}#{EVENT_1}")
        before, _ = self.pack(1)
        # crash recovery reordered the events: seq (and n) changed, event ids did not
        refs = references_1_1()
        desk = refs["flows"][DESK]
        desk[0].update(n=2, seq=2)
        desk[1].update(n=1, seq=1)
        desk.append({"n": 3, "seq": 3, "event_id": "019a9f1c-0000-7000-8000-000000000003", "channel": "desktop",
                     "captured_at": "2026-11-19T10:03:00.000Z", "action": "click", "control": "Button",
                     "label": "btnSave", "screen": "MainForm"})
        self.send(refs)
        selection, suggested = self.selection(1)
        self.assertEqual(selection, [EVENT_1])  # the owner's review still means the same event
        self.assertEqual(suggested, ["019a9f1c-0000-7000-8000-000000000003"])  # only the new one
        after, task = self.pack(1)
        [event] = after["evidence"][0]["events"]
        self.assertEqual((event["event_id"], event["seq"], event["label"]), (EVENT_1, 2, "txtSearch"))
        self.assertEqual(before["evidence"][0]["events"][0]["seq"], 1)
        self.assertEqual([e["event_id"] for e in after["scenarios"][0]["events"]], [EVENT_1])
        self.assertEqual(after["scenarios"][0]["unresolved_questions"], [])
        self.assertIn(f"(event `{EVENT_1}`)", task)
        # the recording page selects by event id
        page = self.client.get(f"/recordings/{RECORDING}?app_id=1").data.decode()
        self.assertIn(f'value="{EVENT_1}" checked', page)
        self.assertEqual(self.post(f"/recordings/{RECORDING}", app_id="1", event="1").status_code, 400)

    def test_an_older_recorder_gets_ids_derived_once_and_reused(self):
        self.pair()
        self.send(references())  # contract 1.0.0: no event ids
        ids = sorted([eid(RECORDING, 1), eid(RECORDING, 2)])
        self.assertEqual(self.selection(1), (ids, []))
        flows = {json.loads(f)[0]["event_id"] for (f,) in self.query("SELECT flow FROM recording_apps")}
        self.assertTrue(flows <= {eid(RECORDING, 1), eid(RECORDING, 5)})
        self.assertTrue(all(event_ids.is_derived(i) for i in ids))
        self.send(references())  # a resend keeps the stored ids
        self.assertEqual(self.selection(1), (ids, []))
        self.assertIn(completeness.UNKNOWN_OLDER, self.client.get(f"/recordings/{RECORDING}").data.decode())

    def test_an_event_id_appears_once_per_recording(self):
        self.pair()
        refs = references_1_1()
        refs["flows"][WEB][0]["event_id"] = EVENT_1
        self.assertIn("appears twice", self.send(refs, status=400)["error"])

    def test_an_imported_log_keeps_or_derives_ids_and_recovery_matches_by_id(self):
        log = (b'{"seq":1,"action":"click","event_id":"019a9f1c-0000-7000-8000-0000000000aa",'
               b'"session_id":"' + b"b" * 32 + b'"}\n{"seq":2,"action":"submit","session_id":"' + b"b" * 32 + b'"}\n')
        location = self.post("/recordings", name="Imported", log=(io.BytesIO(log), "steps.jsonl")).location
        events = json.loads(self.query("SELECT events FROM recordings")[0][0])
        self.assertEqual([e["event_id"] for e in events], ["019a9f1c-0000-7000-8000-0000000000aa", eid("b" * 32, 2)])
        # a recovered copy renumbered the first event: still the same event
        again = log.replace(b'"seq":1', b'"seq":7')
        self.assertEqual(self.post("/recordings", name="Recovered", log=(io.BytesIO(again), "steps.jsonl")).location,
                         location)
        events = json.loads(self.query("SELECT events FROM recordings")[0][0])
        self.assertEqual(len(events), 2)
        self.assertIn(completeness.UNKNOWN_IMPORTED, self.client.get(location).data.decode())


class MigrationTest(unittest.TestCase):
    def test_references_by_number_become_references_by_event_id(self):
        with tempfile.TemporaryDirectory() as work:
            database = Path(work) / "catalog.db"
            create_app(database)  # the current tables, then rows as an older portal stored them
            flow = [{"n": 1, "action": "type", "label": "txtSearch"}, {"n": 2, "action": "click", "label": "btnOpen"}]
            with closing(connect(database)) as db, db:
                db.execute("INSERT INTO apps(id, name, git_url, requirements) VALUES (1, 'CaseDesk', '', '')")
                db.execute("INSERT INTO recordings VALUES (?, 'Old', ?)", (RECORDING, json.dumps(
                    [{"seq": 1, "source": "CaseDesk", "action": "type"}, {"seq": 2, "source": "CaseDesk", "action": "click"}])))
                db.execute("INSERT INTO recording_apps VALUES (?, 1, ?, 'full', ?)", (RECORDING, DESK, json.dumps(flow)))
                db.execute("INSERT INTO evidence(app_id, recording_id, selection, notes, reviewed, suggested) "
                           "VALUES (1, ?, '[1]', 'kept', 1, '[2]')", (RECORDING,))
                db.execute("INSERT INTO scenarios(app_id, name, events) VALUES (1, 'Find', ?)",
                           (json.dumps([f"{RECORDING}#1", "not-a-ref"]),))
            for _ in range(2):  # migrating twice is harmless
                app = create_app(database)
            with closing(connect(database)) as db:
                events = json.loads(db.execute("SELECT events FROM recordings").fetchone()[0])
                steps = json.loads(db.execute("SELECT flow FROM recording_apps").fetchone()[0])
                link = db.execute("SELECT selection, suggested, notes FROM evidence").fetchone()
                linked = scenarios.load(db, 1)[0]["events"]
                self.assertEqual(event_ids.migrate(db), {"recordings": 0, "recording_apps": 0, "evidence": 0,
                                                         "scenarios": 0})
            one, two = eid(RECORDING, 1), eid(RECORDING, 2)
            self.assertEqual([e["event_id"] for e in events], [one, two])
            self.assertEqual([s["event_id"] for s in steps], [one, two])  # the same id for the same event
            self.assertEqual((json.loads(link[0]), json.loads(link[1]), link[2]), ([one], [two], "kept"))
            self.assertEqual(linked, [f"{RECORDING}#{one}", "not-a-ref"])
            # the migrated links still make a build pack with the same event
            client = app.test_client()
            client.get("/")
            with ZipFile(io.BytesIO(client.get("/apps/1/build-pack").data)) as archive:
                manifest = json.loads(archive.read("evidence.json"))
            self.assertEqual(manifest["scenarios"][0]["events"][0]["label"], "txtSearch")
            self.assertEqual(manifest["scenarios"][0]["events"][0]["event_id"], one)


class CompletenessTest(Base):
    def test_the_summary_sentence(self):
        self.assertEqual(completeness.summary(completeness.clean(PARTIAL)),
                         "Web complete; desktop interrupted 10:02–10:05; network evidence incomplete")
        self.assertEqual(completeness.summary(None), "Completeness unknown (older recorder)")
        self.assertEqual(completeness.label(completeness.clean(PARTIAL)), "Partial: needs attention")
        self.assertEqual(completeness.label(completeness.clean(PARTIAL), accepted=True), "Partial: accepted as it is")
        ranges = completeness.ranges(completeness.clean(PARTIAL), "2026-11-19T10:00:00.000Z", "2026-11-19T10:05:00.000Z")
        desktop = [r for r in ranges if r["channel"] == "desktop"][0]
        self.assertEqual((desktop["gaps"][0]["left"], desktop["gaps"][0]["width"]), (40.0, 60.0))
        self.assertTrue(completeness.in_gap(completeness.clean(PARTIAL), "2026-11-19T10:03:00Z"))
        self.assertTrue(completeness.in_gap(completeness.clean(PARTIAL), "2026-11-19T10:03:00Z", "desktop"))
        self.assertFalse(completeness.in_gap(completeness.clean(PARTIAL), "2026-11-19T10:03:00Z", "web"))
        self.assertFalse(completeness.in_gap(completeness.clean(PARTIAL), "2026-11-19T10:01:00Z"))

    def test_the_recording_page_shows_completeness_preflight_upload_and_preview(self):
        self.pair()
        report = copy.deepcopy(PARTIAL)
        report.update(recovered=True, loss_window_seconds=4)
        refs = references_1_1(report=report)
        refs["preflight"]["checks"].append({"name": "Mainframe host", "ok": False, "detail": "no answer on 3270"})
        refs["preflight"]["ready"] = False
        self.send(refs)
        page = self.client.get(f"/recordings/{RECORDING}").data.decode()
        self.assertIn("Web complete; desktop interrupted 10:02–10:05; network evidence incomplete", page)
        self.assertIn("Partial: needs attention", page)
        self.assertIn("Recovered after the recorder stopped unexpectedly", page)
        self.assertIn("up to 4 seconds before the interruption may be lost", page)
        self.assertIn('class="gap" style="left: 40.0%; width: 60.0%"', page)  # 10:02–10:05 of 10:00–10:05
        self.assertIn("collector restarted", page)
        self.assertIn("UI Automation stopped answering", page)
        self.assertIn("✗ Mainframe host", page)
        self.assertIn("no answer on 3270", page)
        self.assertIn("Not ready", page)
        self.assertIn("Upload progress: not available.", page)
        self.assertIn("Evidence preview", page)
        self.assertIn("txtSearch", page)
        self.assertIn("in an interrupted period", page)  # event 2 was captured at 10:02
        self.assertIn("Partial: needs attention", self.client.get("/recordings").data.decode())

    def test_a_complete_recording_has_no_warning(self):
        self.pair()
        self.send(references_1_1())
        page = self.client.get(f"/recordings/{RECORDING}").data.decode()
        self.assertIn(">Complete</span>", page)
        self.assertNotIn('id="attention"', page)
        self.assertNotIn('id="recovered"', page)
        self.assertIn("✓ UI Automation", page)

    def test_upload_progress_comes_from_the_storage_api_when_there_is_one(self):
        self.app = create_app(self.database)  # the storage API (uploads.py), answering a known progress

        def upload(recording_id):
            return {"state": "receiving", "received_bytes": 4096, "total_bytes": 10240, "last_error": None}

        self.app.view_functions["recording_upload"] = upload

        self.app.testing = True
        self.client = self.app.test_client()
        self.client.get("/")
        with self.client.session_transaction() as session:
            self.token = session["form_token"]
        self.pair()
        self.send(references_1_1())
        page = self.client.get(f"/recordings/{RECORDING}").data.decode()
        self.assertIn("Receiving 40% (4.0 KB of 10.0 KB)", page)
        self.assertIn(f'data-upload-url="/api/recordings/{RECORDING}/upload"', page)
        self.assertEqual(portal.upload_view({"status": "complete"})["text"], "Complete")
        self.assertTrue(portal.upload_view({"state": "confirmed", "received_bytes": 1, "total_bytes": 1})["complete"])
        self.assertIsNone(portal.upload_view({"state": "none", "received_bytes": 0, "total_bytes": 0}))
        self.assertIsNone(portal.upload_view(None))


class AttentionTest(Base):
    def test_a_partial_recording_needs_attention_until_accepted(self):
        self.pair()
        self.send(references_1_1(report=PARTIAL))
        self.post("/apps/1/scenarios", name="Find a case")
        self.post("/apps/1", owner="Pat", purpose="Case lookup demo")
        with closing(connect(self.database)) as db:
            app = db.execute("SELECT * FROM apps WHERE id = 1").fetchone()
            j = journey.for_app(db, app)
        self.post("/apps/1/status", status="known")
        with closing(connect(self.database)) as db:
            j = journey.for_app(db, db.execute("SELECT * FROM apps WHERE id = 1").fetchone())
        # the examples exist, but the recording stopped partial: it needs attention, not "done"
        self.assertEqual(j.stage, "Add examples")
        self.assertEqual(j.next.action, "Review partial recording")
        self.assertEqual(j.next.href, f"/recordings/{RECORDING}")
        self.assertIn("desktop interrupted 10:02–10:05", j.next.hint)
        self.assertIn("Complete recording (Order to invoice is partial)", j.missing)
        self.assertIn("Review partial recording", self.client.get("/apps/1").data.decode())
        page = self.client.get(f"/recordings/{RECORDING}").data.decode()
        self.assertIn('id="attention"', page)
        self.assertIn("A stopped recording is not complete by itself", page)
        # the owner accepts it as it is: the journey moves on
        self.assertEqual(self.post(f"/recordings/{RECORDING}/completeness/accept").status_code, 302)
        with closing(connect(self.database)) as db:
            j = journey.for_app(db, db.execute("SELECT * FROM apps WHERE id = 1").fetchone())
        self.assertEqual(j.stage, "Build")
        self.assertIn("Partial: accepted as it is", self.client.get("/recordings").data.decode())
        # a resend with other completeness needs a fresh look
        failed = copy.deepcopy(PARTIAL)
        failed["overall"] = "failed"
        self.send(references_1_1(report=failed))
        with closing(connect(self.database)) as db:
            j = journey.for_app(db, db.execute("SELECT * FROM apps WHERE id = 1").fetchone())
        self.assertEqual(j.next.action, "Review failed recording")
        # the same completeness again keeps the acceptance
        self.post(f"/recordings/{RECORDING}/completeness/accept")
        self.send(references_1_1(report=failed))
        self.assertIn("Failed: accepted as it is", self.client.get("/recordings").data.decode())

    def test_an_older_recording_does_not_hold_up_the_journey(self):
        self.pair()
        self.send(references())
        self.post("/apps/1/status", status="known")
        self.post("/apps/1", owner="Pat", purpose="Case lookup demo")
        with closing(connect(self.database)) as db:
            j = journey.for_app(db, db.execute("SELECT * FROM apps WHERE id = 1").fetchone())
        self.assertEqual(j.stage, "Review required behaviour")
        self.assertEqual(self.post(f"/recordings/{RECORDING}/completeness/accept").status_code, 404)

    def test_the_journey_function_takes_attention(self):
        facts = dict(status="known", owner="Pat", purpose="Demo", identities=1, machines=1, examples=2,
                     behaviour=True, briefs=0, releases=[])
        self.assertEqual(journey.app_journey(1, **facts).stage, "Build")
        stuck = journey.app_journey(1, **facts, attention=[{"id": "ws-x", "name": "X", "overall": "failed",
                                                             "summary": "Web missing"}])
        self.assertEqual((stuck.stage, stuck.next.action), ("Add examples", "Review failed recording"))


if __name__ == "__main__":
    unittest.main()
