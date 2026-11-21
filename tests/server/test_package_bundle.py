"""Package format 3 on the ten-application recording, Orders and Billing: fixture bindings,

visual baselines per screen state, evidence levels, interface contracts between applications

recorded together, and the required "AI generated Twin App" label (twinlab2_contract.package).



The recording is `tests/fixtures/ten-app-session` (synthetic): Billing types an invoice search,

approves and exports; Orders creates an order and types a search. The owner's part goes through

the domain modules the routes call (as in test_ten_apps), the label check through a stub

deployment.

"""



import json

import os

from pathlib import Path

import tempfile

import unittest



from twinlab2 import baselines, buildpack, independent, interfaces, pack_validator, portal, scenarios

from twinlab2.portal import VERSION_PATH

from twinlab2_contract import package

from test_ten_apps import RECORDING, TenAppRecordingTest

from test_verification import Deployment



NOW = "2026-11-21T10:00:00+00:00"





def events_of(db, app_id):

    return json.loads(db.execute("SELECT selection FROM evidence WHERE app_id = ? AND recording_id = ?",

                                 (app_id, RECORDING)).fetchone()[0])





def typed_event(db, app_id):

    for row in db.execute("SELECT flow FROM recording_apps WHERE app_id = ? AND recording_id = ?", (app_id, RECORDING)):

        for step in json.loads(row["flow"]):

            if step["action"] == "type":

                return step["event_id"]

    raise AssertionError("no typing step")





def png():

    """A 1x1 PNG (synthetic)."""

    import struct

    import zlib



    def chunk(kind, body):

        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)



    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b""))





