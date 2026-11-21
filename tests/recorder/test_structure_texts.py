"""Contract 1.4 screen structure from fixture files (no window, no terminal is opened):

- gap 3: a 3270 screen's protected fields with row, column, length and attributes; title,
  labels, message line and key line as text, other protected text (which may be data) as a shape;
- gap 7: a desktop combo box's options from its expand step (its tree is read while it is closed);
- gap 8: a message box (#32770) keeps its message as text and has a screen named by its title.
"""

import json
from pathlib import Path
import tempfile
import unittest

from twinlab2_contract import recorder_contract as contract
from twinlab2_contract import structure as st
from twinlab2_recorder.workstation import flow, structure

EVENT = "01a1fca9-d69a-7024-a914-ece00d6d5392"


def field(row, col, length, text=None, protected=True, **attrs):
    f = {"row": row, "col": col, "len": length, "protected": protected, "numeric": False,
         "hidden": False, "intensified": False, **attrs}  # fmt: skip
    if text is not None:
        f["text" if protected else "shape"] = text
    return f


INQUIRY = {"screen": {"id": "scr:abc", "title": "ACCOUNT INQUIRY", "rows": 24, "cols": 80},
           "fields": [
               field(1, 30, 15, "ACCOUNT INQUIRY", intensified=True),
               field(4, 2, 12, "ACCOUNT NO"), field(4, 15, 8, None, protected=False, numeric=True),
               field(6, 2, 8, "NAME . ."), field(6, 12, 30, "PRIYA OKONKWO-HALE"),
               field(7, 2, 8, "BALANCE:"), field(7, 12, 12, "9,999.99"),
               field(9, 2, 30, "LAST VISIT FROM BRANCH NORTH"),
               field(23, 2, 40, "ACCOUNT 99999 NOT FOUND"),
               field(24, 2, 40, "PF3=EXIT  PF12=CANCEL  ENTER=SEARCH"),
               field(20, 2, 8, None, hidden=True),
           ]}  # fmt: skip


