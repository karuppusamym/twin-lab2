"""Gap 20: a 3270 attention key other than Enter (PF3, PA1, Clear) is a step of the flow, of the
screen transitions and of what the portal keeps. Fixture records only: the recorder's own 3270
connection is fed scripted host and terminal records (no terminal and no host is started).
"""

import json
from pathlib import Path
import tempfile
import unittest

from twinlab2_contract import recorder_contract as contract
from twinlab2_contract import structure as st
from twinlab2_recorder import main as agent_main
from twinlab2_recorder.workstation import flow, timeline, tn3270

APP = "mainframe:host.example.test:23"


def _attr(protected=False, numeric=False):
    return tn3270.CODE[(0x20 if protected else 0) | (0x10 if numeric else 0)]


def screen(*fields):
    """An Erase/Write: (row, col, text, protected) per field, its attribute byte one column before."""
    out = bytearray([0xF5, 0xC3])
    for row, col, text, protected in fields:
        out += bytes([0x11]) + tn3270.encode_addr((row - 1) * 80 + col - 2)
        out += bytes([0x1D, _attr(protected)])
        out += text.encode("cp037")
    return bytes(out)


SIGN_ON = screen((1, 30, "SIGN ON", True), (4, 2, "USER", True), (4, 10, "        ", False),
                 (24, 2, "PF3=EXIT  CLEAR=REFRESH", True))  # fmt: skip
SIGNED_OFF = screen((1, 30, "SIGNED OFF", True), (24, 2, "PA1=RESUME", True))


def key(aid):
    """A terminal record with no changed field: the AID and, unless it is a short read, the cursor."""
    code = next(b for b, name in tn3270.AIDS.items() if name == aid)
    return bytes([code]) if code in tn3270.SHORT_READ else bytes([code]) + tn3270.encode_addr(329)


def record(folder: Path) -> None:
    rec = tn3270.Recorder(folder / "mainframe", APP)
    conn = tn3270.Connection(rec, 1)
    conn.outbound(SIGN_ON)
    conn.inbound(key("PF3"))
    conn.outbound(SIGNED_OFF)
    conn.inbound(key("PA1"))
    conn.outbound(SIGN_ON)
    conn.inbound(key("Clear"))
    conn.outbound(SIGN_ON)
    rec.close()
    (folder / "journal.jsonl").write_text("", encoding="utf-8")
    meta = {"format": timeline.FORMAT, "id": "20261121T100000Z-0a1b2c3d", "name": "Host", "state": "stopped",
            "started": "2026-11-21T10:00:00.000Z", "targets": [{"tn3270": "host.example.test:23", "level": "full"}]}
    (folder / "session.json").write_text(json.dumps(meta), encoding="utf-8")


class MainframeKeysTest(unittest.TestCase):
    def setUp(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            record(folder)
            self.raw = [json.loads(line) for line in (folder / "mainframe" / "steps.jsonl").read_text(
                encoding="utf-8").splitlines()]  # fmt: skip
            self.box = timeline.build(folder)

    def test_the_recorder_writes_every_key(self):
        self.assertEqual([s["key"] for s in self.raw if s["action"] == "aid"], ["PF3", "PA1", "Clear"])

    def test_the_flow_names_each_key(self):
        [steps] = [s for app, s in self.box["flows"].items() if app == APP]
        keys = [(s["action"], s.get("control"), s.get("label"), s.get("key")) for s in steps if s["action"] == "aid"]
        self.assertEqual(keys, [("aid", "key", "PF3", "PF3"), ("aid", "key", "PA1", "PA1"),
                                ("aid", "key", "Clear", "Clear")])  # fmt: skip

    def test_the_transitions_carry_the_key(self):
        screens = {sc["screen"]: sc for sc in self.box["screens"][APP]}
        out = {t["to"]: t for t in screens["SIGN ON"]["transitions"]}
        self.assertEqual(out["SIGNED OFF"].get("key"), "PF3")
        self.assertEqual(out["SIGN ON"].get("key"), "Clear")
        back = {t["to"]: t for t in screens["SIGNED OFF"]["transitions"]}
        self.assertEqual(back["SIGN ON"].get("key"), "PA1")

    def test_the_references_are_in_the_contract(self):
        doc = agent_main.contract_references(self.box)
        self.assertEqual(contract.errors(doc, "references"), [])
        kept = st.clean_screens(doc["screens"])[APP]
        keys = sorted(t.get("key") for sc in kept for t in sc["transitions"])
        self.assertEqual(keys, ["Clear", "PA1", "PF3"])


class KeyNamesTest(unittest.TestCase):
    def test_a_key_name_is_kept_and_a_digit_token_is_not(self):
        for name in ("PF1", "PF24", "PA3", "Clear", "F12", "Enter"):
            self.assertTrue(st.key_name(name), name)
            self.assertIn(name, flow.KEYS)
        for text in ("PF25", "PA4", "F0", "12345", "PF3=EXIT", "clear me"):
            self.assertFalse(st.key_name(text), text)
        t = st.clean_transition({"to": "X", "from_state": "0" * 16, "to_state": "1" * 16, "action": "aid",
                                 "control": "key", "label": "PF3", "key": "PF3"})  # fmt: skip
        self.assertEqual((t["label"], t["key"]), ("PF3", "PF3"))
        t = st.clean_transition({"to": "X", "from_state": "0" * 16, "to_state": "1" * 16, "key": "4711"})
        self.assertNotIn("key", t)

    def test_a_web_or_desktop_step_keeps_only_named_keys(self):
        self.assertEqual(flow.label_step({"action": "key", "key": "F5", "surface": "web"}, 1).get("key"), "F5")
        self.assertNotIn("key", flow.label_step({"action": "key", "key": "a", "surface": "web"}, 1))


if __name__ == "__main__":
    unittest.main()
