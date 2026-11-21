"""The recorder contract: the schema, version compatibility, the portal's checks, the recorder's
stamping, and the collectors' timeline that produces the references."""

import copy
import json
import os
from pathlib import Path
import shutil
from contextlib import closing
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from twinlab2_recorder import main as agent_main
from twinlab2_recorder.workstation import timeline
from twinlab2_recorder.client import PortalError
from twinlab2_contract import recorder_contract as contract
from support import connect
from test_registry import EVENT_1, RegistryTest, completeness_report, references, references_1_1

try:
    import jsonschema
except ImportError:  # the cross-check with a full JSON Schema implementation is optional
    jsonschema = None

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SESSION = FIXTURES / "workstation-session"
BUILT = FIXTURES / "references-from-the-collectors.json"
CV = contract.CONTRACT_VERSION


def pair_message(**extra):
    return {"contract_version": CV, "code": "ABCDEFGHJK", "name": "PC 7",
            "recorder": {"name": "TwinLabRecorder", "version": "0.2.0"}, **extra}  # fmt: skip


class SchemaTest(unittest.TestCase):
    def test_one_version_everywhere(self):
        self.assertEqual(contract.schema()["x-contract-version"], CV)
        self.assertIsNotNone(contract.parse(CV))
        # the version the collectors' timeline writes on every outbox: this portal accepts it
        self.assertEqual(timeline.CONTRACT_VERSION, CV)
        self.assertEqual(contract.compatibility(timeline.CONTRACT_VERSION), contract.parse(CV))

    @unittest.skipIf(jsonschema is None, "jsonschema is not installed")
    def test_the_schema_is_valid_json_schema_2020_12(self):
        jsonschema.Draft202012Validator.check_schema(contract.schema())

    def samples(self):
        good = references()
        steps_without_n = copy.deepcopy(good)
        steps_without_n["flows"]["web:http://127.0.0.1:8080"][0].pop("n")
        new = references_1_1()

        def changed(edit):
            doc = copy.deepcopy(new)
            edit(doc)
            return doc

        def step(doc):
            return doc["flows"]["web:http://127.0.0.1:8080"][0]

        partial = completeness_report("partial", desktop={
            "status": "interrupted", "notes": ["UI Automation stopped answering"],
            "gaps": [{"from": "2026-11-19T10:02:00Z", "to": "2026-11-19T10:05:00Z", "reason": "collector restarted"}]})
        return [
            ("references", good, True),  # 1.0.0: steps without event ids, no completeness
            ("references", dict(good, completeness=completeness_report()), True),  # optional in 1.0
            ("references", new, True),
            ("references", dict(new, completeness=partial), True),
            ("references", dict(new, contract_version="1.7.3", future_field={"x": 1}), True),
            ("references", changed(lambda d: d.pop("preflight")), True),  # preflight is optional
            ("references", changed(lambda d: step(d).pop("seq")), True),  # ordered by n, which equals it
            ("references", changed(lambda d: d["completeness"].update(loss_window_seconds=12.5, recovered=True)), True),
            ("references", dict(good, contract_version="1.1.0"), False),  # 1.1 needs event ids
            ("references", changed(lambda d: d.pop("completeness")), False),
            ("references", changed(lambda d: step(d).pop("event_id")), False),
            ("references", changed(lambda d: step(d).pop("captured_at")), False),
            ("references", changed(lambda d: step(d).update(event_id="9b2f6c1e-8d2a-4b7e-9a51-2c4d9e7f0a13")), False),  # v4
            ("references", changed(lambda d: step(d).update(event_id=EVENT_1.upper())), False),
            ("references", changed(lambda d: step(d).update(channel="email")), False),
            ("references", changed(lambda d: step(d).update(captured_at="2026-11-19 10:01")), False),
            ("references", changed(lambda d: step(d).update(captured_at="2026-11-19T10:01:00+02:00")), False),
            ("references", changed(lambda d: step(d).update(seq=-1)), False),
            ("references", changed(lambda d: d["completeness"].update(overall="stopped")), False),
            ("references", changed(lambda d: d["completeness"]["channels"].update(email={"status": "complete"})), False),
            ("references", changed(lambda d: d["completeness"]["channels"]["web"].update(status="ok")), False),
            ("references", changed(lambda d: d["completeness"]["channels"]["web"].update(
                gaps=[{"from": "yesterday", "to": "2026-11-19T10:05:00Z", "reason": "x"}])), False),
            ("references", changed(lambda d: d["completeness"].update(network_evidence="partial")), False),
            ("references", changed(lambda d: d["completeness"].update(loss_window_seconds=-1)), False),
            ("references", changed(lambda d: d["completeness"].update(recovered="no")), False),
            ("references", changed(lambda d: d["preflight"].pop("ready")), False),
            ("references", changed(lambda d: d["preflight"]["checks"][0].update(ok="yes")), False),
            ("references", dict(good, format="twinlab-references/2"), False),
            ("references", {k: v for k, v in good.items() if k != "timeline"}, False),
            ("references", dict(good, session={"id": "../../etc"}), False),
            ("references", dict(good, apps=[{"key": "exe:casedesk.exe", "level": "full"}]), False),
            ("references", dict(good, apps=[{"key": "desktop:X", "level": "admin"}]), False),
            ("references", steps_without_n, False),
            ("references", dict(good, timeline=[{"app": "presence", "steps": -1}]), False),
            ("references", dict(good, contract_version="1.0"), False),
            ("pair", pair_message(), True),
            ("pair", pair_message(name=""), False),
            ("pair", pair_message(name="a\nb"), False),
            ("pair", {"code": "ABCDEFGHJK", "name": "PC"}, False),
        ]

    def test_valid_payloads_pass_and_broken_ones_do_not(self):
        for message, doc, ok in self.samples():
            with self.subTest(message=message, doc=str(doc)[:80]):
                self.assertEqual(contract.errors(doc, message) == [], ok, contract.errors(doc, message))

    @unittest.skipIf(jsonschema is None, "jsonschema is not installed")
    def test_the_small_validator_agrees_with_jsonschema(self):
        schema = contract.schema()
        for message, doc, ok in self.samples():
            name = contract.definition(message, doc.get("contract_version"))
            sub = dict(schema, **{"$ref": f"#/$defs/{name}"})
            sub.pop("oneOf")
            with self.subTest(message=message, doc=str(doc)[:80]):
                self.assertEqual(jsonschema.Draft202012Validator(sub).is_valid(doc), ok)

    def test_compatibility(self):
        self.assertEqual(contract.compatibility(CV), contract.parse(CV))
        self.assertEqual(contract.compatibility("1.9.0"), (1, 9, 0))  # newer minor: optional fields only
        for old in (None, "0.9.0", "0.1.0"):
            with self.subTest(old=old), self.assertRaises(contract.ContractError) as caught:
                contract.compatibility(old)
            self.assertEqual(caught.exception.update, "recorder")
            self.assertIn("Update TwinLabRecorder", str(caught.exception))
        with self.assertRaises(contract.ContractError) as caught:
            contract.compatibility("2.0.0")
        self.assertEqual(caught.exception.update, "portal")
        self.assertIn("Update the portal", str(caught.exception))
        for bad in ("1.0", "v1.0.0", "01.0.0", 1, "1.0.0-beta"):
            with self.subTest(bad=bad), self.assertRaises(contract.ContractError):
                contract.compatibility(bad)


