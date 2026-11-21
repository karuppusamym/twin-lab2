"""The ten-application session fixture: the committed folder is what its generator writes, the
collectors' timeline still builds the committed references from it, and they are in the recorder
contract (1.1: every step with its event id, the recording with its completeness; 1.2: a web
step under a target's path prefix keeps it). The 1.1 references of the same session as the PR 23
recorder built them (`references-ten-apps-1.1.json`) are kept, for the portal's 1.1 tests."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

from twinlab2_recorder import main as agent_main
from twinlab2_contract import recorder_contract as contract
from twinlab2_recorder.workstation import flow

try:
    import jsonschema
except ImportError:
    jsonschema = None

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
FOLDER = FIXTURES / "ten-app-session" / "session"
BUILT = FIXTURES / "references-ten-apps.json"
LEGACY = FIXTURES / "references-ten-apps-1.1.json"
FIN = "web:https://fin.example.test"
sys.path.insert(0, str(FIXTURES / "ten-app-session"))
import generate  # noqa: E402


def files(folder):
    return {p.relative_to(folder).as_posix(): p.read_bytes().replace(b"\r\n", b"\n")
            for p in sorted(folder.rglob("*")) if p.is_file()}


class TenAppFixtureTest(unittest.TestCase):
    def test_the_committed_folder_is_what_the_generator_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            generate.write(Path(tmp) / "session")
            self.assertEqual(files(Path(tmp) / "session"), files(FOLDER),
                             "regenerate: python tests/fixtures/ten-app-session/generate.py")

    def test_the_collectors_still_build_the_references(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "session"
            generate.write(folder)
            built = generate.build(folder)
        self.assertEqual(built, json.loads(BUILT.read_text(encoding="utf-8")),
                         "the timeline builds other references: review, then regenerate")

    def test_the_references_are_in_the_contract(self):
        doc = agent_main.contract_references(json.loads(BUILT.read_text(encoding="utf-8")))
        self.assertEqual(doc["contract_version"], "1.5.0")
        self.assertEqual(contract.errors(doc, "references"), [])
        if jsonschema is not None:
            schema = contract.schema()
            jsonschema.validate(doc, {**schema, "$ref": "#/$defs/references_1_1"})
        steps = [s for flow in doc["flows"].values() for s in flow]
        self.assertEqual(len(steps), len(generate.STEPS))
        self.assertEqual({s["event_id"] for s in steps}, set(generate.tags()))
        self.assertEqual(doc["completeness"]["overall"], "complete")
        # ten identities: the two applications on erp.example.test, and the three on
        # fin.example.test, are one origin each to the recorder
        self.assertEqual(len(doc["apps"]), 10)
        self.assertEqual(sorted(a["surface"] for a in doc["apps"]), ["desktop"] * 3 + ["mainframe"] + ["web"] * 6)
        # the two programs titled "Billing" are two identities that the recorder names alike
        billing = [a for a in doc["apps"] if a["surface"] == "desktop" and a["display"] == "Billing"]
        self.assertEqual({a["exe"] for a in billing}, {"ContosoBilling.exe", "billing.exe"})
        # each step is on the application whose tag its label carries
        tags = generate.tags()
        for key, flow in doc["flows"].items():
            expected = {"web:https://erp.example.test": 2, FIN: 3}.get(key, 1)
            self.assertEqual(len({tags[s["event_id"]] for s in flow}), expected, key)
        text = json.dumps(doc, ensure_ascii=False)
        for leak in ("INV-1001", "notepad", "s-accoun", "C:\\\\", "period", "2026-11?", "/42", "/7", "2025"):
            self.assertNotIn(leak, text)

    def test_a_web_step_keeps_the_path_prefix_of_its_target(self):
        doc = json.loads(BUILT.read_text(encoding="utf-8"))
        screens = {s["event_id"]: s["screen"] for s in doc["flows"][FIN]}
        tags = generate.tags()
        by_tag = {}
        for event, screen in screens.items():
            by_tag.setdefault(tags[event], set()).add(screen.removeprefix("https://fin.example.test"))
        self.assertEqual(by_tag, {
            "ledger": {"/finance", "/finance/{id}/{id}", "/finance/journal/edit"},  # billing-archive is not billing
            "receivable": {"/finance/billing/invoices/{id}", "/finance/billing"},
            "payroll": {"/finance/payroll/runs/{id}", "/finance/payroll/runs/new"},
        })
        paths = sorted(t["path"] for t in doc["session"]["targets"] if t.get("path"))
        self.assertEqual(paths, ["/billing", "/billing-admin", "/finance", "/finance/billing", "/finance/payroll"])

    def test_the_other_applications_steps_are_as_the_1_1_recorder_sent_them(self):
        """The 1.2 rules change only what is under a target's multi-segment prefix: the ten
        applications of the 1.1 fixture get the same labels and screens as the 1.1 recorder gave."""
        old, new = (json.loads(p.read_text(encoding="utf-8")) for p in (LEGACY, BUILT))
        self.assertEqual(old["contract_version"], "1.1.0")
        self.assertEqual(contract.errors(agent_main.contract_references(old), "references"), [])
        self.assertEqual({s["event_id"] for f in old["flows"].values() for s in f}, set(generate.legacy_tags()))

        def facts(doc, screen):
            return {k: [(s.get("action"), s.get("control"), s.get("label"), screen(s.get("screen"))) for s in v]
                    for k, v in doc["flows"].items() if k != FIN}

        def as_1_1(screen):
            # 1.4 keeps a plain lower-case route word (gap 4) where 1.1 wrote {id}
            if not screen:
                return screen
            head, _, path = screen.partition("://")[2].partition("/")
            segs = path.split("/")
            return screen[: len(screen) - len(path)] + "/".join(
                "{id}" if i and flow.route_word(s) and s not in flow.ROUTE_WORDS else s for i, s in enumerate(segs))

        self.assertEqual(facts(old, as_1_1), facts(new, as_1_1))
        changed = {s.get("screen") for f in new["flows"].values() for s in f} - {
            s.get("screen") for f in old["flows"].values() for s in f}
        self.assertIn("https://erp.example.test/billing/invoices/{id}", changed)


if __name__ == "__main__":
    unittest.main()
