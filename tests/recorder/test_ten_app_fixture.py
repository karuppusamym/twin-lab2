"""The ten-application session fixture: the committed folder is what its generator writes, the
collectors' timeline still builds the committed references from it, and they are in the recorder
contract (1.1: every step with its event id, the recording with its completeness)."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

from twinlab2_recorder import main as agent_main
from twinlab2_contract import recorder_contract as contract

try:
    import jsonschema
except ImportError:
    jsonschema = None

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
FOLDER = FIXTURES / "ten-app-session" / "session"
BUILT = FIXTURES / "references-ten-apps.json"
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
        self.assertEqual(doc["contract_version"], "1.1.0")
        self.assertEqual(contract.errors(doc, "references"), [])
        if jsonschema is not None:
            schema = contract.schema()
            jsonschema.validate(doc, {**schema, "$ref": "#/$defs/references_1_1"})
        steps = [s for flow in doc["flows"].values() for s in flow]
        self.assertEqual(len(steps), len(generate.STEPS))
        self.assertEqual({s["event_id"] for s in steps}, set(generate.tags()))
        self.assertEqual(doc["completeness"]["overall"], "complete")
        # nine identities: the two applications on erp.example.test are one origin to the recorder
        self.assertEqual(len(doc["apps"]), 9)
        self.assertEqual(sorted(a["surface"] for a in doc["apps"]), ["desktop"] * 3 + ["mainframe"] + ["web"] * 5)
        # the two programs titled "Billing" are two identities that the recorder names alike
        billing = [a for a in doc["apps"] if a["surface"] == "desktop" and a["display"] == "Billing"]
        self.assertEqual({a["exe"] for a in billing}, {"ContosoBilling.exe", "billing.exe"})
        # each step is on the application whose tag its label carries
        tags = generate.tags()
        for key, flow in doc["flows"].items():
            self.assertEqual(len({tags[s["event_id"]] for s in flow}), 2 if key == "web:https://erp.example.test" else 1)
        text = json.dumps(doc, ensure_ascii=False)
        for leak in ("INV-1001", "notepad", "s-accoun", "C:\\\\"):
            self.assertNotIn(leak, text)


if __name__ == "__main__":
    unittest.main()
