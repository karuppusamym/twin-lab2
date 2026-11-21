"""Recorder contract 1.8: completeness in separate dimensions (twinlab2_contract.dimensions).

The recorder reports channel availability, event detail, screen/state coverage and network
evidence; the portal adds owner-reviewed scope, derives what it can for an older recorder, shows
all five on the recording, and the package validator weighs recording-based items by them."""

import copy
import unittest

from twinlab2 import completeness, pack_validator
from twinlab2_contract import dimensions as dims
from twinlab2_contract import recorder_contract as contract
from test_completeness import Base
from test_registry import completeness_report, references_1_1

DIMS = {
    "channels": {"status": "partial", "detail": "1 of 2 requested channels captured the whole session", "have": 1, "of": 2},
    "event_detail": {"status": "complete", "detail": "4 of 4 recorded events kept their detail", "have": 4, "of": 4},
    "screen_coverage": {"status": "none", "detail": "0 of 4 steps belong to a screen state", "have": 0, "of": 4},
    "network_evidence": {"status": "complete", "detail": "the network journal covers the whole web recording"},
}


def with_dimensions(dimensions=DIMS):
    report = completeness_report()
    report["dimensions"] = copy.deepcopy(dimensions)
    doc = references_1_1(report=report)
    doc["contract_version"] = "1.8.0"
    return doc


class ContractTest(unittest.TestCase):
    def test_the_contract_accepts_the_block_and_refuses_more(self):
        self.assertEqual(contract.CONTRACT_VERSION, "1.8.0")
        doc = with_dimensions()
        doc["recorder"] = {"name": "TwinLabRecorder", "version": "0.2.0"}
        self.assertEqual(contract.errors(doc, "references"), [])
        bad = copy.deepcopy(doc)
        bad["completeness"]["dimensions"]["owner_scope"] = {"status": "complete", "detail": "x"}
        self.assertTrue(contract.errors(bad, "references"))  # the portal's own: a recorder never sends it
        bad = copy.deepcopy(doc)
        bad["completeness"]["dimensions"]["channels"]["status"] = "great"
        self.assertTrue(contract.errors(bad, "references"))

    def test_older_versions_stay_accepted(self):
        for version in ("1.1.0", "1.7.0"):
            doc = references_1_1()
            doc["contract_version"] = version
            doc["recorder"] = {"name": "TwinLabRecorder", "version": "0.2.0"}
            self.assertEqual(contract.errors(doc, "references"), [], version)


class DeriveTest(unittest.TestCase):
    def test_an_older_completeness_gives_channels_and_network_and_unknowns(self):
        got = dims.derive(completeness.clean(completeness_report()))
        self.assertEqual(got["channels"]["status"], "complete")
        self.assertEqual(got["network_evidence"]["status"], "complete")
        self.assertEqual(got["event_detail"]["status"], "unknown")
        self.assertEqual(got["screen_coverage"]["status"], "unknown")

    def test_an_interrupted_channel_is_partial_and_missing_ones_none(self):
        report = completeness_report()
        report["channels"]["desktop"] = {"status": "interrupted", "gaps": [], "notes": []}
        self.assertEqual(dims.channels_of(report)["status"], "partial")
        report["channels"] = {k: {"status": "missing", "gaps": [], "notes": []} for k in ("web", "network")}
        self.assertEqual(dims.channels_of(report)["status"], "none")

    def test_the_recorders_own_dimensions_win_and_unknown_fields_go(self):
        sent = dict(completeness_report(), dimensions=dict(DIMS, extra={"status": "complete", "detail": ""}))
        sent["dimensions"]["event_detail"] = dict(DIMS["event_detail"], secret="x")
        data = completeness.clean(sent)
        self.assertEqual(set(data["dimensions"]), set(DIMS))
        self.assertNotIn("secret", data["dimensions"]["event_detail"])
        self.assertEqual(dims.derive(data)["channels"]["status"], "partial")  # sent, not derived

    def test_worst_of_several_recordings(self):
        a, b = dims.item("complete", "a", 3, 3), dims.item("partial", "b", 1, 3)
        got = dims.worst([a, b])
        self.assertEqual((got["status"], got["have"], got["of"]), ("partial", 4, 6))
        self.assertEqual(dims.worst([dims.item("not_applicable", "x"), a])["status"], "complete")

    def test_owner_scope(self):
        self.assertEqual(completeness.owner_scope(["known", "not_replicated"])["status"], "complete")
        self.assertEqual(completeness.owner_scope(["known", "discovered"])["status"], "partial")
        self.assertEqual(completeness.owner_scope(["discovered"])["status"], "none")
        self.assertEqual(completeness.owner_scope(["discovered"], accepted=True)["status"], "complete")
        self.assertEqual(completeness.owner_scope([])["status"], "unknown")


