"""Package completeness (docs/PACKAGE-COMPLETENESS.md, twinlab2.pack_validator).

- the pure validator: a complete pack per surface (web, desktop, mainframe) passes, and removing
  each critical item the surface needs fails with that item's gap;
- the spec document and the code list the same items with the same severities;
- the ten-application session: the web and desktop packages recorded at level full are complete;
  the TN3270 host (level steps) misses its screen layout; the desktop toolkit and the AID keys
  reach the pack;
- the portal: the preview answers with the checklist, a freeze with critical gaps is refused
  (409 incomplete_package) unless the owner writes a reason, which is previewed, audited, kept on
  the revision and carried by the package ("Known gaps").
"""

import io
import json
from pathlib import Path
import re
import unittest
from zipfile import ZipFile

from twinlab2 import buildpack, pack_validator, packages
from twinlab2_contract import package
from twinlab2.portal import VERSION_PATH
from support import FIXTURE_GAPS
from test_roles_audit import Base
from test_ten_apps import RECORDING, TenAppRecordingTest

ROOT = Path(__file__).resolve().parents[2]
REC = "ws-20261121T090000Z-00000001"


def event(n, action, screen, control=None, label=None, key=None):
    out = {"event_id": f"00000000-0000-4000-8000-{n:012d}", "seq": n, "action": action, "recording_id": REC}
    for k, v in (("screen", screen), ("control", control), ("label", label), ("key", key)):
        if v:
            out[k] = v
    return out


def pack(surface, scenario, screens, attachments, target_extra=None, app_extra=None):
    deploy = ({"kind": "local", "notes": "Install locally."} if surface == "desktop" else
              {"kind": "openshift", "name": "demo-1", "files": ["deploy/ocp/demo-1/openshift.yaml"], "port": 8080})
    target = {"kind": surface, "data_backend": {"engine": "sqlite", "source": "chosen", "reason": "x"},
              "data_model": {"files": ["attachments/3.sql"], "tables": ["orders"]}, "deploy": deploy}
    if surface != "desktop":
        target["version_endpoint"] = "/.twinlab/version"
    target["twin_label"] = package.twin_label(surface, [4, 2, 1])
    target.update(target_extra or {})
    typed = next((e for e in scenario["events"] if e["action"] == "type"), None)
    fields = package.fixture_fields(scenario["example_values"])
    if typed and fields:
        scenario["fixture_fields"] = fields
        scenario["bindings"] = [{"event_id": typed["event_id"], "recording_id": REC, "field": fields[0]["field"],
                                 "type": fields[0]["type"], "value": fields[0]["value"]}]
    baselines = [{"screen": sc["screen"], "state": st["id"], "viewport": "1280x720", "theme": "light", "scale": "1",
                  "path": f"baselines/{n}.png", "sha256": "0" * 64, "source": "recording"}
                 for n, (sc, st) in enumerate(((sc, st) for sc in screens for st in sc["states"]), 1)]
    data = {"format": 3, "visual_baselines": baselines, "app": {"id": 1, "name": "Demo", "git_url": "https://example.test/demo.git",
                                 "status": "catalog", "surface": surface, **(app_extra or {})},
            "scenarios": [scenario], "evidence": [{"recording_id": REC, "events": scenario["events"]}],
            "attachments": attachments, "target": target, "screens": screens, "unresolved_questions": []}
    files = {"deploy/ocp/demo-1/openshift.yaml": b"kind: List\n"} if surface != "desktop" else {}
    return data, files


def scenario(events, examples):
    return {"name": "Create an order", "starting_conditions": "Signed in as a clerk; no orders yet.",
            "actions": ["Open the orders", "Create one", "Save it"], "events": events,
            "expected_result": "The order is listed with its number, and still listed after a reload.",
            "failure_cases": ['Saving without an amount shows "Enter an amount".',
                              'A search with no results shows "No orders".',
                              'Saving an order number that already exists shows "Duplicate order".',
                              'When saving times out the app says "Try again"; a retry does not save twice.',
                              "Only clerks can create orders; viewers see the list read-only.",
                              "Cancel closes the dialog and discards the entry."],
            "example_values": examples, "unresolved_questions": [], "notes": ""}


def state(sid, events, controls, grids=()):
    return {"id": sid, "controls": controls, "regions": [], "grids": list(grids), "withheld": 0,
            "events": [e["event_id"] for e in events]}


