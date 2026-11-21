"""Safe editing: owner-edited resources carry a revision and a stale update is refused with 409
`conflict` and the current server version; an owner can remove an application's assignment from
a recording (audited, and the next draft package leaves its events out); the catalog, the
recordings list and a recording's events are paginated."""

import json

from test_completeness import RECORDING, Base
from test_registry import EVENT_1, EVENT_2, EVENT_5, references_1_1

NEW_EVENT = "019a9f1c-0000-7000-8000-000000000003"


class Recorded(Base):
    def setUp(self):
        super().setUp()
        self.pair()
        self.send(references_1_1())
        self.app_id = 1
        self.url = f"/api/v1/recordings/{RECORDING}/assignments/{self.app_id}"

    def conflict(self, response):
        self.assertEqual(response.status_code, 409, response.text)
        self.assertTrue(response.headers["content-type"].startswith("application/problem+json"))
        body = response.json()
        self.assertEqual(body["code"], "conflict")
        return body["current"]


class AssignmentRevisionTest(Recorded):
    def test_a_stale_save_is_refused_with_the_current_version(self):
        first = self.ok(self.client.get(self.url))
        self.assertIn("revision", first)
        # owner A saves on the revision both loaded
        saved = self.ok(self.client.put(self.url, json={"events": [EVENT_1], "notes": "A's notes",
                                                        "expected_revision": first["revision"]}))
        self.assertEqual(saved["revision"], first["revision"] + 1)
        # owner B still holds the first revision: refused, nothing overwritten
        current = self.conflict(self.client.put(self.url, json={
            "events": [EVENT_2], "notes": "B's notes", "expected_revision": first["revision"]}))
        self.assertEqual(current["notes"], "A's notes")
        self.assertEqual(current["selected"], [EVENT_1])
        self.assertEqual(current["revision"], saved["revision"])
        self.assertEqual(self.ok(self.client.get(self.url))["notes"], "A's notes")

    def test_if_match_works_as_expected_revision(self):
        rev = self.ok(self.client.get(self.url))["revision"]
        self.ok(self.client.put(self.url, json={"events": [EVENT_1]}, headers={"If-Match": f'"{rev}"'}))
        self.conflict(self.client.put(self.url, json={"events": [EVENT_2]}, headers={"If-Match": f'"{rev}"'}))

    def test_a_resend_that_suggests_events_is_a_new_revision(self):
        rev = self.ok(self.client.put(self.url, json={"events": [EVENT_1]}))["revision"]
        self.send(references_1_1())  # nothing new: the same revision
        self.assertEqual(self.ok(self.client.get(self.url))["revision"], rev)
        doc = references_1_1()
        flow = doc["flows"][next(k for k in doc["flows"] if k.startswith("desktop"))]
        flow.append(dict(flow[-1], n=3, seq=3, event_id=NEW_EVENT, label="btnClose"))
        doc["timeline"][0]["steps"] = 3
        self.send(doc)  # a new event is suggested: the owner's copy is stale
        after = self.ok(self.client.get(self.url))
        self.assertEqual((after["suggested"], after["revision"]), ([NEW_EVENT], rev + 1))
        self.conflict(self.client.put(self.url, json={"events": [EVENT_1], "expected_revision": rev}))


class ScenarioAndAppRevisionTest(Recorded):
    def test_a_stale_scenario_save_is_refused(self):
        created = self.ok(self.client.post("/api/v1/apps/1/scenarios", json={"name": "Search a case"}), 201)
        url = f"/api/v1/apps/1/scenarios/{created['id']}"
        saved = self.ok(self.client.put(url, json={"name": "Search a case", "expected": "Mine",
                                                   "expected_revision": created["revision"]}))
        self.assertEqual(saved["revision"], created["revision"] + 1)
        current = self.conflict(self.client.put(url, json={"name": "Theirs", "expected_revision": created["revision"]}))
        self.assertEqual(current["expected"], "Mine")
        listed = self.ok(self.client.get("/api/v1/apps/1/scenarios"))["scenarios"][0]
        self.assertEqual((listed["name"], listed["revision"]), ("Search a case", saved["revision"]))

    def test_a_stale_purpose_edit_is_refused(self):
        app = self.ok(self.client.get("/api/v1/apps/1"))
        changed = self.ok(self.client.patch("/api/v1/apps/1", json={"purpose": "Mine",
                                                                    "expected_revision": app["revision"]}))
        self.assertEqual(changed["revision"], app["revision"] + 1)
        current = self.conflict(self.client.patch("/api/v1/apps/1", json={
            "purpose": "Theirs", "expected_revision": app["revision"]}))
        self.assertEqual(current["purpose"], "Mine")
        # a setup change is an edit of the application too
        setup = self.ok(self.client.get("/api/v1/apps/1/setup"))
        self.assertEqual(setup["app"]["revision"], changed["revision"])
        self.conflict(self.client.put("/api/v1/apps/1/data-backend", json={
            "data_backend": "sqlite", "expected_revision": app["revision"]}))
        after = self.ok(self.client.put("/api/v1/apps/1/data-backend", json={
            "data_backend": "sqlite", "expected_revision": changed["revision"]}))
        self.assertEqual(after["app"]["revision"], changed["revision"] + 1)


