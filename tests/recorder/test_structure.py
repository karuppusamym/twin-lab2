"""The screen structure the recorder sends (contract 1.3, twinlab2_recorder.workstation.structure):
web pages from their after-snapshots, desktop windows from their value-free control trees,
3270 screens from their field tables; deduplicated per screen; never a value, a row or a record's
identifier; in the contract's schema; and the references of 1.0 to 1.2 still valid."""

import json
from pathlib import Path
import tempfile
import unittest

from twinlab2_contract import recorder_contract as contract
from twinlab2_contract import structure as st
from twinlab2_recorder import main as agent_main
from twinlab2_recorder.workstation import structure, timeline

try:
    import jsonschema
except ImportError:
    jsonschema = None

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

SEARCH = """<html><body><header id="topbar"></header>
<form id="searchForm"><label for="searchBy">Search by</label>
<select id="searchBy" name="by"><option>Order number</option><option>Customer name</option></select>
<label for="q">Search for</label><input id="q" name="q" required value="">
<input id="note" name="note" readonly value="prefilled">
<button id="searchBtn" type="submit">Search</button></form>
<div id="searchError" class="error" hidden>Not found</div>
<select id="customer" name="customer"><option>Jane Doe</option><option>Acct 1234</option></select>
<table id="results"><thead><tr><th>Number</th><th>Customer</th><th>Total 2026</th></tr></thead>
<tbody>{rows}</tbody></table>
<button id="prevPage" type="button" disabled>Previous</button>
<button id="nextPage" type="button" {next}>Next</button>
<input type="checkbox" name="vat" checked><label>Ship to <input name="ship" type="checkbox"></label>
<div id="cust-ORD-9999">x</div><span class="col-6 card">t</span>
</body></html>"""
ROW = '<tr id="row-ORD-9999"><td>ORD-9999</td><td>Jane Doe</td><td><a class="open" href="#">Jane Doe</a></td></tr>'


def page(rows=0, next_enabled=False):
    return SEARCH.format(rows=ROW * rows, next="" if next_enabled else "disabled")


def clean(raw):
    return st.clean_state(dict(raw, events=[]))


class WebStateTest(unittest.TestCase):
    def test_controls_states_regions_and_grids(self):
        state = clean(structure.web_state(page()))
        by_id = {c.get("id") or c.get("name"): c for c in state["controls"]}
        self.assertEqual(by_id["searchBy"]["option_texts"], ["Order number", "Customer name"])
        self.assertEqual(by_id["searchBy"]["label"], "Search by")
        self.assertTrue(by_id["q"]["required"])
        self.assertEqual(by_id["q"]["value"], "empty")
        self.assertTrue(by_id["note"]["readonly"])
        self.assertEqual(by_id["note"]["value"], "filled")
        self.assertEqual(by_id["searchBtn"]["label"], "Search")
        self.assertFalse(by_id["prevPage"]["enabled"])
        self.assertFalse(by_id["nextPage"]["enabled"])
        self.assertTrue(by_id["vat"]["checked"])
        self.assertFalse(by_id["ship"]["checked"])
        self.assertEqual(by_id["ship"]["label"], "Ship to")  # the wrapping <label>
        # a select whose options are not a code list (a name, a digit): a count only
        self.assertEqual(by_id["customer"]["options"], 2)
        self.assertNotIn("option_texts", by_id["customer"])
        regions = {r["id"]: r for r in state["regions"]}
        self.assertFalse(regions["searchError"]["visible"])
        self.assertEqual(regions["searchError"]["classes"], ["error"])
        self.assertNotIn("cust-ORD-9999", json.dumps(state))
        [grid] = state["grids"]
        self.assertEqual(grid["id"], "results")
        self.assertEqual(grid["columns"], ["Number", "Customer", ""])  # a header with a digit is withheld
        self.assertEqual(grid["rows"], "0")
        self.assertGreater(state["withheld"], 0)

    def test_rows_are_shapes_and_row_controls_one_entry(self):
        for n, shape in ((0, "0"), (1, "1"), (5, "few"), (12, "many")):
            state = clean(structure.web_state(page(n)))
            self.assertEqual(state["grids"][0]["rows"], shape)
            repeated = [c for c in state["controls"] if c.get("classes") == ["open"]]
            self.assertEqual(len(repeated), 1 if n else 0)
            if n:
                self.assertEqual(repeated[0]["repeat"], shape)
                self.assertNotIn("label", repeated[0])  # never a row's text

    def test_no_value_or_record_reaches_the_state(self):
        text = json.dumps(clean(structure.web_state(page(3, True))))
        for leak in ("Jane", "ORD", "9999", "prefilled", "Not found", "Acct", "col-6"):
            self.assertNotIn(leak, text)

    def test_the_same_structure_is_one_state(self):
        a, b = clean(structure.web_state(page(3, True))), clean(structure.web_state(page(4, True)))
        self.assertEqual(st.state_key(a), st.state_key(b))  # 3 and 4 rows: both "few"
        self.assertNotEqual(st.state_key(a), st.state_key(clean(structure.web_state(page(0)))))