class FixtureRecordingTest(unittest.TestCase):
    """References that the collectors' timeline built from a recorded session folder."""

    def test_the_fixture_built_by_the_collectors_conforms(self):
        built = json.loads(BUILT.read_text(encoding="utf-8"))
        # The timeline stamps its contract version (1.3.0; Twin Lab's 1.1.0 since 4385949) on
        # every outbox, also on this session, which was recorded before event ids: no step has
        # one, so the recorder sends it as contract 1.0
        self.assertEqual(built["contract_version"], "1.3.0")
        self.assertFalse(any(s.get("event_id") for steps in built["flows"].values() for s in steps))
        doc = agent_main.contract_references(built)
        self.assertEqual(doc["contract_version"], "1.0.0")
        self.assertNotIn("completeness", doc)  # computed without a capture journal: not known
        self.assertEqual(contract.errors(doc, "references"), [])
        self.assertEqual({a["surface"] for a in doc["apps"]}, {"desktop", "web", "mainframe"})
        labels = {s.get("label") for steps in doc["flows"].values() for s in steps}
        self.assertTrue({"txtSearch", "btnOpen", "btnSave", "USERID", "ACCOUNT NO", "PF3"} <= labels, labels)
        text = json.dumps(doc)
        for leak in ("X-999", "C-100", "Xxxx", "notepad", "s-1a2b"):
            self.assertNotIn(leak, text)

    def test_the_collectors_still_build_the_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "s"
            shutil.copytree(SESSION, folder, ignore=shutil.ignore_patterns("README.md"))
            built = json.loads(json.dumps(timeline.build(folder)))
        self.assertEqual(built, json.loads(BUILT.read_text(encoding="utf-8")),
                         "the timeline builds other references from the fixture: review, then regenerate")