class RemoveAssignmentTest(Recorded):
    def draft_evidence(self, app_id):
        draft = self.ok(self.client.get(f"/api/v1/apps/{app_id}/package-draft"))
        [evidence] = [f for f in draft["files"] if f["path"] == "evidence.json"]
        return json.loads(evidence["text"])

    def test_removing_is_audited_and_leaves_the_events_out_of_the_next_draft(self):
        saved = self.ok(self.client.put(self.url, json={"events": [EVENT_1, EVENT_2], "notes": "x"}))
        self.ok(self.client.post("/api/v1/apps/1/scenarios", json={"name": "Search a case"}), 201)
        before = self.draft_evidence(1)
        self.assertTrue(any(r["events"] for r in before["evidence"]), before)
        # a stale removal is refused like a stale save
        self.conflict(self.client.delete(self.url + f"?expected_revision={saved['revision'] - 1}"))
        removed = self.ok(self.client.delete(self.url + f"?expected_revision={saved['revision']}"))
        self.assertEqual((removed["removed"], removed["selected"], removed["notes"]), (True, [], ""))
        self.assertEqual(removed["revision"], saved["revision"] + 1)
        [entry] = [e for e in self.ok(self.client.get("/api/v1/apps/1/history"))["entries"]
                   if e["action"] == "evidence.remove"]
        self.assertEqual(entry["object_id"], RECORDING)
        self.assertEqual(sorted(entry["details"]["removed"]), sorted([EVENT_1, EVENT_2]))
        after = self.draft_evidence(1)
        self.assertEqual([r for r in after["evidence"] if r["recording_id"] == RECORDING], [])
        # a resend of the same recording does not bring the selection back
        self.send(references_1_1())
        again = self.ok(self.client.get(self.url))
        self.assertEqual((again["removed"], again["selected"]), (True, []))
        # saving again assigns it again
        self.assertFalse(self.ok(self.client.put(self.url, json={"events": [EVENT_1]}))["removed"])

    def test_removing_twice_is_harmless(self):
        self.ok(self.client.delete(self.url))
        self.assertTrue(self.ok(self.client.delete(self.url))["removed"])