class PackageBundleTest(TenAppRecordingTest):

    # the ten-application tests run in test_ten_apps; this class uses only its setup

    test_ten_applications_get_ten_separate_packages = None

    test_the_download_route_names_the_package_by_its_application = None

    test_the_recording_export_holds_the_packages_the_caller_builds = None



    def setUp(self):

        super().setUp()

        self.by_tag = self.record()

        self.orders, self.billing = self.by_tag["orders"], self.by_tag["billing"]



    def scenario(self, app_id, name, examples, bindings=None):

        events = [scenarios.event_ref(RECORDING, e) for e in events_of(self.db, app_id)]

        values = {"name": name, "starting": "Signed in as a clerk.", "actions": "Do it", "expected": "Done.",

                  "failures": "", "examples": examples, "questions": "", "notes": "", "events": json.dumps(events)}

        if bindings is not None:

            values["bindings"] = json.dumps(scenarios.clean_bindings(bindings, events))

        return scenarios.save(self.db, app_id, values)



    def validation(self, app_id):

        return buildpack.draft(self.db, app_id, VERSION_PATH).validation



    def gap(self, result, item):

        return next((g for g in result["gaps"] if g["item"] == item), None)



    def level(self, result, item):

        return next(i for i in result["items"] if i["item"] == item)["level"]



    # -- 1. bindings ---------------------------------------------------------------------------------



    def test_each_typing_step_is_bound_to_a_fixture_field(self):

        sid = self.scenario(self.billing, "Invoice for an order", "Order number: 1001; Customer: Example GmbH")

        result = self.validation(self.billing)

        gap = self.gap(result, "data.bindings")

        self.assertIsNotNone(gap, "one example line no longer satisfies a typing step")

        self.assertIn("is bound to no fixture field", gap["details"][0])

        # an example that names no step is only documented, so it does not count as covered

        self.assertEqual(self.level(result, "data.examples"), "documented")



        ref = scenarios.event_ref(RECORDING, typed_event(self.db, self.billing))

        scenarios.save(self.db, self.billing, {"bindings": json.dumps(

            scenarios.clean_bindings([{"event": ref, "field": "Order number"}], [ref]))}, sid)

        result = self.validation(self.billing)

        self.assertIsNone(self.gap(result, "data.bindings"))

        self.assertEqual(self.level(result, "data.bindings"), "owner-confirmed")

        data = json.loads(buildpack.draft(self.db, self.billing, VERSION_PATH).manifest)

        self.assertEqual(data["format"], 3)

        bound = data["scenarios"][0]["bindings"]

        self.assertEqual(bound, [{"event_id": ref.split("#")[1], "recording_id": RECORDING, "field": "Order number",

                                  "type": "number", "value": "1001"}])

        task = buildpack.draft(self.db, self.billing, VERSION_PATH).files["BUILD_TASK.md"].decode()

        self.assertIn("field Order number (number): `1001`", task)



    def test_a_binding_must_name_a_linked_event_and_one_target(self):

        events = ["r#e1"]

        with self.assertRaises(ValueError):

            scenarios.clean_bindings([{"event": "r#other", "field": "X"}], events)

        with self.assertRaises(ValueError):

            scenarios.clean_bindings([{"event": "r#e1", "field": "X", "identifier": "y"}], events)

        with self.assertRaises(ValueError):

            scenarios.clean_bindings([{"event": "r#e1", "field": "X"}, {"event": "r#e1", "field": "Y"}], events)

        self.assertEqual(scenarios.clean_bindings([{"event": "r#e1", "identifier": "Order Number"}], events),

                         [{"event": "r#e1", "identifier": "order-number"}])



    # -- 3. cross-app relationships ------------------------------------------------------------------



    def test_an_order_number_typed_in_billing_must_be_one_orders_produces(self):

        self.scenario(self.orders, "Create an order", "Customer: Example GmbH")

        ref = scenarios.event_ref(RECORDING, typed_event(self.db, self.billing))

        self.scenario(self.billing, "Invoice for an order", "Order number: 1001",

                      [{"event": ref, "identifier": "order-number"}])

        gap = self.gap(self.validation(self.billing), "interfaces.identifiers")

        self.assertEqual(gap["severity"], "critical")

        self.assertIn("which no interface contract lists", gap["details"][0])



        # the owner names Orders as the producer of the order number

        doc = interfaces.generate(self.db, RECORDING)

        self.assertIn(self.orders, [a["id"] for a in doc["apps"]])

        doc["identifiers"].append({"name": "order-number", "field": "Order number", "type": "identifier",

                                   "value": "1001", "producer": self.orders, "consumers": [self.billing]})

        doc["relationships"].append({"kind": "identifier", "identifier": "order-number", "producer": self.orders,

                                     "consumer": self.billing, "when_down": ""})

        doc["launch_order"] = [self.orders] + [a for a in doc["launch_order"] if a != self.orders]

        doc["reset_order"] = list(reversed(doc["launch_order"]))

        saved = interfaces.save(self.db, RECORDING, doc, "ana", NOW)

        self.assertTrue(saved["edited"])

        result = self.validation(self.billing)

        self.assertIn("Orders never produces it", " ".join(self.gap(result, "interfaces.identifiers")["details"])

                      .replace("orders.example.test", "Orders"))

        self.assertIsNotNone(self.gap(result, "interfaces.peer_down"))



        # Orders' scenario names the order number: produced; the peer-down behaviour is written

        self.scenario(self.orders, "Order number", "Order number: 1001")

        doc["relationships"][-1]["when_down"] = "Billing shows 'Orders is not available' and keeps the invoice draft."

        interfaces.save(self.db, RECORDING, doc, "ana", NOW, expected_revision=1)

        result = self.validation(self.billing)

        self.assertIsNone(self.gap(result, "interfaces.identifiers"))

        self.assertIsNone(self.gap(result, "interfaces.peer_down"))

        self.assertEqual(self.level(result, "interfaces.identifiers"), "owner-confirmed")

        draft = buildpack.draft(self.db, self.billing, VERSION_PATH)

        data = json.loads(draft.manifest)

        contract = data["interfaces"][0]

        self.assertEqual({a["id"] for a in contract["apps"]}, {self.orders, self.billing},

                         "the package names only the applications it exchanges data with")

        self.assertEqual(contract["launch_order"], [self.orders, self.billing])

        self.assertEqual(contract["reset_order"], [self.billing, self.orders])

        task = draft.files["BUILD_TASK.md"].decode()

        self.assertIn("## Interfaces with other applications", task)

        self.assertIn("Shared identifier `order-number` (identifier): synthetic value `1001`", task)

        # Verify checks what it can: the identifier statically, the peer's deployment

        checks = interfaces.verify(self.db, data, self.billing, lambda url: None)

        self.assertEqual(checks[0]["status"], "pass")

        self.assertIn({"what": "peer orders.example.test", "status": "blocked"},

                      [{k: c[k] for k in ("what", "status")} for c in checks])

        self.assertEqual({c["status"] for c in checks if c["what"].startswith("when ")}, {"not-checked"})



        # a stale save is refused

        with self.assertRaises(interfaces.Stale):

            interfaces.save(self.db, RECORDING, doc, "ana", NOW, expected_revision=1)

        # Billing's own value differs from the shared one: broken

        self.scenario(self.billing, "Second invoice", "Order number: 1002",

                      [{"event": ref, "field": "Order number"}])

        self.assertIn("is `1001`", " ".join(self.gap(self.validation(self.billing), "interfaces.identifiers")["details"]))



    def test_the_generated_contract_finds_shared_fields_and_orders_producers_first(self):

        self.scenario(self.orders, "Create an order", "Order number: 1001")

        self.scenario(self.billing, "Invoice for an order", "Order number: 1001")

        doc = interfaces.generate(self.db, RECORDING)

        ident = next(i for i in doc["identifiers"] if i["name"] == "order-number")

        # Billing's steps reach it first in this recording, so it is inferred as the producer:

        # the owner corrects that in the contract (see the test above)

        self.assertEqual(ident["producer"], self.billing)

        self.assertEqual(ident["consumers"], [self.orders])

        self.assertLess(doc["launch_order"].index(self.billing), doc["launch_order"].index(self.orders))

        self.assertEqual(doc["reset_order"], list(reversed(doc["launch_order"])))

        self.assertEqual(package.check_interfaces(doc), [])



    # -- 2. visual baselines per screen state --------------------------------------------------------



    def test_baselines_cover_one_screen_state_each(self):

        self.scenario(self.billing, "Invoice", "Order number: 1001")

        folder = Path(self.work.name) / "recordings" / RECORDING / "web"

        folder.mkdir(parents=True)

        events = events_of(self.db, self.billing)

        picture = png()

        rows = []

        for n, e in enumerate(events, 1):

            (folder / f"{n}.png").write_bytes(picture)

            rows.append(json.dumps({"seq": n, "event_id": e, "action": "click", "screenshot": f"{n}.png"}))

        (folder / "steps.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")

        old = os.environ.get("TWINLAB2_RECORDINGS")

        os.environ["TWINLAB2_RECORDINGS"] = str(folder.parent.parent)

        self.addCleanup(lambda: os.environ.pop("TWINLAB2_RECORDINGS") if old is None

                        else os.environ.__setitem__("TWINLAB2_RECORDINGS", old))



        needed = baselines.needed(self.db, self.billing)

        self.assertGreaterEqual(len(needed), 2, needed)

        result = self.validation(self.billing)

        self.assertIn(f"0 of {len(needed)} screen state(s)", self.gap(result, "visuals.per_screen")["details"][0])

        first = needed[0]

        candidates = [c for c in baselines.candidates(self.db, self.billing)

                      if c["source"] == "recording" and c["state"] == first["state"]]

        self.assertTrue(candidates)

        source = {"recording_id": RECORDING, "event_id": candidates[0]["event_id"]}

        with self.assertRaises(ValueError):  # a baseline names its viewport, theme and scale

            baselines.approve(self.db, self.billing, {"screen": first["screen"], "state": first["state"],

                                                      "viewport": "big", "theme": "light", "scale": "1",

                                                      "source": source}, "ana", NOW)

        for theme in ("light", "dark"):  # two pictures of one state still cover one state

            baselines.approve(self.db, self.billing, {"screen": first["screen"], "state": first["state"],

                                                      "viewport": "1280x720", "theme": theme, "scale": "1",

                                                      "source": source}, "ana", NOW)

        result = self.validation(self.billing)

        self.assertIn(f"1 of {len(needed)} screen state(s)", self.gap(result, "visuals.per_screen")["details"][0])

        draft = buildpack.draft(self.db, self.billing, VERSION_PATH)

        data = json.loads(draft.manifest)

        self.assertEqual([(b["state"], b["theme"]) for b in data["visual_baselines"]],

                         [(first["state"], "dark"), (first["state"], "light")])

        self.assertIn(data["visual_baselines"][0]["path"], draft.files)

        self.assertIn("## Visual baselines", draft.files["BUILD_TASK.md"].decode())



    # -- 3. evidence levels --------------------------------------------------------------------------



    def test_documented_items_do_not_count_as_covered(self):

        self.scenario(self.billing, "Invoice", "Order number: 1001")

        result = self.validation(self.billing)

        self.assertEqual(self.level(result, "scenario.expected"), "documented")

        self.assertEqual(self.level(result, "screens.structure"), "observed")

        self.assertLess(result["coverage"], result["score"])

        tested = pack_validator.with_tested(result, {"structural": {"status": "pass"}})

        self.assertEqual(self.level(tested, "screens.structure"), "replay-tested")

        self.assertEqual(self.level(tested, "scenario.expected"), "documented")

        with self.db:

            self.db.execute("INSERT INTO item_confirmations(app_id, item, note, confirmed_by, confirmed_at) "

                            "VALUES (?, 'scenario.expected', '', 'ana', ?)", (self.billing, NOW))

        result = self.validation(self.billing)

        self.assertEqual(self.level(result, "scenario.expected"), "owner-confirmed")

        summary = json.loads(buildpack.draft(self.db, self.billing, VERSION_PATH).manifest)["completeness"]

        self.assertEqual(summary["evidence"]["scenario.expected"], "owner-confirmed")

        self.assertIn("coverage", summary)



    # -- the Twin App label ------------------------------------------------------------------------------



    def test_the_package_requires_the_twin_label(self):

        self.scenario(self.billing, "Invoice", "Order number: 1001")

        draft = buildpack.draft(self.db, self.billing, VERSION_PATH)

        data = json.loads(draft.manifest)

        self.assertEqual(data["target"]["twin_label"]["text"], "AI generated Twin App")

        self.assertIn('<meta name="twin-app"', data["target"]["twin_label"]["surfaces"]["web"]["meta"])

        self.assertIn('## Required: the "AI generated Twin App" label', draft.files["BUILD_TASK.md"].decode())

        self.assertIsNone(self.gap(draft.validation, "deploy.twin_label"))

        del data["target"]["twin_label"]

        gap = self.gap(pack_validator.validate(data, draft.files), "deploy.twin_label")

        self.assertEqual(gap["severity"], "critical")