SCREENSHOTS = [{"path": "attachments/4.png", "kind": "Screen", "title": "List"},
               {"path": "attachments/5.png", "kind": "Screen", "title": "New order"},
               {"path": "attachments/6.png", "kind": "Screen", "title": "Error state and style: fonts, colours"}]


def web_pack():
    a, b = "https://orders.example.test/orders", "https://orders.example.test/orders/new"
    e1, e2, e3 = event(1, "click", a, "button", "New"), event(2, "type", b, "input", "Amount"), \
        event(3, "click", b, "button", "Save")
    screens = [
        {"screen": a, "surface": "web", "states": [state("s1", [e1], [
            {"kind": "button", "tag": "button", "id": "new", "label": "New"}],
            [{"kind": "table", "id": "orders", "columns": ["Number", "Amount"], "rows": "few"}])],
         "transitions": [{"from_state": "s1", "to": b, "to_state": "s2", "action": "click", "count": 1}]},
        {"screen": b, "surface": "web", "states": [state("s2", [e2, e3], [
            {"kind": "input", "tag": "input", "id": "amount", "label": "Amount", "required": True},
            {"kind": "button", "tag": "button", "id": "save", "label": "Save"}])], "transitions": []},
    ]
    attachments = [{"path": "attachments/1.har", "kind": "API"}, {"path": "attachments/3.sql", "kind": "Database"},
                   *SCREENSHOTS]
    return pack("web", scenario([e1, e2, e3], ["Amount: 12.50"]), screens, attachments)


def desktop_pack():
    main, dialog = "OrdersMain", "OrderDialog"
    e1 = event(1, "menu", main, "MenuItem", "New order")
    e2 = event(2, "type", dialog, "Edit", "Amount")
    e3 = event(3, "key", dialog, None, "Amount", key="Ctrl+S")
    screens = [
        {"screen": main, "surface": "desktop", "states": [state("s1", [e1], [
            {"kind": "MenuItem", "id": "mnuNew", "label": "New order"},
            {"kind": "DataGrid", "id": "grdOrders"}], [{"kind": "DataGrid", "id": "grdOrders",
                                                          "columns": ["Number", "Amount"], "rows": "many"}])],
         "transitions": [{"from_state": "s1", "to": dialog, "to_state": "s2", "action": "menu", "count": 1}]},
        {"screen": dialog, "surface": "desktop", "states": [state("s2", [e2, e3], [
            {"kind": "Edit", "id": "txtAmount", "label": "Amount", "required": True},
            {"kind": "Button", "id": "btnSave", "label": "Save"}])], "transitions": []},
    ]
    attachments = [{"path": "attachments/1.md", "kind": "Utility"}, {"path": "attachments/3.sql", "kind": "Database"},
                   *SCREENSHOTS]
    return pack("desktop", scenario([e1, e2, e3], ["Amount: 12.50"]), screens, attachments,
                app_extra={"toolkit": "WinForms"})


def mainframe_pack():
    inquiry, detail = "ACCOUNT INQUIRY", "ACCOUNT DETAIL"
    e1 = event(1, "type", inquiry, None, "ACCOUNT")
    e2 = event(2, "aid", inquiry, None, None, key="Enter")
    e3 = event(3, "aid", detail, None, None, key="PF3")
    field = {"kind": "field", "row": 4, "col": 20, "len": 10, "numeric": True, "label": "ACCOUNT", "enabled": True}
    title = {"kind": "field", "row": 1, "col": 30, "len": 15, "protected": True, "text": inquiry, "intensified": True}
    label = {"kind": "field", "row": 4, "col": 2, "len": 16, "protected": True, "text": "ACCOUNT NUMBER"}
    screens = [
        {"screen": inquiry, "surface": "mainframe", "states": [state("s1", [e1, e2], [title, label, field])],
         "transitions": [{"from_state": "s1", "to": detail, "to_state": "s2", "action": "aid", "key": "Enter",
                          "count": 1}]},
        {"screen": detail, "surface": "mainframe", "states": [state("s2", [e3], [
            dict(title, len=14, text=detail), dict(field, row=2, col=10, len=8, numeric=False, label="NAME")])],
         "transitions": [{"from_state": "s2", "to": inquiry, "to_state": "s1", "action": "aid", "key": "PF3",
                          "count": 1}]},
    ]
    attachments = [{"path": "attachments/3.sql", "kind": "Database"}, *SCREENSHOTS]
    sc = scenario([e1, e2, e3], ["ACCOUNT: 0012345678"])
    sc["failure_cases"].append('An unknown account shows the message "ACCOUNT NOT FOUND".')
    return pack("mainframe", sc, screens, attachments)


