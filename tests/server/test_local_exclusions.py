"""Recorder contract 1.5: applications the recording PC's own exclude list kept out of a recording
(`completeness.local_exclusions`). The portal keeps their count and names, says "excluded on this
PC" beside the completeness, and does not treat them as a gap."""

import copy
import unittest

from twinlab2 import completeness
from twinlab2_contract import recorder_contract as contract
from test_completeness import Base, RECORDING
from test_registry import completeness_report, references_1_1

EXCLUDED = {"count": 2, "names": ["Microsoft Word", "Microsoft Excel"], "entries": 24, "managed": False}


def with_exclusions(local=EXCLUDED, overall="complete"):
    report = completeness_report(overall)
    report["local_exclusions"] = copy.deepcopy(local)
    doc = references_1_1(report=report)
    doc["contract_version"] = "1.5.0"
    return doc


class CleanTest(unittest.TestCase):
    def test_the_stored_form_keeps_names_and_counts_only(self):
        report = completeness_report()
        report["local_exclusions"] = {"count": 2, "names": ["Microsoft Word", "  Microsoft   Excel ", 7, ""],
                                      "entries": 24, "managed": True, "titles": ["Budget.xlsx - Excel"]}  # fmt: skip
        got = completeness.clean(report)["local_exclusions"]
        self.assertEqual(got, {"count": 2, "names": ["Microsoft Excel", "Microsoft Word"], "entries": 24,
                               "managed": True})  # fmt: skip

    def test_a_recording_without_exclusions_has_no_block(self):
        self.assertNotIn("local_exclusions", completeness.clean(completeness_report()))

    def test_the_summary_says_excluded_on_this_pc(self):
        data = completeness.clean(dict(completeness_report(), local_exclusions=EXCLUDED))
        self.assertEqual(completeness.summary(data), "Web complete; desktop complete; network evidence complete; "
                         "2 applications excluded on this PC (Microsoft Excel, Microsoft Word)")  # fmt: skip
        self.assertEqual(completeness.label(data), "Complete")  # not a gap: the recording stays complete
        managed = completeness.clean(dict(completeness_report(), local_exclusions=dict(EXCLUDED, count=1,
                                                                                         names=["Teams"], managed=True)))  # fmt: skip
        self.assertTrue(completeness.summary(managed).endswith("1 application excluded on this PC (Teams) by the "
                                                               "office's policy"))  # fmt: skip

    def test_the_contract_accepts_the_block_and_refuses_more(self):
        doc = with_exclusions()
        doc["recorder"] = {"name": "TwinLabRecorder", "version": "0.2.0"}
        self.assertEqual(contract.errors(doc, "references"), [])
        doc["completeness"]["local_exclusions"]["titles"] = ["Budget.xlsx - Excel"]
        self.assertTrue(any("titles" in e for e in contract.errors(doc, "references")))


class PortalTest(Base):
    def test_the_recording_page_says_how_many_were_excluded_on_this_pc(self):
        self.pair()
        self.send(with_exclusions())
        status = self.recording()["status"]
        self.assertEqual(status["completeness"]["local_exclusions"], {
            "count": 2, "names": ["Microsoft Excel", "Microsoft Word"], "entries": 24, "managed": False})
        self.assertIn("2 applications excluded on this PC (Microsoft Excel, Microsoft Word)", status["summary"])
        self.assertEqual(status["label"], "Complete")
        self.assertFalse(status["attention"])  # the gap they leave is not a failure
        self.assertEqual(self.listed(), "Complete")

    def test_a_1_4_recorder_is_still_accepted(self):
        self.pair()
        doc = references_1_1()
        doc["contract_version"] = "1.4.0"
        self.send(doc)
        self.assertNotIn("local_exclusions", self.recording()["status"]["completeness"])
        self.assertEqual(RECORDING, self.recording()["id"])


if __name__ == "__main__":
    unittest.main()
