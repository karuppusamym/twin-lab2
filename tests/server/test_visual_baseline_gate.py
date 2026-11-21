"""Approved visual baselines gate Verify (wp/bundle-leftovers). The owner approves a baseline per
screen state from a masked recorder picture (test_package_bundle); here the approved images become
the **required** states of the replay's visual.yaml, a baseline that cannot be replayed is listed
and never passed, and approved baselines that matched nothing block instead of falling back to
"reported". No browser is started."""

import io
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml
from PIL import Image
from twinlab2 import independent
from twinlab2.replay import visual

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "ten-app-session" / "session" / "web"
EVENT_1 = "019aa1b0-0001-7000-8000-000000000001"
EVENT_2 = "019aa1b0-0002-7000-8000-000000000002"


def png(colour=(250, 250, 250), size=(1280, 720)):
    out = io.BytesIO()
    Image.new("RGB", size, colour).save(out, "PNG")
    return out.getvalue()


def baseline(events, **over):
    return dict({"screen": "Orders list", "state": "state-1", "viewport": "1280x720", "theme": "light", "scale": "1",
                 "png": png(), "events": events}, **over)


class ApprovedVisual(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name) / "web"
        shutil.copytree(FIXTURE, self.folder)
        self.work = Path(self.tmp.name) / "work"
        self.work.mkdir()

    def spec(self, baselines):
        path, how, notes = independent.approved_visual(self.folder, self.work, baselines)
        return path, how, notes, (yaml.safe_load(path.read_text(encoding="utf-8")) if path else None)

    def test_an_approved_baseline_becomes_a_required_state_after_its_step(self):
        path, how, notes, doc = self.spec([baseline([EVENT_2])])
        self.assertEqual((how, notes), ("approved baselines", []))
        state = next(s for s in doc["states"] if s["required"])
        self.assertEqual((state["after"], state["viewport"]), (2, "1280x720"))
        self.assertTrue((self.work / state["approved"]).read_bytes().startswith(b"\x89PNG"))
        spec = visual.load(path)
        got = next(s for s in spec.states if s.approved)
        self.assertEqual(got.after, 2)
        self.assertIsNone(got.problem)
        self.assertEqual(visual.baseline_image(got, self.folder, {}, path), (self.work / state["approved"]).resolve())

    def test_a_dark_theme_or_other_scale_is_listed_not_replayed(self):
        path, how, notes, _doc = self.spec([baseline([EVENT_1], theme="dark"), baseline([EVENT_2], scale="2")])
        self.assertIsNone(path)
        self.assertEqual(len(notes), 2)
        self.assertIn("dark theme", notes[0])

    def test_a_baseline_of_another_recording_is_left_to_that_recording(self):
        path, _how, notes, _doc = self.spec([baseline(["not-in-this-recording"])])
        self.assertEqual((path, notes), (None, []))

    def test_a_declared_visual_yaml_wins(self):
        (self.folder / visual.FILE).write_text("version: 1\nstates: []\n", encoding="utf-8")
        _path, how, _notes = independent.TwinLabChecks()._visual(self.folder, self.work, [baseline([EVENT_2])])
        self.assertEqual(how, "declared")


class FakeRunner:
    """A replay that reports one visual state per call, required when an approved baseline was given."""

    def __init__(self, status="PASS"):
        self.status = status

    def source(self, surfaces=None):
        return {"commit": "x", "replay": "twinlab2.replay"}

    def surfaces(self, recording_id):
        return ["web"]

    def replay(self, recording_id, base_url, baselines=None, **kw):
        report = {"status": "pass", "surface": "web", "cases": [], "steps": [], "coverage": {"steps": 1, "compared": 1},
                  "categories": {"replay": {"status": "PASS"}}}
        if baselines:
            report["visual"] = [{"id": "baseline-orders", "status": self.status, "required": True, "ratio": 0.0,
                                 "compared": 10, "viewport": "1280x720"}]
            report["visual_declaration"] = "approved baselines"
            report["baseline_notes"] = ["Orders list / state-2: dark theme at scale 1 is not replayed (light, 1)"]
        else:
            report["visual"] = [{"id": "after-step-1", "status": "PASS", "required": False, "ratio": 0.0,
                                 "compared": 10, "viewport": "1280x720"}]
            report["visual_declaration"] = "generated"
        return report

    def reset(self, lab_url):
        return {"result": "pass"}


class Gate(unittest.TestCase):
    scenarios = [{"id": "s1", "name": "Orders", "starting": "", "actions": "", "expected": ""}]
    manifest = {"scenarios": [{"id": "s1", "events": [{"recording_id": "rec", "event_id": "e"}]}]}

    def run_verify(self, runner, baselines):
        plan = {"baselines": baselines} if baselines else {}
        return independent.run(runner, self.scenarios, self.manifest, "http://x/", "", plan=plan)

    def visual(self, report):
        return report["dimensions"]["visual"]

    def test_without_baselines_visual_stays_reported(self):
        visual_dim = self.visual(self.run_verify(FakeRunner(), None))
        self.assertEqual((visual_dim["status"], visual_dim["required"]), ("reported", 0))

    def test_with_baselines_visual_is_required_and_decides(self):
        ok = self.visual(self.run_verify(FakeRunner("PASS"), [baseline([EVENT_1])]))
        self.assertEqual((ok["status"], ok["required"]), ("pass", 1))
        self.assertIn("approved by the owner as baselines", ok["scope"])
        self.assertEqual(len(ok["baseline_notes"]), 1, "what could not be replayed is reported")
        bad = self.visual(self.run_verify(FakeRunner("FAIL"), [baseline([EVENT_1])]))
        self.assertEqual(bad["status"], "fail")

    def test_baselines_that_matched_nothing_block_instead_of_reporting(self):
        class Nothing(FakeRunner):
            def replay(self, recording_id, base_url, **kw):
                return super().replay(recording_id, base_url)

        got = self.visual(self.run_verify(Nothing(), [baseline(["elsewhere"])]))
        self.assertEqual(got["status"], "blocked")
        self.assertIn("matched no state", got["scope"])


if __name__ == "__main__":
    unittest.main()