PACKS = {"web": web_pack, "desktop": desktop_pack, "mainframe": mainframe_pack}


def first_events(data):
    return data["scenarios"][0]["events"]


def drop_screen_structure(data, files):
    del data["screens"]


def drop_second_screen(data, files):
    data["screens"] = data["screens"][:1]


def empty_controls(data, files):
    for st in data["screens"][1]["states"]:
        st["controls"] = []


def unstate_last_event(data, files):
    data["screens"][1]["states"][0]["events"].pop()


def untarget_last_event(data, files):
    last = first_events(data)[-1]
    for k in ("control", "label"):
        last.pop(k, None)
    last["action"] = "click"


def drop_entry_screen(data, files):
    first_events(data)[0].pop("screen")


def not_a_route(data, files):
    old = data["screens"][0]["screen"]
    for e in first_events(data):
        if e.get("screen") == old:
            e["screen"] = "Orders page"
    data["screens"][0]["screen"] = "Orders page"


def unfield_layout(data, files):
    data["screens"][1]["states"][0]["controls"][0].pop("len")


def untitle_screen(data, files):
    for e in first_events(data):
        if e.get("screen") == "ACCOUNT DETAIL":
            e["screen"] = "scr:0a1b2c3d4e5f"
    data["screens"][1]["screen"] = "scr:0a1b2c3d4e5f"
    data["screens"][0]["transitions"][0]["to"] = "scr:0a1b2c3d4e5f"


def drop_aid_key(data, files):
    first_events(data)[-1].pop("key")


def drop_deploy_files(data, files):
    files.clear()


def set_scenario(**values):
    def change(data, files):
        data["scenarios"][0].update(values)
    return change


def set_target(key, value):
    def change(data, files):
        data["target"][key] = value
    return change


def incompatible_binding(data, files):
    """The bound value no longer fits its control: the control takes an email address."""
    typed = next(e for e in first_events(data) if e["action"] == "type")
    for sc in data["screens"]:
        for st in sc["states"]:
            if typed["event_id"] in st["events"]:
                for c in st["controls"]:
                    if typed.get("label") in (c.get("id"), c.get("name"), c.get("label")):
                        c["type"] = "email"


def unproduced_identifier(data, files):
    """A step bound to a shared identifier no interface contract lists (no producer)."""
    data["scenarios"][0]["bindings"] = [dict(b, identifier="order-number") for b in data["scenarios"][0]["bindings"]]
    for b in data["scenarios"][0]["bindings"]:
        b.pop("field", None)


# per critical item: a change that removes it from a complete pack
REMOVALS = {
    "scenario.present": lambda d, f: d.update(scenarios=[]),
    "scenario.events": set_scenario(events=[]),
    "scenario.links_current": set_scenario(unresolved_questions=[
        f"Linked event {REC}#00000000-0000-4000-8000-000000000009 is no longer selected for this app."]),
    "scenario.expected": set_scenario(expected_result=""),
    "screens.structure": drop_screen_structure,
    "screens.referenced": drop_second_screen,
    "screens.controls": empty_controls,
    "screens.states": unstate_last_event,
    "screens.targets": untarget_last_event,
    "navigation.entry": drop_entry_screen,
    "data.examples": set_scenario(example_values=[]),
    "data.backend": set_target("data_backend", {}),
    "web.routes": not_a_route,
    "mainframe.layout": unfield_layout,
    "mainframe.title": untitle_screen,
    "mainframe.aid": drop_aid_key,
    "deploy.target": drop_deploy_files,
    "deploy.version": lambda d, f: d["target"].pop("version_endpoint"),
    "data.binding_types": incompatible_binding,
    "interfaces.identifiers": unproduced_identifier,
    "deploy.twin_label": lambda d, f: d["target"].pop("twin_label"),
}


def critical_items(surface):
    return {i.id for i in pack_validator.SPEC if i.severity == "critical" and surface in i.surfaces}