class MainframeTexts(unittest.TestCase):
    def setUp(self):
        self.state = st.clean_state(dict(structure.mainframe_state(INQUIRY), events=[EVENT]))
        self.texts = {(t["row"], t["col"]): t for t in self.state["texts"]}

    def test_every_protected_field_has_its_position(self):
        self.assertEqual(len(self.state["texts"]), 10)
        for t in self.state["texts"]:
            self.assertTrue({"kind", "row", "col", "len"} <= set(t), t)

    def test_title_labels_message_and_keys_keep_their_text(self):
        self.assertEqual(self.texts[(1, 30)], {"kind": "title", "row": 1, "col": 30, "len": 15,
                                               "intensified": True, "text": "ACCOUNT INQUIRY"})  # fmt: skip
        self.assertEqual(self.texts[(4, 2)]["kind"], "label")  # before an input field
        self.assertEqual(self.texts[(6, 2)]["kind"], "label")  # ends as a label does
        self.assertEqual(self.texts[(7, 2)]["text"], "BALANCE:")
        self.assertEqual(self.texts[(23, 2)], {"kind": "message", "row": 23, "col": 2, "len": 40,
                                               "text": "ACCOUNT 99999 NOT FOUND"})  # fmt: skip
        self.assertEqual(self.texts[(24, 2)]["kind"], "keys")
        self.assertEqual(self.texts[(24, 2)]["text"], "PF3=EXIT PF12=CANCEL ENTER=SEARCH")

    def test_text_that_may_be_data_is_a_shape(self):
        self.assertEqual(self.texts[(6, 12)], {"kind": "text", "row": 6, "col": 12, "len": 30,
                                               "shape": "XXXXX XXXXXXX-XXXX"})  # fmt: skip
        self.assertEqual(self.texts[(7, 12)]["shape"], "9,999.99")
        self.assertEqual(self.texts[(9, 2)]["kind"], "text")
        self.assertEqual(self.texts[(20, 2)], {"kind": "text", "row": 20, "col": 2, "len": 8, "hidden": True})
        text = json.dumps(self.state)
        for word in ("PRIYA", "OKONKWO", "NORTH", "BRANCH"):
            self.assertNotIn(word, text)

    def test_the_input_field_is_still_a_control(self):
        self.assertEqual([(c["row"], c["col"], c.get("label")) for c in self.state["controls"]],
                         [(4, 15, "ACCOUNT NO")])

    def test_the_portal_keeps_the_texts_and_checks_them_again(self):
        screens = st.clean_screens({"mainframe:host": [
            {"screen": "ACCOUNT INQUIRY", "surface": "mainframe", "states": [self.state]}]})
        kept = screens["mainframe:host"][0]["states"][0]["texts"]
        self.assertEqual(kept, self.state["texts"])
        bad = dict(self.state, texts=[{"kind": "message", "text": "mail jane@x.test"},
                                      {"kind": "text", "shape": "Jane"},
                                      {"kind": "label", "text": "ACCT 4711"}])  # fmt: skip
        again = st.clean_state(bad)
        self.assertEqual(again["texts"], [{"kind": "label", "text": "ACCT 9999"}])
        self.assertEqual(again["withheld"], self.state["withheld"] + 2)

    def test_the_proxy_keeps_key_names_and_masks_other_digit_tokens(self):
        from twinlab2_recorder.workstation import tn3270

        with tempfile.TemporaryDirectory() as tmp:
            rec = tn3270.Recorder(Path(tmp), "mainframe:h:1")
            try:
                rec.typed.add("PRIYA")
                self.assertEqual(rec._clean("ENTER=SHOW  PF3=SIGN OFF  PA1  F12=CANCEL"),
                                 "ENTER=SHOW PF3=SIGN OFF PA1 F12=CANCEL")
                self.assertEqual(rec._clean("ACCT 10442 FOR PRIYA PF33 X1"), "ACCT 99999 FOR XXXXX XX99 X9")
            finally:
                rec.close()

    def test_a_state_without_texts_keeps_its_1_3_id(self):
        web = st.clean_state({"controls": [{"kind": "input", "id": "q"}], "events": []})
        self.assertNotIn("texts", web)


MESSAGE_BOX = {"type": "Window", "name": "Sign in", "name_is": "title", "cls": "#32770", "children": [
    {"type": "Button", "aid": "2", "name": "OK", "name_is": "structure", "cls": "Button"},
    {"type": "Image", "aid": "20", "cls": "Static"},
    {"type": "Text", "aid": "65535", "name": "Enter your user name and password.", "name_is": "message", "cls": "Static"},
    {"type": "TitleBar", "value": "Xxxx xx"}]}  # fmt: skip


class MessageBox(unittest.TestCase):
    def test_its_message_is_kept_as_text(self):
        state = st.clean_state(dict(structure.desktop_state(MESSAGE_BOX), events=[]))
        self.assertEqual(state["texts"], [{"kind": "message", "text": "Enter your user name and password."}])
        self.assertEqual([(c["kind"], c.get("label"), c.get("id")) for c in state["controls"]], [("Button", "OK", None)])

    def test_data_text_elsewhere_is_still_withheld(self):
        tree = {"type": "Window", "aid": "MainForm", "children": [
            {"type": "Text", "aid": "lblCustomer", "name": "Xxxxx Xxxxxxx", "name_is": "data"}]}
        self.assertNotIn("texts", structure.desktop_state(tree))

    def test_its_screen_is_named_by_its_title(self):
        step = {"window": {"selector": "uia:Name=Sign in;ControlType=Window;ClassName=#32770", "title": "Sign in",
                           "props": {"ControlType": "Window", "Name": "Sign in", "ClassName": "#32770"}},
                "target": {"selector": "uia:AutomationId=2;ControlType=Button", "props": {"Name": "OK"}}}  # fmt: skip
        self.assertEqual(flow.desktop_step(step)["screen"], "Sign in")
        # another window without an AutomationId still gets no screen from its title
        other = json.loads(json.dumps(step))
        other["window"]["props"]["ClassName"] = other["window"]["selector"] = "WindowsForms10.Window"
        self.assertIsNone(flow.desktop_step(other)["screen"])
        # a message box whose title carries digits is not named by it
        other = json.loads(json.dumps(step))
        other["window"]["title"] = "Case 10442"
        self.assertIsNone(flow.desktop_step(other)["screen"])