class PortalTest(Base):
    def test_the_recording_shows_five_dimensions(self):
        self.pair()
        self.send(with_dimensions())
        status = self.recording()["status"]
        self.assertEqual([d["id"] for d in status["dimensions"]], list(dims.KEYS))
        by = {d["id"]: d for d in status["dimensions"]}
        self.assertEqual(by["screen_coverage"]["status"], "none")
        self.assertEqual(by["channels"]["have"], 1)
        self.assertIn(by["owner_scope"]["status"], ("none", "partial", "complete", "unknown"))
        self.assertEqual(status["completeness"]["dimensions"]["event_detail"]["status"], "complete")

    def test_a_1_7_recorder_is_still_read(self):
        self.pair()
        doc = references_1_1()
        doc["contract_version"] = "1.7.0"
        self.send(doc)
        status = self.recording()["status"]
        self.assertNotIn("dimensions", status["completeness"])
        by = {d["id"]: d["status"] for d in status["dimensions"]}
        self.assertEqual(by["event_detail"], "unknown")
        self.assertEqual(by["channels"], "complete")


def _pack(capture):
    return {"app": {"id": 1, "surface": "web"}, "target": {"kind": "web"},
            "scenarios": [{"name": "Create", "events": [{"event_id": "e1", "screen": "Orders"}]}],
            "evidence": [{"recording_id": "r1", "events": [], **({"capture": capture} if capture else {})}]}


class ValidatorTest(unittest.TestCase):
    def levels(self, capture):
        result = pack_validator.validate(_pack(capture))
        return result, {i["item"]: i for i in result["items"]}

    def test_without_capture_the_result_is_as_before(self):
        result, items = self.levels(None)
        self.assertNotIn("capture", result)
        self.assertTrue(all("capture" not in i for i in items.values()))

    def test_an_item_the_recordings_do_not_show_is_only_documented(self):
        before, plain = self.levels(None)
        result, items = self.levels({"channels": "complete", "event_detail": "none", "screen_coverage": "complete",
                                     "network_evidence": "complete", "owner_scope": "complete"})
        self.assertEqual(plain["scenario.events"]["level"], "observed")
        self.assertEqual(items["scenario.events"]["level"], "documented")
        self.assertEqual(items["scenario.events"]["capture"], "none")
        self.assertEqual(items["screens.controls"]["level"], "observed")  # screen coverage is complete
        self.assertLess(result["coverage"], before["coverage"])
        self.assertEqual([c["id"] for c in result["capture"]], list(dims.KEYS))
        self.assertEqual(pack_validator.summary(result)["capture"]["event_detail"], "none")
        self.assertIn("## Recording completeness", "\n".join(pack_validator.markdown(result)))

    def test_partial_keeps_observed_and_marks_it(self):
        _result, items = self.levels({"event_detail": "partial"})
        self.assertEqual(items["scenario.events"]["level"], "observed")
        self.assertEqual(items["scenario.events"]["capture"], "partial")

    def test_the_weakest_recording_wins(self):
        data = _pack({"event_detail": "complete"})
        data["evidence"].append({"recording_id": "r2", "events": [], "capture": {"event_detail": "none"}})
        result = pack_validator.validate(data)
        self.assertEqual({c["id"]: c["status"] for c in result["capture"]}["event_detail"], "none")


if __name__ == "__main__":
    unittest.main()