class ValidatorTest(unittest.TestCase):
    def test_a_complete_pack_of_each_surface_passes(self):
        for surface, make in PACKS.items():
            with self.subTest(surface):
                result = pack_validator.validate(*make())
                self.assertEqual(result["surface"], surface)
                self.assertTrue(result["passed"], result["gaps"])
                self.assertEqual(result["gaps"], [], [g["item"] for g in result["gaps"]])
                self.assertEqual(result["score"], 100)
                self.assertEqual(result["counts"], {"critical": 0, "important": 0, "nice": 0})

    def test_every_critical_item_removed_fails_with_its_gap(self):
        for surface, make in PACKS.items():
            self.assertEqual(critical_items(surface) - set(REMOVALS), set(), f"{surface}: a critical item is not tested")
            for item in sorted(critical_items(surface)):
                if surface == "desktop" and item == "deploy.target":
                    continue  # a desktop pack has no deployment files to lose: local install
                with self.subTest(surface=surface, item=item):
                    data, files = make()
                    REMOVALS[item](data, files)
                    result = pack_validator.validate(data, files)
                    self.assertFalse(result["passed"])
                    gap = next((g for g in result["gaps"] if g["item"] == item), None)
                    self.assertIsNotNone(gap, [g["item"] for g in result["gaps"]])
                    self.assertEqual(gap["severity"], "critical")
                    self.assertTrue(gap["why"] and gap["fix"]["action"] and gap["fix"]["page"])
                    self.assertTrue(gap["fix"]["href"].startswith("/"))
                    self.assertTrue(gap["details"])
                    self.assertLess(result["score"], 100)

    def test_gaps_say_which_scenario_screen_or_event_and_where_to_fix(self):
        data, files = web_pack()
        set_scenario(expected_result="")(data, files)
        gap = next(g for g in pack_validator.validate(data, files)["gaps"] if g["item"] == "scenario.expected")
        self.assertEqual(gap["details"], ['Scenario "Create an order" does not state its expected result.'])
        self.assertEqual(gap["fix"], {"page": "Scenarios", "href": "/apps/1#scenarios",
                                      "action": "Write the expected result."})

    def test_important_and_nice_gaps_do_not_fail_the_pack(self):
        data, files = web_pack()
        data["attachments"] = []
        data["visual_baselines"] = []
        data["scenarios"][0]["bindings"] = []
        data["target"]["data_model"] = {"files": [], "tables": []}
        data["app"]["git_url"] = ""
        set_scenario(failure_cases=[], starting_conditions="", expected_result="The order is listed.")(data, files)
        result = pack_validator.validate(data, files)
        self.assertTrue(result["passed"])
        self.assertEqual({g["item"] for g in result["gaps"]},
                         {"web.api", "visuals.screens", "visuals.per_screen", "visuals.style", "data.model",
                          "deploy.repository", "scenario.failures", "scenario.starting", "scenario.messages",
                          "behaviour.invalid_input", "behaviour.empty_results", "behaviour.retries",
                          "behaviour.duplicates", "behaviour.persistence", "behaviour.unrecorded", "data.bindings"})
        self.assertEqual(result["counts"]["critical"], 0)
        self.assertTrue(all(g["severity"] != "critical" for g in result["gaps"]))
        api = next(g for g in result["gaps"] if g["item"] == "web.api")
        self.assertIn("HAR", api["capture_gap"])

    def test_withheld_identifiers_are_shown_per_screen(self):
        data, files = web_pack()
        data["screens"][1]["states"][0]["withheld"] = 2
        gap = next(g for g in pack_validator.validate(data, files)["gaps"] if g["item"] == "screens.withheld")
        self.assertEqual(gap["severity"], "important")
        self.assertIn("state 1: 2 identifier(s)", gap["details"][0])
        self.assertIn("ident()", gap["capture_gap"])

    def test_scores_per_verification_dimension(self):
        data, files = web_pack()
        data["attachments"] = [a for a in data["attachments"] if a["kind"] != "Screen"]
        data["visual_baselines"] = []
        result = pack_validator.validate(data, files)
        dims = {d["id"]: d for d in result["dimensions"]}
        self.assertEqual(list(dims), list(pack_validator.DIMENSIONS))
        self.assertLess(dims["visual"]["score"], 100)
        self.assertEqual((dims["behavioural"]["score"], dims["structural"]["score"], dims["deployment"]["score"]),
                         (100, 100, 100))
        self.assertIn("visuals.per_screen", dims["visual"]["missing"])
        self.assertTrue(all(g["dimension"] == "visual" for g in result["gaps"]))
        summary = pack_validator.summary(result)
        self.assertEqual(summary["dimensions"]["deployment"], 100)

    def test_happy_path_only_scenarios_miss_business_behaviour(self):
        data, files = web_pack()
        set_scenario(failure_cases=[], expected_result="The order is listed.")(data, files)
        gaps = {g["item"]: g for g in pack_validator.validate(data, files)["gaps"]}
        for item in ("behaviour.invalid_input", "behaviour.empty_results", "behaviour.retries",
                     "behaviour.duplicates", "behaviour.persistence"):
            self.assertEqual(gaps[item]["severity"], "important", item)
        data["screens"][1]["states"][0]["controls"].append({"kind": "button", "tag": "button", "id": "cancel",
                                                            "label": "Cancel"})
        self.assertIn("behaviour.cancellation", {g["item"] for g in pack_validator.validate(data, files)["gaps"]})

    def test_items_that_do_not_apply_are_not_counted(self):
        data, files = web_pack()
        for sc in data["screens"]:
            for st in sc["states"]:
                st["grids"] = []
        result = pack_validator.validate(data, files)
        screens = next(s for s in result["sections"] if s["id"] == "screens")
        self.assertIn("screens.grids", screens["not_applicable"])
        self.assertEqual(screens["score"], 100)

    def test_sign_in_needs_who_is_signed_in(self):
        data, files = web_pack()
        data["screens"][0]["states"][0]["controls"].append({"kind": "input", "tag": "input", "type": "password",
                                                            "id": "pw", "label": "Password"})
        set_scenario(starting_conditions="No orders yet.")(data, files)
        self.assertIn("access.signin", {g["item"] for g in pack_validator.validate(data, files)["gaps"]})
        set_scenario(starting_conditions="Signed in as a clerk.")(data, files)
        self.assertNotIn("access.signin", {g["item"] for g in pack_validator.validate(data, files)["gaps"]})

    def test_a_desktop_menu_step_needs_menu_controls_and_a_key_step_its_key(self):
        data, files = desktop_pack()
        data["screens"][0]["states"][0]["controls"] = [{"kind": "DataGrid", "id": "grdOrders"}]
        first_events(data)[-1].pop("key")
        data["app"].pop("toolkit")
        items = {g["item"] for g in pack_validator.validate(data, files)["gaps"]}
        self.assertTrue({"desktop.menus", "desktop.shortcuts", "desktop.toolkit"} <= items, items)

    def test_the_markdown_lists_what_is_present_and_missing(self):
        data, files = web_pack()
        data["attachments"] = []
        text = "\n".join(pack_validator.markdown(pack_validator.validate(data, files)))
        self.assertIn("## Completeness", text)
        self.assertIn("- present (documented): Expected result of each scenario", text)
        self.assertIn("do not count as covered", text)
        self.assertIn("- missing (important): API calls (HAR or API description).", text)
        known = {"reason": "Shown to the builder.", "gaps": [{"item": "x", "title": "Entry screen"}]}
        text = "\n".join(pack_validator.markdown(pack_validator.validate(data, files), known))
        self.assertIn("## Known gaps", text)
        self.assertIn("> Shown to the builder.", text)