class PortalContractTest(unittest.TestCase):
    """The portal's checks on pairing and upload (a portal and sign-in as in test_registry)."""

    setUp, ok, pair, agent = RegistryTest.setUp, RegistryTest.ok, RegistryTest.pair, RegistryTest.agent

    def code(self):
        return self.ok(self.client.post("/api/v1/machines/pairing-codes"), 201)["display"]

    def upload(self, doc, token):
        return self.client.http.post("/api/agent/references", json=doc, headers=self.agent(token))

    def test_pairing_refuses_an_old_or_newer_contract_without_using_the_code(self):
        code = self.code()
        for version, update in ((None, "recorder"), ("0.9.0", "recorder"), ("2.0.0", "portal")):
            body = {"code": code, "name": "PC"} | ({"contract_version": version} if version else {})
            response = self.client.http.post("/api/agent/pair", json=body)
            self.assertEqual(response.status_code, 426, version)
            self.assertEqual(response.json()["update"], update)
            self.assertEqual(response.json()["contract"]["portal"], CV)
            self.assertIn("pdate", response.json()["error"])
        response = self.client.http.post("/api/agent/pair", json={"contract_version": CV, "code": code, "name": "PC",
                                                             "recorder": {"name": "TwinLabRecorder", "version": "0.2.0"}})
        self.assertEqual(response.status_code, 200, response.text)  # the code still worked
        self.assertEqual(response.json()["contract_version"], CV)
        [machine] = self.ok(self.client.get("/api/v1/machines"))["machines"]
        self.assertEqual(machine["contract_version"], CV)
        with closing(connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM pairing_failures").fetchone()[0], 0)

    def test_upload_refuses_an_old_or_major_bumped_contract(self):
        _, token = self.pair()
        old = {k: v for k, v in references().items() if k != "contract_version"}
        for doc, update in ((old, "recorder"), (dict(references(), contract_version="0.3.1"), "recorder"),
                            (dict(references(), contract_version="2.0.0"), "portal")):  # fmt: skip
            response = self.upload(doc, token)
            self.assertEqual(response.status_code, 426, doc.get("contract_version"))
            self.assertEqual(response.json()["update"], update)
        with closing(connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM recordings").fetchone()[0], 0)
        shape = dict(references(), apps=[{"key": "desktop:X", "level": "admin"}])
        response = self.upload(shape, token)
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.json()["errors"])

    def test_each_recording_keeps_the_recorder_and_contract_versions(self):
        _, token = self.pair()
        doc = agent_main.contract_references(json.loads(BUILT.read_text(encoding="utf-8")))
        doc["contract_version"] = "1.0.3"  # a newer patch of the 1.0 shape is accepted
        response = self.upload(doc, token)
        self.assertEqual(response.status_code, 200, response.text)
        doc = dict(references_1_1(), contract_version="1.2.0", recorder={  # a newer minor is accepted
            "name": "TwinLabRecorder", "version": "0.2.0", "twinlab_commit": "430dfe538965b46ccbe824f8f2b4f415c5576483"})
        response = self.upload(doc, token)
        self.assertEqual(response.status_code, 200, response.text)
        recording_id = response.json()["recording_id"]
        with closing(connect(self.database)) as db:
            row = db.execute("SELECT contract_version, recorder, recorder_version, twinlab_commit "
                             "FROM recording_contracts WHERE recording_id = ?", (recording_id,)).fetchone()  # fmt: skip
        self.assertEqual(row, ("1.2.0", "TwinLabRecorder", "0.2.0", "430dfe538965b46ccbe824f8f2b4f415c5576483"))
        shown = self.ok(self.client.get(f"/api/v1/recordings/{recording_id}"))
        self.assertEqual(shown["refs"]["contract"]["contract_version"], "1.2.0")
        self.assertEqual(shown["refs"]["contract"]["twinlab_commit"], "430dfe538965b46ccbe824f8f2b4f415c5576483")


class RecorderStampTest(unittest.TestCase):
    def test_a_recording_with_event_ids_on_only_some_steps_is_refused(self):
        doc = references_1_1()
        steps = [s for flow in doc["flows"].values() for s in flow]
        self.assertGreater(len(steps), 1)
        for field in ("event_id", "channel", "captured_at"):
            steps[-1].pop(field, None)
        with self.assertRaises(agent_main.PortalError) as caught:
            agent_main.contract_references(doc)
        self.assertIn("event_id: required", str(caught.exception))
        self.assertEqual(agent_main.contract_references(references_1_1())["contract_version"],
                         references_1_1()["contract_version"])  # fmt: skip

    def test_the_recorder_stamps_and_checks_before_sending(self):
        box = json.loads(BUILT.read_text(encoding="utf-8"))
        doc = agent_main.contract_references(box)
        self.assertEqual(doc["contract_version"], "1.0.0")  # a session recorded before event ids
        newer = {k: v for k, v in references_1_1().items() if k != "contract_version"}
        self.assertEqual(agent_main.contract_references(newer)["contract_version"], "1.1.0")
        unstamped = {k: v for k, v in box.items() if k not in ("contract_version", "completeness")}
        self.assertEqual(agent_main.contract_references(unstamped)["contract_version"], "1.0.0")
        self.assertEqual(doc["recorder"]["name"], "TwinLabRecorder")
        self.assertEqual(box["contract_version"], "1.3.0")  # the outbox on disk is left as it is
        self.assertIn("completeness", box)
        with self.assertRaises(PortalError):
            agent_main.contract_references(dict(box, contract_version="2.0.0"))
        broken = copy.deepcopy(box)
        broken["timeline"] = "x"
        with self.assertRaises(PortalError) as caught:
            agent_main.contract_references(broken)
        self.assertIn("outside recorder contract", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