class PaginationTest(Recorded):
    def test_the_catalog_is_paginated_filtered_and_sorted(self):
        for i in range(5):
            self.ok(self.client.post("/api/v1/apps", json={"name": f"Zeta {i}", "purpose": "p"}), 201)
        everything = self.ok(self.client.get("/api/v1/apps"))
        total = everything["page"]["total"]
        page = self.ok(self.client.get("/api/v1/apps?limit=2&offset=0&sort=name"))
        self.assertEqual(len(page["apps"]), 2)
        self.assertEqual(page["page"], {"total": total, "limit": 2, "offset": 0, "next_offset": 2})
        last = self.ok(self.client.get(f"/api/v1/apps?limit=2&offset={total - 1}&sort=name"))
        self.assertEqual((len(last["apps"]), last["page"]["next_offset"]), (1, None))
        names = [a["name"] for a in self.ok(self.client.get("/api/v1/apps?q=zeta&sort=-name"))["apps"]]
        self.assertEqual(names, [f"Zeta {i}" for i in range(4, -1, -1)])
        self.assertEqual(self.ok(self.client.get("/api/v1/apps?status=out_of_scope"))["apps"], [])
        stage = everything["apps"][0]["journey"]["stage"]
        self.assertTrue(all(a["journey"]["stage"] == stage for a in
                            self.ok(self.client.get("/api/v1/apps", params={"stage": stage}))["apps"]))
        self.assertEqual(self.client.get("/api/v1/apps?limit=0").status_code, 400)
        self.assertEqual(self.client.get("/api/v1/apps?sort=nope").status_code, 400)

    def test_recordings_and_their_events_are_paginated(self):
        listed = self.ok(self.client.get("/api/v1/recordings?limit=1"))
        self.assertEqual(listed["page"]["total"], 1)
        events = self.ok(self.client.get(f"/api/v1/recordings/{RECORDING}/events?limit=2"))
        self.assertEqual([e["event_id"] for e in events["events"]], [EVENT_1, EVENT_2])
        self.assertEqual(events["page"], {"total": 3, "limit": 2, "offset": 0, "next_offset": 2})
        rest = self.ok(self.client.get(f"/api/v1/recordings/{RECORDING}/events?limit=2&offset=2"))
        self.assertEqual([e["event_id"] for e in rest["events"]], [EVENT_5])
        found = self.ok(self.client.get(f"/api/v1/recordings/{RECORDING}/events?q=btnopen&ids=true"))
        self.assertEqual(found["ids"], [EVENT_2])
        self.assertEqual(sum(x["count"] for x in events["sources"]), 3)
        source = rest["events"][0]["source"]
        web = self.ok(self.client.get(f"/api/v1/recordings/{RECORDING}/events", params={"source": source}))
        self.assertEqual([e["event_id"] for e in web["events"]], [EVENT_5])


class ScaleTest(Base):
    """A 50-application catalog and a 2,000-event recording (tools/demo_portal.scale_seed): every
    page is small and quick, whatever the size."""

    def test_a_large_catalog_and_recording_stay_paged(self):
        import importlib.util
        from pathlib import Path
        import time

        tool = Path(__file__).resolve().parents[2] / "tools" / "demo_portal.py"
        spec = importlib.util.spec_from_file_location("demo_portal_scale", tool)
        demo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(demo)
        recording = demo.scale_seed(self.client, apps=50, events=2000)

        started = time.perf_counter()
        first = self.ok(self.client.get("/api/v1/apps?limit=20&sort=name"))
        self.assertEqual((first["page"]["total"], len(first["apps"]), first["page"]["next_offset"]), (50, 20, 20))
        self.assertLessEqual(set(demo.SCALE_OWNERS), set(first["filters"]["owners"]))
        owned = self.ok(self.client.get("/api/v1/apps", params={"owner": "ben", "status": "known"}))
        self.assertTrue(owned["apps"] and all("ben" in a["owner"].split(", ") and a["status"] == "known" for a in owned["apps"]))
        furthest = self.ok(self.client.get("/api/v1/apps?sort=-stage"))["apps"]
        self.assertEqual(furthest[-1]["status"], "not_replicated")  # out of scope last, either direction
        self.assertNotEqual(furthest[0]["status"], "not_replicated")
        detail = self.ok(self.client.get(f"/api/v1/recordings/{recording}"))
        self.assertEqual((len(detail["events"]), detail["events_total"]), (200, 2000))
        page = self.ok(self.client.get(f"/api/v1/recordings/{recording}/events?limit=100&offset=1900"))
        self.assertEqual((len(page["events"]), page["page"]["next_offset"]), (100, None))
        found = self.ok(self.client.get(f"/api/v1/recordings/{recording}/events",
                                        params={"q": "submit crm.example.test", "ids": "true"}))
        self.assertEqual(len(found["ids"]), found["page"]["total"])
        self.assertTrue(0 < found["page"]["total"] < 2000)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 10, f"five paged requests took {elapsed:.1f}s")
        # the whole recording selected at once, and removed again
        url = f"/api/v1/recordings/{recording}/assignments/1"
        everything = self.ok(self.client.get(f"/api/v1/recordings/{recording}/events?ids=true&limit=1"))["ids"]
        saved = self.ok(self.client.put(url, json={"events": everything, "expected_revision": 0}))
        self.assertEqual(len(saved["selected"]), 2000)
        self.assertTrue(self.ok(self.client.delete(url + f"?expected_revision={saved['revision']}"))["removed"])