class SpecDocumentTest(unittest.TestCase):
    def test_the_document_lists_every_item_with_its_severity_and_surfaces(self):
        doc = (ROOT / "docs" / "PACKAGE-COMPLETENESS.md").read_text(encoding="utf-8")
        rows = {m.group(1): (m.group(3).strip(), m.group(4), m.group(5)) for m in
                re.finditer(r"^\| `([a-z]+\.[a-z_]+)` \| ([^|]+) \| ([^|]+) \| (\w+) \| (\w+) \|", doc, re.M)}
        expected = {i.id: ("all" if i.surfaces == pack_validator.SURFACES else ", ".join(i.surfaces), i.severity,
                           pack_validator.dimension_of(i)) for i in pack_validator.SPEC}
        self.assertEqual(rows, expected)

    def test_every_capture_gap_is_documented(self):
        doc = (ROOT / "docs" / "PACKAGE-COMPLETENESS.md").read_text(encoding="utf-8")
        section = doc.split("## Capture gaps", 1)[1]
        for item in pack_validator.SPEC:
            if item.capture_gap:
                self.assertIn(f"`{item.id}`", section)


class PackBuilderTest(unittest.TestCase):
    def test_mainframe_aid_keys_are_named_keys(self):
        for key in ("Enter", "PF3", "PF12", "PF24", "PA1", "Clear", "F5", "Ctrl+S"):
            self.assertTrue(buildpack.NAMED_KEY.match(key), key)
        for key in ("a", "1234", "PF", "Hello"):
            self.assertFalse(buildpack.NAMED_KEY.match(key), key)