class TwinLabelTest(unittest.TestCase):

    def test_each_surface_has_its_label_and_comparisons_leave_it_out(self):

        page = ('<html><head><meta name="twin-app" content="AI generated Twin App"></head><body>'

                '<div data-twin-app>AI generated Twin App</div><main id="m"><button id="b">Go</button></main></body></html>')

        self.assertTrue(package.html_has_label(page))

        self.assertNotIn("twin", package.strip_label_html(page))

        self.assertFalse(package.html_has_label("<html><body><div data-twin-app>Demo</div></body></html>"))

        tree = {"name": "Billing" + package.TWIN_TITLE_SUFFIX, "children": [

            {"type": "Button", "aid": "ok"}, {"type": "Text", "aid": package.TWIN_AID, "name": package.TWIN_LABEL}]}

        self.assertTrue(package.tree_has_label(tree))

        self.assertEqual(package.strip_label_tree(tree), {"name": "Billing", "children": [{"type": "Button", "aid": "ok"}]})

        self.assertFalse(package.tree_has_label({"name": "Billing", "children": []}))

        host = package.twin_label("mainframe", [1, 3, 24])["surfaces"]["mainframe"]

        self.assertEqual(host["row"], 23, "the label goes on a row the recorded screens leave empty")



    def test_web_structural_comparison_ignores_the_label_and_requires_it(self):

        from twinlab2.replay import fidelity



        recorded = '<html><body><main id="m"><button id="b">Go</button></main></body></html>'

        live = ('<html><head><meta name="twin-app" content="AI generated Twin App"></head><body>'

                '<div data-twin-app>AI generated Twin App <button id="twin-dismiss">Hide</button></div>'

                '<main id="m"><button id="b">Go</button></main></body></html>')

        self.assertTrue(fidelity.compare_dom(recorded, live), "without masking the banner differs")

        self.assertEqual(fidelity.compare_dom(recorded, package.strip_label_html(live)), [])



    def test_verify_checks_the_deployment_shows_the_label(self):

        for labelled in (True, False):

            deployment = Deployment("a" * 40, labelled=labelled)

            try:

                self.assertEqual(portal.page_has_label(deployment.url), labelled)

            finally:

                deployment.close()

        self.assertTrue(portal.twin_label_required({"format": 3}))

        self.assertFalse(portal.twin_label_required({"format": 2}), "a format-2 package predates the label")



    def test_the_replay_is_told_to_require_the_label(self):

        seen = []



        class Runner:

            def replay(self, recording_id, base_url, **kw):

                seen.append(kw)

                return {"status": "pass", "steps": [{"seq": 4, "differences": [{"check": "twin_label"}]}]}



        independent._replay(Runner(), "r", "web", "http://x/", {"twin_label": True})

        self.assertEqual(seen, [{"twin_label": True}])

        report = independent.run(type("R", (Runner,), {"source": lambda self: {}, "reset": lambda self, u: {}})(),

                                 [{"id": "recording:r", "name": "s"}], {}, "http://x/", "", plan={"twin_label": True})

        self.assertEqual(report["scenarios"][0]["replays"][0]["twin_label_missing"], [4])