CASE_FORM = {"type": "Window", "aid": "CaseForm", "children": [
    {"type": "ComboBox", "aid": "cboStatus", "name": "Status", "name_is": "structure", "expand": "collapsed",
     "value": "Xxxxxxxx", "items_are": "data", "children": [
         {"type": "Text", "name": "Xxxxxx", "name_is": "data"},
         {"type": "Button", "name": "Xxxx", "name_is": "data"}]}]}  # fmt: skip


class ComboOptions(unittest.TestCase):
    def rows(self, folder: Path, expand: dict) -> list[dict]:
        (folder / "desktop" / "trees").mkdir(parents=True)
        (folder / "desktop" / "trees" / "0001.json").write_text(json.dumps(CASE_FORM), encoding="utf-8")
        sel = "uia:AutomationId=cboStatus;ControlType=ComboBox"
        return [{"app": "desktop:CaseDesk", "surface": "desktop", "event_id": EVENT,
                 "_step": {"action": "click", "tree": "trees/0001.json", "target": {"selector": sel}}},
                {"app": "desktop:CaseDesk", "surface": "desktop",
                 "_step": dict(expand, action="expand", target={"selector": sel})}]  # fmt: skip

    def test_the_options_come_from_the_expand_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            rows = self.rows(folder, {"items": ["Open", "In progress", "Resolved", "Closed"]})
            combos = structure.combo_lists(folder, rows)
            state = structure.state_of(folder, rows[0], combos)
        combo = state["controls"][0]
        self.assertEqual(combo["options"], 4)
        self.assertEqual(combo["option_texts"], ["Open", "In progress", "Resolved", "Closed"])

    def test_a_list_of_data_is_only_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            rows = self.rows(folder, {"items_are": "data", "item_count": 12})
            state = structure.state_of(folder, rows[0], structure.combo_lists(folder, rows))
        self.assertEqual(state["controls"][0]["options"], 12)
        self.assertNotIn("option_texts", state["controls"][0])

    def test_without_an_expand_step_nothing_changes(self):
        state = st.clean_state(dict(structure.desktop_state(CASE_FORM), events=[]))
        self.assertEqual(state["controls"][0]["options"], 0)


class Version(unittest.TestCase):
    def test_1_4_with_texts_is_in_the_schema(self):
        self.assertGreaterEqual(contract.parse(contract.CONTRACT_VERSION), (1, 4, 0))
        state = st.clean_state(dict(structure.mainframe_state(INQUIRY), events=[EVENT]))
        sid = st.state_key(state)
        doc = {"screens": {"mainframe:host": [{"screen": "ACCOUNT INQUIRY", "surface": "mainframe",
                                               "states": [dict(state, id=sid)], "transitions": []}]}}  # fmt: skip
        found: list[str] = []
        contract._check(doc["screens"]["mainframe:host"][0], {"$ref": "#/$defs/screen"}, "$", found)
        self.assertEqual(found, [])
        bad = json.loads(json.dumps(doc["screens"]["mainframe:host"][0]))
        bad["states"][0]["texts"].append({"kind": "message", "text": "a@b"})
        found = []
        contract._check(bad, {"$ref": "#/$defs/screen"}, "$", found)
        self.assertTrue(found)


if __name__ == "__main__":
    unittest.main()