class TenAppCompletenessTest(TenAppRecordingTest):
    test_ten_applications_get_ten_separate_packages = None
    test_the_download_route_names_the_package_by_its_application = None
    test_the_recording_export_holds_the_packages_the_caller_builds = None

    def test_each_surface_of_the_session(self):
        by_tag = self.record()
        results = {}
        for tag in ("billing", "contoso", "mainframe"):
            self.prepare(by_tag[tag], tag)
            draft = buildpack.draft(self.db, by_tag[tag], VERSION_PATH)
            results[tag] = (draft, json.loads(draft.manifest))
        web, desktop, host = (results[t][0].validation for t in ("billing", "contoso", "mainframe"))
        self.assertEqual((web["surface"], desktop["surface"], host["surface"]), ("web", "desktop", "mainframe"))
        self.assertTrue(web["passed"], [g["item"] for g in web["gaps"] if g["severity"] == "critical"])
        self.assertTrue(desktop["passed"], [g["item"] for g in desktop["gaps"] if g["severity"] == "critical"])
        self.assertFalse(host["passed"])
        self.assertEqual({g["item"] for g in pack_validator.critical(host)},
                         {"screens.structure", "screens.referenced", "mainframe.layout"})
        # what the pack builder now copies: the toolkit, the AID key, the version endpoint
        manifest = results["contoso"][1]
        self.assertEqual(manifest["app"]["toolkit"], "winforms")
        self.assertNotIn("desktop.toolkit", {g["item"] for g in desktop["gaps"]})
        host_manifest = results["mainframe"][1]
        self.assertIn("Enter", {e.get("key") for r in host_manifest["evidence"] for e in r["events"]})
        self.assertEqual(results["billing"][1]["target"]["version_endpoint"], "/" + VERSION_PATH)
        self.assertNotIn("version_endpoint", manifest["target"])
        # evidence.json and BUILD_TASK.md carry the result
        for tag, (draft, data) in results.items():
            self.assertEqual(data["completeness"]["score"], draft.validation["score"], tag)
            self.assertIn("## Completeness", draft.files["BUILD_TASK.md"].decode("utf-8"), tag)
            self.assertNotIn("known_gaps", data)

    def test_the_host_is_frozen_only_with_a_reason(self):
        by_tag = self.record()
        app_id = by_tag["mainframe"]
        self.prepare(app_id, "mainframe")
        draft = buildpack.draft(self.db, app_id, VERSION_PATH)
        with self.assertRaises(packages.Incomplete) as refused:
            with self.db:
                packages.freeze(self.db, app_id, "ana", draft.sha256, "", VERSION_PATH)
        self.assertIn("critical item", str(refused.exception))
        with self.assertRaises(packages.Incomplete):  # a reason too short is no reason
            with self.db:
                packages.freeze(self.db, app_id, "ana", draft.sha256, "", VERSION_PATH, override="ok")
        self.assertIsNone(packages.latest(self.db, app_id))
        reason = "Level steps only: the host layout follows in the next recording."
        with self.assertRaises(packages.DraftChanged):  # approved without the reason: other bytes
            with self.db:
                packages.freeze(self.db, app_id, "ana", draft.sha256, "", VERSION_PATH, override=reason)
        with_reason = buildpack.draft(self.db, app_id, VERSION_PATH, known_gaps=reason)
        with self.db:
            rev = packages.freeze(self.db, app_id, "ana", with_reason.sha256, "", VERSION_PATH, override=reason)
        row = packages.get(self.db, app_id, rev)
        self.assertEqual(row["sha256"], with_reason.sha256)
        validation, known = packages.completeness_of(row)
        self.assertFalse(validation["passed"])
        self.assertEqual(known["reason"], reason)
        self.assertEqual({g["item"] for g in known["gaps"]}, {"screens.structure", "screens.referenced",
                                                              "mainframe.layout"})
        self.assertTrue(row["summary"].startswith("Frozen with 3 known critical gaps: Level steps only"))
        files = buildpack.read(packages.open_package(self.db, row["storage_ref"]))
        task = files["BUILD_TASK.md"].decode("utf-8")
        self.assertIn("## Known gaps", task)
        self.assertIn(reason, task)
        self.assertEqual(json.loads(files["evidence.json"])["known_gaps"]["reason"], reason)
        audit = self.db.execute("SELECT actor, details FROM audit_log WHERE action = 'package.freeze_override'").fetchall()
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0][0], "ana")
        details = json.loads(audit[0][1])
        self.assertEqual((details["reason"], details["rev"], details["sha256"]), (reason, rev, row["sha256"]))


