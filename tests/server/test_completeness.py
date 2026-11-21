"""Recorder contract 1.1: event ids that never change, the migration of references by number, and
how a recording's completeness is shown (a stopped recording is not complete by itself)."""

from contextlib import closing
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from zipfile import ZipFile

from twinlab2 import completeness
from twinlab2 import event_ids
from twinlab2 import journey
from twinlab2 import portal
from twinlab2 import uploads
from twinlab2.portal import create_app
from support import freeze
from twinlab2 import scenarios
from test_registry import (DESK, EVENT_1, EVENT_2, EVENT_5, WEB, CV, completeness_report, eid, references,
                           references_1_1)
from support import Client, connect, new_database

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
        self.client = Client(self.app)
        self.assertEqual(self.client.get("/api/v1/apps").status_code, 200)

    def ok(self, response, status=200):
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def pair(self):
        code = self.ok(self.client.post("/api/v1/machines/pairing-codes"), 201)["code"]
        self.agent = {"Authorization": "Bearer " + self.client.http.post(
            "/api/agent/pair", json={"contract_version": CV, "code": code, "name": "PC"}).json()["token"]}

    def send(self, refs, status=200):
        response = self.client.http.post("/api/agent/references", json=refs, headers=self.agent)
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def recording(self):
        return self.ok(self.client.get(f"/api/v1/recordings/{RECORDING}"))

    def listed(self):
        [found] = [r for r in self.ok(self.client.get("/api/v1/recordings"))["recordings"] if r["id"] == RECORDING]
        return found["status"]["label"]

    def query(self, sql, *args):
        with closing(connect(self.database)) as db:
            return db.execute(sql, args).fetchall()

    def selection(self, app_id):
        row = self.query("SELECT selection, suggested FROM evidence WHERE app_id = ? AND recording_id = ?",
                         app_id, RECORDING)[0]  # fmt: skip
        return json.loads(row[0]), json.loads(row[1])

    def pack(self, app_id):
        response = freeze(self.client, app_id)
        self.assertEqual(response.status_code, 200, response.text)
        with ZipFile(io.BytesIO(response.content)) as archive:
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
        self.ok(self.client.put(f"/api/v1/recordings/{RECORDING}/assignments/1",
                                json={"events": [EVENT_1], "notes": "Only the search"}))
        self.ok(self.client.post("/api/v1/apps/1/scenarios", json={
            "name": "Find a case", "expected": "The case is shown.", "events": [f"{RECORDING}#{EVENT_1}"]}), 201)
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
        # the recording's assignment selects by event id
        assigned = self.ok(self.client.get(f"/api/v1/recordings/{RECORDING}/assignments/1"))
        self.assertEqual(assigned["selected"], [EVENT_1])
        self.assertEqual(self.client.put(f"/api/v1/recordings/{RECORDING}/assignments/1",
                                         json={"events": ["1"]}).status_code, 400)

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
        self.assertEqual(self.recording()["status"]["summary"], completeness.UNKNOWN_OLDER)

    def test_an_event_id_appears_once_per_recording(self):
        self.pair()
        refs = references_1_1()
        refs["flows"][WEB][0]["event_id"] = EVENT_1
        self.assertIn("appears twice", self.send(refs, status=400)["error"])

    def test_an_imported_log_keeps_or_derives_ids_and_recovery_matches_by_id(self):
        log = (b'{"seq":1,"action":"click","event_id":"019a9f1c-0000-7000-8000-0000000000aa",'
               b'"session_id":"' + b"b" * 32 + b'"}\n{"seq":2,"action":"submit","session_id":"' + b"b" * 32 + b'"}\n')
        imported = self.ok(self.client.post("/api/v1/recordings", data={"name": "Imported"},
                                            files={"log": ("steps.jsonl", log, "application/jsonl")}), 201)["id"]
        events = json.loads(self.query("SELECT events FROM recordings")[0][0])
        self.assertEqual([e["event_id"] for e in events], ["019a9f1c-0000-7000-8000-0000000000aa", eid("b" * 32, 2)])
        # a recovered copy renumbered the first event: still the same event
        again = log.replace(b'"seq":1', b'"seq":7')
        recovered = self.ok(self.client.post("/api/v1/recordings", data={"name": "Recovered"},
                                             files={"log": ("steps.jsonl", again, "application/jsonl")}), 201)["id"]
        self.assertEqual(recovered, imported)
        events = json.loads(self.query("SELECT events FROM recordings")[0][0])
        self.assertEqual(len(events), 2)
        self.assertEqual(self.ok(self.client.get(f"/api/v1/recordings/{imported}"))["status"]["summary"],
                         completeness.UNKNOWN_IMPORTED)


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
            with ZipFile(io.BytesIO(freeze(Client(app), 1).content)) as archive:
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
        detail = self.recording()
        status = detail["status"]
        self.assertEqual(status["summary"], "Web complete; desktop interrupted 10:02–10:05; network evidence incomplete")
        self.assertEqual(status["label"], "Partial: needs attention")
        # recovered after the recorder stopped unexpectedly: up to 4 seconds before the interruption may be lost
        self.assertTrue(status["completeness"]["recovered"])
        self.assertEqual(status["completeness"]["loss_window_seconds"], 4)
        [lane] = [lane for lane in detail["refs"]["ranges"] if lane["label"] == "Desktop"]
        [gap] = lane["gaps"]
        self.assertEqual((gap["left"], gap["width"]), (40.0, 60.0))  # 10:02-10:05 of 10:00-10:05
        self.assertEqual(gap["reason"], "collector restarted")
        self.assertIn("UI Automation stopped answering", status["completeness"]["channels"]["desktop"]["notes"])
        self.assertIn({"name": "Mainframe host", "ok": False, "detail": "no answer on 3270"},
                      status["preflight"]["checks"])
        self.assertFalse(status["preflight"]["ready"])  # not ready
        self.assertIsNone(detail["upload"])  # upload progress: not available
        [preview] = [p for p in detail["preview"] if p["app_id"] == 1]  # the evidence preview
        self.assertIn("txtSearch", [e.get("label") for e in preview["events"]])
        in_gap = {e["event_id"]: e["in_gap"] for e in detail["events"]}
        self.assertEqual((in_gap[EVENT_1], in_gap[EVENT_2]), (False, True))  # event 2 was captured at 10:02
        self.assertEqual(self.listed(), "Partial: needs attention")

    def test_a_complete_recording_has_no_warning(self):
        self.pair()
        self.send(references_1_1())
        status = self.recording()["status"]
        self.assertEqual(status["label"], "Complete")
        self.assertFalse(status["attention"])
        self.assertFalse(status["completeness"]["recovered"])
        self.assertIn({"name": "UI Automation", "ok": True, "detail": "available"}, status["preflight"]["checks"])

    def test_upload_progress_comes_from_the_storage_api_when_there_is_one(self):
        def upload(recording_id):  # the storage API (uploads.py), answering a known progress
            return {"state": "receiving", "received_bytes": 4096, "total_bytes": 10240, "last_error": None}

        self.pair()
        self.send(references_1_1())
        with mock.patch.object(uploads, "recording_upload", upload):
            shown = self.recording()["upload"]
        self.assertEqual(shown, {"text": "Receiving 40% (4.0 KB of 10.0 KB)", "percent": 40, "complete": False})
        # the progress the web app polls
        polled = self.client.http.get(f"/api/recordings/{RECORDING}/upload")
        self.assertEqual((polled.status_code, polled.json()["state"]), (200, "none"))
        self.assertEqual(portal.upload_view({"status": "complete"})["text"], "Complete")
        self.assertTrue(portal.upload_view({"state": "confirmed", "received_bytes": 1, "total_bytes": 1})["complete"])
        self.assertIsNone(portal.upload_view({"state": "none", "received_bytes": 0, "total_bytes": 0}))
        self.assertIsNone(portal.upload_view(None))