TREE = {"type": "Window", "aid": "MainForm", "name": "Jane Doe - Cases", "name_is": "title", "children": [
    {"type": "Edit", "aid": "txtCase", "name": "Case", "name_is": "structure", "value": "Xxxx", "readonly": True},
    {"type": "Edit", "aid": "txtPassword", "password": True},
    {"type": "Button", "aid": "btnSave", "name": "Save", "name_is": "structure", "enabled": False},
    {"type": "Button", "name": "Xxxx Xxx", "name_is": "data"},
    {"type": "CheckBox", "aid": "chkUrgent", "name": "Urgent", "name_is": "structure", "toggle": "On"},
    {"type": "ComboBox", "aid": "cmbStatus", "items_are": "code-list", "children": [
        {"type": "ListItem", "name": "Open", "name_is": "structure"},
        {"type": "ListItem", "name": "Closed", "name_is": "structure"}]},
    {"type": "ComboBox", "aid": "cmbOwner", "items_are": "data", "children": [
        {"type": "ListItem", "name": "Xxxx", "name_is": "data"}]},
    {"type": "Pane", "aid": "pnlDetails", "visible": False},
    {"type": "DataGrid", "aid": "grdCases", "rows": 14, "children": [
        {"type": "Header", "children": [{"type": "HeaderItem", "name": "Case", "name_is": "structure"},
                                        {"type": "HeaderItem", "name": "Opened", "name_is": "structure"}]},
        {"type": "DataItem", "name": "Xxxx", "name_is": "data",
         "children": [{"type": "Button", "aid": "btnRow", "name": "Xxx", "name_is": "data"}]}]}]}


class DesktopStateTest(unittest.TestCase):
    def test_controls_grids_and_states(self):
        state = clean(structure.desktop_state(TREE))
        by_id = {c.get("id"): c for c in state["controls"]}
        self.assertTrue(by_id["txtCase"]["readonly"])
        self.assertEqual(by_id["txtCase"]["value"], "filled")
        self.assertNotIn("value", by_id["txtPassword"])
        self.assertFalse(by_id["btnSave"]["enabled"])
        self.assertEqual(by_id["btnSave"]["label"], "Save")
        self.assertTrue(by_id["chkUrgent"]["checked"])
        self.assertEqual(by_id["cmbStatus"]["option_texts"], ["Open", "Closed"])
        self.assertEqual(by_id["cmbOwner"]["options"], 1)
        self.assertNotIn("option_texts", by_id["cmbOwner"])
        self.assertNotIn("btnRow", by_id)  # inside a record
        self.assertEqual(state["regions"], [{"kind": "Window", "id": "MainForm", "visible": True},
                                            {"kind": "Pane", "id": "pnlDetails", "visible": False}])
        self.assertEqual(state["grids"], [{"kind": "DataGrid", "rows": "many", "id": "grdCases",
                                           "columns": ["Case", "Opened"]}])
        text = json.dumps(state)
        for leak in ("Jane", "Xxxx", " - Cases"):
            self.assertNotIn(leak, text)