class FormatTest(unittest.TestCase):

    def test_a_format_2_package_stays_readable(self):

        old = {"format": 2, "app": {"id": 1, "name": "Billing"}, "scenarios": [{"name": "s", "events": []}]}

        new = package.upgrade(old)

        self.assertEqual(new["scenarios"][0]["bindings"], [])

        self.assertEqual((new["visual_baselines"], new["interfaces"]), ([], []))

        with self.assertRaises(ValueError):

            package.upgrade({"format": 9})



    def test_fixture_fields_and_types(self):

        fields = package.fixture_fields(["Order number: 1001; Customer: Example GmbH (synthetic).",

                                         "Email (email): ana@example.test", "Status: Open", "free text"])

        self.assertEqual([(f["field"], f["type"], f["value"]) for f in fields],

                         [("Order number", "number", "1001"), ("Customer", "text", "Example GmbH"),

                          ("Email", "email", "ana@example.test"), ("Status", "text", "Open")])

        self.assertIn("takes an email", package.incompatible({"value": "1001"}, {"type": "email"}))

        self.assertIn("not one of the options",

                      package.incompatible({"value": "Closed"}, {"kind": "select", "option_texts": ["Open", "Paid"]}))

        self.assertIsNone(package.incompatible({"value": "Paid"}, {"kind": "select", "option_texts": ["Open", "Paid"]}))



    def test_incompatible_bindings_are_critical(self):

        data = {"app": {"id": 1}, "target": {"kind": "web"},

                "scenarios": [{"name": "s", "events": [{"event_id": "e1", "action": "type", "label": "mail",

                                                        "screen": "/a", "seq": 1, "recording_id": "r"}],

                               "fixture_fields": [{"field": "Email", "type": "number", "value": "1001"}],

                               "bindings": [{"event_id": "e1", "field": "Email", "value": "1001"}]}],

                "screens": [{"screen": "/a", "states": [{"id": "s1", "events": ["e1"],

                                                          "controls": [{"kind": "input", "id": "mail", "type": "email"}]}]}]}

        gap = next(g for g in pack_validator.validate(data)["gaps"] if g["item"] == "data.binding_types")

        self.assertEqual(gap["severity"], "critical")

        self.assertIn("takes an email", gap["details"][0])





if __name__ == "__main__":

    unittest.main()

