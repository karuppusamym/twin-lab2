"""Recorder contract 1.6: the person paused a recording (`completeness.paused`). The portal keeps
how many times, for how long and when, says "paused twice, for 3 min 21 s in all" beside the
completeness, and does not treat the pauses as a gap."""

import copy
import unittest

from twinlab2 import completeness
from twinlab2_contract import recorder_contract as contract
from test_completeness import Base
from test_registry import completeness_report, references_1_1

PAUSED = {"count": 2, "seconds": 201.0, "periods": [
    {"from": "2026-11-21T10:53:12.000Z", "to": "2026-11-21T10:55:40.000Z", "seconds": 148.0},
    {"from": "2026-11-21T11:02:00.000Z", "to": "2026-11-21T11:02:53.000Z", "seconds": 53.0}]}  # fmt: skip


def with_pauses(paused=PAUSED):
    report = completeness_report()
    report["paused"] = copy.deepcopy(paused)
    doc = references_1_1(report=report)
    doc["contract_version"] = "1.6.0"
    return doc


class CleanTest(unittest.TestCase):
    def test_the_stored_form_keeps_valid_periods(self):
        got = completeness.clean(dict(completeness_report(), paused={
            "count": 3, "seconds": 210, "periods": [PAUSED["periods"][0], {"from": "soon", "to": "later"},
                                                   {"from": "2026-11-21T12:00:00Z", "to": "2026-11-21T12:00:09Z"}],
            "titles": ["Budget"]}))["paused"]  # fmt: skip
        self.assertEqual(got["count"], 3)
        self.assertEqual(got["seconds"], 210.0)
        self.assertEqual(
            [p["seconds"] for p in got["periods"]], [148.0, 9.0]
        )  # the unreadable one is left out
        self.assertNotIn("titles", got)

    def test_a_recording_without_a_pause_has_no_block(self):
        self.assertNotIn("paused", completeness.clean(completeness_report()))
        self.assertNotIn(
            "paused",
            completeness.clean(
                dict(completeness_report(), paused={"count": 0, "periods": []})
            ),
        )

    def test_the_summary_says_paused_and_the_recording_stays_complete(self):
        data = completeness.clean(dict(completeness_report(), paused=PAUSED))
        self.assertEqual(completeness.summary(data), "Web complete; desktop complete; network evidence complete; "
                         "paused twice, for 3 min 21 s in all")  # fmt: skip
        self.assertEqual(completeness.label(data), "Complete")
        once = completeness.clean(dict(completeness_report(), paused=dict(PAUSED, count=1, seconds=45,
                                                                          periods=PAUSED["periods"][:1])))  # fmt: skip
        self.assertTrue(completeness.summary(once).endswith("paused once, for 45 s"))
        self.assertEqual(completeness.duration_text(3720), "1 h 2 min")

    def test_the_contract_accepts_the_block_and_refuses_more(self):
        self.assertEqual(contract.CONTRACT_VERSION, "1.6.0")
        doc = with_pauses()
        doc["recorder"] = {"name": "TwinLabRecorder", "version": "0.2.0"}
        self.assertEqual(contract.errors(doc, "references"), [])
        doc["completeness"]["paused"]["periods"][0]["app"] = "Notepad"
        self.assertTrue(any("app" in e for e in contract.errors(doc, "references")))


class PortalTest(Base):
    def test_the_recording_page_says_when_it_was_paused(self):
        self.pair()
        self.send(with_pauses())
        status = self.recording()["status"]
        self.assertEqual(status["completeness"]["paused"], PAUSED)
        self.assertIn("paused twice, for 3 min 21 s in all", status["summary"])
        self.assertEqual(status["label"], "Complete")
        self.assertFalse(status["attention"])

    def test_a_1_5_recorder_is_still_accepted(self):
        self.pair()
        doc = references_1_1()
        doc["contract_version"] = "1.5.0"
        self.send(doc)
        self.assertNotIn("paused", self.recording()["status"]["completeness"])


if __name__ == "__main__":
    unittest.main()