class MainframeStateTest(unittest.TestCase):
    def test_fields_with_their_attributes_and_labels(self):
        screen = {"screen": {"id": "scr:abc", "title": "ACCOUNT INQUIRY"}, "fields": [
            {"row": 1, "col": 30, "len": 15, "protected": True, "text": "ACCOUNT INQUIRY"},
            {"row": 3, "col": 2, "len": 10, "protected": True, "text": "ACCOUNT NO"},
            {"row": 3, "col": 13, "len": 8, "protected": False, "numeric": True, "shape": "99999999"},
            {"row": 4, "col": 2, "len": 8, "protected": True, "text": "PASSWORD"},
            {"row": 4, "col": 11, "len": 8, "protected": False, "hidden": True},
            {"row": 5, "col": 2, "len": 10, "protected": True, "text": "JANE DOE"},
            {"row": 6, "col": 2, "len": 4, "protected": False, "intensified": True}]}
        state = clean(structure.mainframe_state(screen))
        fields = state["controls"]
        self.assertEqual([(f["row"], f["col"], f["len"]) for f in fields], [(3, 13, 8), (4, 11, 8), (6, 2, 4)])
        self.assertEqual(fields[0]["label"], "ACCOUNT NO")
        self.assertTrue(fields[0]["numeric"])
        self.assertEqual(fields[0]["value"], "filled")
        self.assertTrue(fields[1]["hidden"])
        self.assertFalse(fields[1]["visible"])
        self.assertTrue(fields[2]["intensified"])
        self.assertNotIn("label", fields[2])  # the protected text before it is on another row
        self.assertNotIn("JANE", json.dumps(state))