class AttentionTest(Base):
    def test_a_partial_recording_needs_attention_until_accepted(self):
        self.pair()
        self.send(references_1_1(report=PARTIAL))
        self.ok(self.client.post("/api/v1/apps/1/scenarios", json={"name": "Find a case"}), 201)
        self.ok(self.client.patch("/api/v1/apps/1", json={"owner": "Pat", "purpose": "Case lookup demo"}))
        with closing(connect(self.database)) as db:
            app = db.execute("SELECT * FROM apps WHERE id = 1").fetchone()
            j = journey.for_app(db, app)
        self.ok(self.client.patch("/api/v1/apps/1", json={"status": "known"}))
        with closing(connect(self.database)) as db:
            j = journey.for_app(db, db.execute("SELECT * FROM apps WHERE id = 1").fetchone())
        # the examples exist, but the recording stopped partial: it needs attention, not "done"
        self.assertEqual(j.stage, "Add examples")
        self.assertEqual(j.next.action, "Review partial recording")
        self.assertEqual(j.next.href, f"/recordings/{RECORDING}")
        self.assertIn("desktop interrupted 10:02–10:05", j.next.hint)
        self.assertIn("Complete recording (Order to invoice is partial)", j.missing)
        app_journey = self.ok(self.client.get("/api/v1/apps/1"))["journey"]
        self.assertEqual(app_journey["next"]["action"], "Review partial recording")
        self.assertTrue(self.recording()["status"]["attention"])  # a stopped recording is not complete by itself
        # the owner accepts it as it is: the journey moves on
        accepted = self.ok(self.client.post(f"/api/v1/recordings/{RECORDING}/completeness/accept"))
        self.assertEqual((accepted["status"]["accepted"], accepted["status"]["attention"]), (True, False))
        with closing(connect(self.database)) as db:
            j = journey.for_app(db, db.execute("SELECT * FROM apps WHERE id = 1").fetchone())
        self.assertEqual(j.stage, "Build")
        self.assertEqual(self.listed(), "Partial: accepted as it is")
        # a resend with other completeness needs a fresh look
        failed = copy.deepcopy(PARTIAL)
        failed["overall"] = "failed"
        self.send(references_1_1(report=failed))
        with closing(connect(self.database)) as db:
            j = journey.for_app(db, db.execute("SELECT * FROM apps WHERE id = 1").fetchone())
        self.assertEqual(j.next.action, "Review failed recording")
        # the same completeness again keeps the acceptance
        self.ok(self.client.post(f"/api/v1/recordings/{RECORDING}/completeness/accept"))
        self.send(references_1_1(report=failed))
        self.assertEqual(self.listed(), "Failed: accepted as it is")

    def test_an_older_recording_does_not_hold_up_the_journey(self):
        self.pair()
        self.send(references())
        self.ok(self.client.patch("/api/v1/apps/1", json={"status": "known"}))
        self.ok(self.client.patch("/api/v1/apps/1", json={"owner": "Pat", "purpose": "Case lookup demo"}))
        with closing(connect(self.database)) as db:
            j = journey.for_app(db, db.execute("SELECT * FROM apps WHERE id = 1").fetchone())
        self.assertEqual(j.stage, "Review required behaviour")
        self.assertEqual(self.client.post(f"/api/v1/recordings/{RECORDING}/completeness/accept").status_code, 404)

    def test_the_journey_function_takes_attention(self):
        facts = dict(status="known", owner="Pat", purpose="Demo", identities=1, machines=1, examples=2,
                     behaviour=True, briefs=0, releases=[])
        self.assertEqual(journey.app_journey(1, **facts).stage, "Build")
        stuck = journey.app_journey(1, **facts, attention=[{"id": "ws-x", "name": "X", "overall": "failed",
                                                             "summary": "Web missing"}])
        self.assertEqual((stuck.stage, stuck.next.action), ("Add examples", "Review failed recording"))


if __name__ == "__main__":
    unittest.main()
