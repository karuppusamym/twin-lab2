"""Contract 1.3 screen structure through the portal, on the ten-application session: each
application's screens are stored by the same per-application rules as its steps (never on
another application), merged across sends, checked word by word again, selected by the owner's
evidence, redacted, and carried by its package (`screens` in evidence.json, "Screens and
controls" in BUILD_TASK.md). Older contracts (1.0 to 1.2) are accepted as before."""

import json
import unittest

from twinlab2.portal import VERSION_PATH
from twinlab2 import buildpack, packages
from twinlab2_recorder import main as agent_main
from test_ten_apps import EXPECTED, FIXTURES, RECORDING, TenAppRecordingTest, fixture, references


def ids_in(screens):
    """Every identifier a screens block names (controls, regions, grids)."""
    out = set()
    for sc in screens:
        for state in sc["states"]:
            for item in state["controls"] + state["regions"] + state["grids"]:
                for k in ("id", "name"):
                    if item.get(k):
                        out.add(item[k])
    return out


def owner_tag(ident):
    """The application tag an identifier of the fixture starts with (generate.page and tree)."""
    found = [t for t in EXPECTED if ident.startswith(t)]
    return max(found, key=len) if found else None


class ScreenStructureTest(TenAppRecordingTest):
    test_ten_applications_get_ten_separate_packages = None
    test_the_download_route_names_the_package_by_its_application = None
    test_the_recording_export_holds_the_packages_the_caller_builds = None

    def stored(self):
        """{app_id: [screen]} of the ten-app recording."""
        out = {}
        for row in self.db.execute("SELECT app_id, screens FROM recording_apps WHERE recording_id = ?", (RECORDING,)):
            out.setdefault(row["app_id"], []).extend(json.loads(row["screens"]))
        return out

    def take_offer(self):
        self.catalog()
        headers = self.pair()
        self.offer(1)
        return headers

    def test_no_screen_lands_on_another_application(self):
        by_tag = self.record()  # sent in parts, a part again, then whole
        stored = self.stored()
        tags = fixture.tags()
        with_screens = set()
        for tag, app_id in by_tag.items():
            screens = stored.get(app_id, [])
            for ident in ids_in(screens):
                self.assertIn(owner_tag(ident), (None, tag), f"{tag} holds {ident}")
            events = {e for sc in screens for st in sc["states"] for e in st["events"]}
            self.assertLessEqual(events, {e for e, t in tags.items() if t == tag}, tag)
            if screens:
                with_screens.add(tag)
                self.assertTrue(any(owner_tag(i) == tag for i in ids_in(screens)), tag)
        # every application recorded at level full has screens; the mainframe (level steps) none
        self.assertEqual(with_screens, set(EXPECTED) - {"mainframe"})
        # every state of every screen is there once, with all its events, after the four sends
        web_events = {e for e, t in tags.items() if t not in ("contoso", "plain", "kasse", "mainframe")}
        seen = [e for screens in stored.values() for sc in screens for st in sc["states"] for e in st["events"]]
        self.assertEqual(len(seen), len(set(seen)))
        self.assertLessEqual(web_events, set(seen))
        # each finance screen is on its own prefix's application, like its steps
        prefixes = {"ledger": "/finance", "receivable": "/finance/billing", "payroll": "/finance/payroll"}
        for tag, prefix in prefixes.items():
            for sc in stored[by_tag[tag]]:
                self.assertTrue(sc["screen"].split("fin.example.test", 1)[1].startswith(prefix), (tag, sc["screen"]))
        receivable = {sc["screen"] for sc in stored[by_tag["receivable"]]}
        self.assertIn("https://fin.example.test/finance/billing", receivable)
        ledger = {sc["screen"] for sc in stored[by_tag["ledger"]]}
        self.assertIn("https://fin.example.test/finance/{id}/{id}", ledger)  # /finance/billing-archive/2025

    def test_each_package_has_only_its_own_screens_and_no_data(self):
        by_tag = self.record()
        stored = self.stored()
        everyone = {tag: ids_in(stored.get(app_id, [])) for tag, app_id in by_tag.items()}
        for tag, app_id in by_tag.items():
            self.prepare(app_id, tag)
            row = self.freeze(app_id)
            files = buildpack.read(packages.open_package(self.db, row["storage_ref"]))
            evidence = json.loads(files["evidence.json"])
            task = files["BUILD_TASK.md"].decode("utf-8")
            text = json.dumps(evidence, ensure_ascii=False) + task
            for word in ("Jane", "INV-9999", "X99", "Xxxx", "row-"):
                self.assertNotIn(word, text, tag)
            if tag == "mainframe":
                self.assertNotIn("screens", evidence)
                self.assertNotIn("## Screens and controls", task)
                continue
            self.assertTrue(evidence["screens"], tag)
            self.assertIn("## Screens and controls", task)
            for other, ids in everyone.items():
                for ident in ids:
                    if other != tag and owner_tag(ident) == other:
                        self.assertNotIn(ident, text, f"{tag}'s package holds {other}'s {ident}")
            mine = {e["event_id"] for r in evidence["evidence"] for e in r["events"]}
            for sc in evidence["screens"]:
                self.assertNotIn("?", sc["screen"])
                for st in sc["states"]:
                    self.assertLessEqual(set(st["events"]), mine)
        billing = buildpack.read(packages.open_package(self.db, packages.get(self.db, by_tag["billing"], 1)["storage_ref"]))
        task = billing["BUILD_TASK.md"].decode("utf-8")
        self.assertIn("`button#billingSave`", task)
        self.assertIn("disabled", task)
        self.assertIn('"Billing field"', task)  # the input's own <label>
        self.assertIn("grid `table#billingRows`: columns Number, Customer; rows: 1", task)
        self.assertIn("- hidden: `div#billingError`", task)

    def test_the_package_states_follow_the_owners_selection(self):
        by_tag = self.record()
        app_id = by_tag["billing"]
        full = buildpack.screen_structure(self.db, app_id)
        events = sorted({e for sc in full for st in sc["states"] for e in st["events"]})
        self.assertGreater(len(events), 1)
        with self.db:
            self.db.execute("UPDATE evidence SET selection = ?, reviewed = 1 WHERE app_id = ? AND recording_id = ?",
                            (json.dumps(events[:1]), app_id, RECORDING))
        some = buildpack.screen_structure(self.db, app_id)
        self.assertEqual({e for sc in some for st in sc["states"] for e in st["events"]}, set(events[:1]))
        self.assertLess(sum(len(sc["states"]) for sc in some), sum(len(sc["states"]) for sc in full))
        with self.db:
            self.db.execute("UPDATE evidence SET selection = '[]' WHERE app_id = ?", (app_id,))
        self.assertEqual(buildpack.screen_structure(self.db, app_id), [])
        data, _files, _report = buildpack.manifest(self.db, app_id)
        self.assertNotIn("screens", data)  # no structure: evidence.json as before 1.3

    def test_a_screens_block_from_an_older_contract_is_ignored(self):
        headers = self.take_offer()
        doc = references()
        self.send(headers, dict(json.loads(json.dumps(doc)), contract_version="1.2.0"))
        self.assertFalse(any(self.stored().values()))
        self.send(headers, doc)  # the same recording from a 1.3 recorder: now it has screens
        self.assertTrue(any(self.stored().values()))
        payload = self.db.execute("SELECT payload FROM session_refs WHERE recording_id = ?", (RECORDING,)).fetchone()
        self.assertIn("screens", json.loads(payload[0]))

    def test_the_committed_1_2_references_are_still_accepted(self):
        headers = self.take_offer()
        doc = agent_main.contract_references(
            json.loads((FIXTURES / "references-ten-apps-1.2.json").read_text(encoding="utf-8")))
        self.assertEqual(doc["contract_version"], "1.2.0")
        self.assertNotIn("screens", doc)
        self.send(headers, doc)
        self.assertEqual(set(self.assignment()), set(EXPECTED))
        self.assertFalse(any(self.stored().values()))

    def test_the_portal_checks_every_word_again(self):
        headers = self.take_offer()
        doc = references()
        key = "web:https://orders.example.test"
        state = doc["screens"][key][0]["states"][0]
        bad = json.loads(json.dumps(doc))
        bad["screens"][key][0]["states"][0]["controls"].append({"kind": "input", "classes": ["acct4711"]})
        response = self.client.post("/api/agent/references", json=bad, headers=headers)
        self.assertEqual(response.status_code, 400)  # a digit in an identifier: outside the contract
        self.assertTrue(any("classes" in e for e in response.json()["errors"]))
        # inside the schema, outside the label rules: dropped by the portal's own check
        state["controls"].append({"kind": "input", "id": "acct", "label": "Jane Doe Smith Brown",
                                  "option_texts": ["Mr Smith Jones Brown Green"], "classes": ["ok"],
                                  "extra": "dropped"})
        state["regions"].append({"kind": "div", "id": "custPanel", "visible": True, "text": "Mrs Brown"})
        self.send(headers, doc)
        orders = [sc for screens in self.stored().values() for sc in screens if "orders.example.test" in sc["screen"]]
        text = json.dumps(orders)
        for word in ("Jane", "Smith", "Brown", "extra", "dropped"):
            self.assertNotIn(word, text)
        added = next(c for sc in orders for st in sc["states"] for c in st["controls"] if c.get("id") == "acct")
        self.assertEqual(set(added), {"kind", "id", "classes"})
        self.assertEqual(added["classes"], ["ok"])
        # the state changed when it was checked again: it is stored under its new id, and the
        # transitions that named it by the recorder's id were renamed with it
        for sc in orders:
            ids = {st["id"] for st in sc["states"]}
            for t in sc["transitions"]:
                self.assertIn(t["from_state"], ids)

    def test_redaction_covers_the_screen_structure(self):
        by_tag = self.record()
        app_id = by_tag["orders"]
        row = self.db.execute("SELECT key, screens FROM recording_apps WHERE app_id = ? AND recording_id = ?",
                              (app_id, RECORDING)).fetchone()
        screens = json.loads(row["screens"])
        screens[0]["states"][0]["controls"][0]["label"] = "password: hunter"
        with self.db:
            self.db.execute("UPDATE recording_apps SET screens = ? WHERE app_id = ? AND recording_id = ? AND key = ?",
                            (json.dumps(screens), app_id, RECORDING, row["key"]))
        self.prepare(app_id, "orders")
        draft = buildpack.draft(self.db, app_id, VERSION_PATH)
        self.assertIn("Screen structure in evidence.json", [item["item"] for item in draft.redacted])
        self.assertNotIn("hunter", draft.manifest)
        self.assertNotIn("hunter", draft.files["BUILD_TASK.md"].decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