class SessionStructureTest(unittest.TestCase):
    def session(self, folder):
        """A web session: a search page seen before and after a search, twice, and a new-order page."""
        origin = "https://orders.example.test"
        (folder / "web" / "dom").mkdir(parents=True)
        steps = []
        pages = [("goto", "/orders/search", None, page()), ("type", "/orders/search", "input#q", page()),
                 ("submit", "/orders/search", "form#searchForm", page(5, True)),
                 ("click", "/orders/search", "button#prevPage", page()),
                 ("submit", "/orders/search", "form#searchForm", page(6, True)),
                 ("click", "/orders/new", "a#newOrder", "<html><body><form id='orderForm'>"
                  "<input id='qty' name='qty'></form></body></html>")]
        for seq, (action, path, selector, html) in enumerate(pages, 1):
            (folder / "web" / "dom" / f"{seq:04d}.html").write_text(html, encoding="utf-8")
            event = f"019aa1b0-{seq:04x}-7000-8000-{seq:012x}"
            steps.append({"seq": seq, "ts": f"2026-11-20T10:00:0{seq}.000Z", "url": origin + path, "action": action,
                          "selector": selector, "dom": f"dom/{seq:04d}.html", "tab": "t1", "origin": origin,
                          "event_id": event, "channel": "web", "captured_at": f"2026-11-20T10:00:0{seq}.000Z"})
        (folder / "web" / "steps.jsonl").write_text("\n".join(json.dumps(s) for s in steps) + "\n", encoding="utf-8")
        (folder / "journal.jsonl").write_text("", encoding="utf-8")
        meta = {"format": timeline.FORMAT, "id": "20261120T100000Z-0a1b2c3d", "name": "Orders",
                "state": "stopped", "started": "2026-11-20T10:00:00.000Z", "targets": [{"origin": origin, "level": "full"}]}
        (folder / "session.json").write_text(json.dumps(meta), encoding="utf-8")

    def test_states_are_deduplicated_and_transitions_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            self.session(folder)
            box = timeline.build(folder)
        self.assertEqual(box["contract_version"], "1.6.0")
        [screens] = box["screens"].values()
        by_screen = {sc["screen"]: sc for sc in screens}
        search = by_screen["https://orders.example.test/orders/search"]
        self.assertEqual(len(search["states"]), 2)  # before and after a search (5 and 6 rows: "few")
        before, after = search["states"]
        self.assertEqual(len(before["events"]), 3)
        self.assertEqual(len(after["events"]), 2)
        trans = {(t["from_state"], t["to_state"], t.get("label")): t["count"] for t in search["transitions"]}
        self.assertEqual(trans[(before["id"], after["id"], "searchForm")], 2)
        self.assertEqual(trans[(after["id"], before["id"], "prevPage")], 1)
        new = by_screen["https://orders.example.test/orders/new"]
        self.assertTrue(any(t["to"] == new["screen"] and t.get("label") == "newOrder" for t in search["transitions"]))
        doc = agent_main.contract_references(box)
        self.assertEqual(contract.errors(doc, "references"), [])
        if jsonschema is not None:
            jsonschema.validate(doc, {**contract.schema(), "$ref": "#/$defs/references_1_1"})

    def test_a_steps_level_application_gets_no_structure(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            self.session(folder)
            meta = json.loads((folder / "session.json").read_text(encoding="utf-8"))
            meta["targets"][0]["level"] = "steps"
            (folder / "session.json").write_text(json.dumps(meta), encoding="utf-8")
            box = timeline.build(folder)
        self.assertNotIn("screens", box)
        self.assertTrue(box["flows"])


class OlderContractsTest(unittest.TestCase):
    def test_the_1_0_to_1_5_references_are_still_valid(self):
        for name, version in (("references-ten-apps-1.1.json", "1.1.0"), ("references-ten-apps-1.2.json", "1.2.0"),
                              ("references-ten-apps-1.3.json", "1.3.0"), ("references-ten-apps-1.4.json", "1.4.0"),
                              ("references-ten-apps-1.5.json", "1.5.0")):
            doc = agent_main.contract_references(json.loads((FIXTURES / name).read_text(encoding="utf-8")))
            self.assertEqual(doc["contract_version"], version)
            self.assertEqual(contract.errors(doc, "references"), [], name)
        old = agent_main.contract_references(json.loads((FIXTURES / "references-from-the-collectors.json")
                                                        .read_text(encoding="utf-8")))
        self.assertEqual(old["contract_version"], "1.0.0")
        self.assertEqual(contract.errors(old, "references"), [])
        self.assertEqual(contract.CONTRACT_VERSION, "1.6.0")
        self.assertEqual(contract.schema()["x-contract-version"], "1.6.0")

    def test_a_1_0_send_carries_no_screens(self):
        box = json.loads((FIXTURES / "references-ten-apps.json").read_text(encoding="utf-8"))
        self.assertIn("screens", box)
        for steps in box["flows"].values():
            for s in steps:
                for k in ("event_id", "channel", "captured_at"):
                    s.pop(k, None)
        self.assertNotIn("screens", agent_main.contract_references(box))

    def test_merge_and_select(self):
        box = json.loads((FIXTURES / "references-ten-apps.json").read_text(encoding="utf-8"))
        screens = box["screens"]["web:https://erp.example.test"]
        self.assertEqual(st.merge(screens, screens), screens)
        events = {e for sc in screens for s in sc["states"] for e in s["events"]}
        one = sorted(events)[0]
        picked = st.select(screens, {one})
        self.assertEqual([e for sc in picked for s in sc["states"] for e in s["events"]], [one])
        self.assertEqual(st.select(screens, set()), [])


if __name__ == "__main__":
    unittest.main()