class FreezeOverrideRouteTest(Base):
    """Through the API: an application whose only scenario links no recorded event."""

    def setUp(self):
        super().setUp()
        self.app_id = self.make_app()
        self.scenario(self.app_id, name="Save an invoice", expected="Saved.")
        self.path = f"/api/v1/apps/{self.app_id}/packages"

    def test_the_preview_answers_with_the_checklist(self):
        draft = self.get(f"/api/v1/apps/{self.app_id}/package-draft", "alice").json()
        c = draft["completeness"]
        self.assertFalse(c["passed"])
        self.assertIsNone(draft["known_gaps"])
        gap = next(g for g in c["gaps"] if g["item"] == "scenario.events")
        self.assertEqual(gap["fix"]["href"], f"/apps/{self.app_id}#scenarios")
        self.assertEqual([s["id"] for s in c["sections"]], list(pack_validator.SECTIONS))
        self.assertIn("## Completeness", next(f["text"] for f in draft["files"] if f["path"] == "BUILD_TASK.md"))

    def test_a_freeze_with_critical_gaps_is_refused_without_a_reason(self):
        sha = self.get(f"/api/v1/apps/{self.app_id}/package-draft", "alice").json()["sha256"]
        refused = self.post(self.path, "alice", approve=True, sha256=sha)
        self.assertEqual(refused.status_code, 409)
        body = refused.json()
        self.assertEqual(body["code"], "incomplete_package")
        self.assertIn("scenario.events", {e["field"] for e in body["errors"]})
        self.assertEqual(self.rows("SELECT COUNT(*) FROM builder_packages")[0][0], 0)
        short = self.post(self.path, "alice", approve=True, sha256=sha, override_reason="because")
        self.assertEqual(short.json()["code"], "incomplete_package")

    def test_the_override_is_previewed_audited_and_shown(self):
        draft = self.as_("alice").get(f"/api/v1/apps/{self.app_id}/package-draft",
                                      params={"override_reason": FIXTURE_GAPS}).json()
        self.assertEqual(draft["known_gaps"]["reason"], FIXTURE_GAPS)
        task = next(f["text"] for f in draft["files"] if f["path"] == "BUILD_TASK.md")
        self.assertIn("## Known gaps", task)
        frozen = self.post(self.path, "alice", approve=True, sha256=draft["sha256"], override_reason=FIXTURE_GAPS)
        self.assertEqual(frozen.status_code, 201, frozen.text)
        revision = frozen.json()
        self.assertEqual(revision["sha256"], draft["sha256"])
        self.assertEqual(revision["known_gaps"]["reason"], FIXTURE_GAPS)
        self.assertIn("scenario.events", {g["item"] for g in revision["known_gaps"]["gaps"]})
        self.assertFalse(revision["completeness"]["passed"])
        self.assertIn("known critical gap", revision["summary"])
        builder = self.get(f"/api/v1/apps/{self.app_id}/packages/1", "alice").json()
        self.assertEqual(builder["known_gaps"], revision["known_gaps"])
        self.assertIn("package.freeze_override", self.actions(self.app_id))
        download = self.get(f"/api/v1/apps/{self.app_id}/packages/1/download", "alice")
        with ZipFile(io.BytesIO(download.content)) as archive:
            self.assertIn(FIXTURE_GAPS, archive.read("BUILD_TASK.md").decode("utf-8"))
        # the same draft with the same reason is unchanged
        again = self.post(self.path, "alice", approve=True, sha256=draft["sha256"], override_reason=FIXTURE_GAPS)
        self.assertEqual(again.json()["code"], "unchanged")


if __name__ == "__main__":
    unittest.main()
