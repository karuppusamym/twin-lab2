"""The package shows the screen texts a recorder at contract 1.4 sends (`states[].texts`), not
only protected fields given as controls (gaps 3 and 8):

- a 3270 state's texts (title, labels, message line, key line with their text; other protected
  text as a shape) are its layout table in "Screens and controls", and the title row comes from
  the text of kind `title`; the completeness check no longer reports the layout as missing;
- a desktop message box's message (a text without a position) is shown as the application's text;
- a 3270 key other than Enter (PF3, PA1, Clear) is named on its transition (gap 20).
"""

import unittest

from twinlab2 import buildpack, pack_validator
from twinlab2_contract import structure as st
from twinlab2_recorder.workstation import structure

from test_pack_content import task
from test_pack_validator import desktop_pack, mainframe_pack


def field(row, col, length, text=None, protected=True, **attrs):
    f = {"row": row, "col": col, "len": length, "protected": protected, "numeric": False,
         "hidden": False, "intensified": False, **attrs}  # fmt: skip
    if text is not None:
        f["text" if protected else "shape"] = text
    return f


# what the 3270 proxy writes for a screen (tn3270.Recorder.screen_step), digit tokens masked
INQUIRY = {"screen": {"id": "scr:abc", "title": "ACCOUNT INQUIRY", "rows": 24, "cols": 80},
           "fields": [
               field(2, 30, 15, "ACCOUNT INQUIRY", intensified=True),
               field(4, 2, 14, "ACCOUNT NUMBER"), field(4, 20, 10, None, protected=False, numeric=True),
               field(6, 2, 8, "NAME . ."), field(6, 12, 30, "PRIYA OKONKWO-HALE"),
               field(23, 2, 40, "ACCOUNT 9999999999 NOT FOUND"),
               field(24, 2, 40, "PF3=EXIT  PA1=RESUME  ENTER=INQUIRE"),
           ]}  # fmt: skip


def recorded_pack():
    """The mainframe pack whose first screen has the state the 1.4 recorder sends, cleaned by the
    portal: input fields as controls, every protected field as a text."""
    data, files = mainframe_pack()
    sc = data["screens"][0]
    events = sc["states"][0]["events"]
    state = st.clean_state(dict(structure.mainframe_state(INQUIRY), events=events))
    sc["states"] = [{"id": sc["states"][0]["id"], **state}]
    sc["transitions"][0]["key"] = "Enter"
    sc["transitions"].append({"from_state": sc["states"][0]["id"], "to": sc["screen"],
                              "to_state": sc["states"][0]["id"], "action": "aid", "control": "key",
                              "label": "Clear", "key": "Clear", "count": 1})  # fmt: skip
    return data, files


class HostTextsTest(unittest.TestCase):
    def setUp(self):
        self.data, self.files = recorded_pack()
        self.text = task(self.data, self.files)

    def test_the_texts_are_the_layout_table(self):
        for line in ("| 2 | 30 | 15 | protected, intensified | title: `ACCOUNT INQUIRY` |",
                     "| 4 | 2 | 14 | protected | label: `ACCOUNT NUMBER` |",
                     "| 4 | 20 | 10 | input, numeric | (input: ACCOUNT NUMBER) |",
                     "| 6 | 12 | 30 | protected | output: shape `XXXXX XXXXXXX-XXXX` |",
                     "| 23 | 2 | 40 | protected | message line: `ACCOUNT 9999999999 NOT FOUND` |",
                     "| 24 | 2 | 40 | protected | key line: `PF3=EXIT PA1=RESUME ENTER=INQUIRE` |"):
            self.assertIn(line, self.text)

    def test_data_never_reaches_the_package(self):
        for word in ("PRIYA", "OKONKWO"):
            self.assertNotIn(word, self.text)

    def test_the_title_row_is_the_title_text(self):
        self.assertIn("title (compared exactly): row 2 holds only `ACCOUNT INQUIRY`", self.text)

    def test_the_layout_is_not_reported_missing(self):
        gaps = {g["item"]: g for g in pack_validator.validate(self.data, self.files)["gaps"]}
        self.assertNotIn("mainframe.protected", gaps)
        data, files = mainframe_pack()  # a 1.3 recording of the same screens: no texts, no layout
        for s in data["screens"]:
            for state in s["states"]:
                state["controls"] = [c for c in state["controls"] if not c.get("protected")]
        self.assertIn("mainframe.protected", {g["item"] for g in pack_validator.validate(data, files)["gaps"]})

    def test_a_key_that_keeps_the_screen_is_a_transition(self):
        self.assertIn("aid key `Clear` key Clear → state 1", self.text)
        self.assertIn("key PF3 → `ACCOUNT INQUIRY` state 1", self.text)


class DesktopMessageTest(unittest.TestCase):
    def test_a_message_box_shows_its_message(self):
        data, files = desktop_pack()
        data["screens"][1]["states"][0]["texts"] = [{"kind": "message", "text": "The case was saved."}]
        text = task(data, files)
        self.assertIn('- message (the application\'s own text, shown as recorded): "The case was saved."', text)
        self.assertNotIn("Layout (every field", text)
        self.assertEqual(buildpack.host_fields(data["screens"][1]["states"][0]),
                         data["screens"][1]["states"][0]["controls"])  # no position: no host field


if __name__ == "__main__":
    unittest.main()
