"""Applications that run inside host processes, in one recorder session, through the agent API to
separate applications and packages: two Java programs started with `-jar` under one javaw.exe
image, a Java Web Start program, two Electron apps under electron.exe and a .NET app under
dotnet.exe; host processes whose application cannot be told stay presence and register nothing.

The session is `tests/fixtures/host-app-session/generate.py`, built here into a temporary folder
by the recorder's own code (hosts.resolve, session.app_facts, timeline.build).
"""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from twinlab2.portal import VERSION_PATH, create_app
from twinlab2 import artifacts, auth, buildpack, packages, registry, scenarios
from twinlab2_recorder import main as agent_main
from twinlab2_contract import recorder_contract as contract
from support import Client, connect, new_database

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "host-app-session"
# loaded by path under a name of its own: the ten-app fixture's module is also `generate`
_spec = importlib.util.spec_from_file_location("host_app_fixture", FIXTURE / "generate.py")
fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixture)

RECORDING = "ws-" + fixture.SESSION_ID
OWNER = "ana"
NOW = "2026-11-20T17:00:00+00:00"

# tag: (identity key, the name it is registered under)
EXPECTED = {
    "jarbilling": ("desktop:java/billing-client.jar", "billing-client.jar"),
    "ledger": ("desktop:java/ledger-7.3.jar", "Billing"),
    "claims": ("desktop:Fabrikam/Claims Desk", "Claims Desk"),
    "uibilling": ("desktop:electron/Billing", "Billing (electron)"),
    "stock": ("desktop:electron/stock-viewer", "stock-viewer"),
    "payroll": ("desktop:Northwind Traders/Payroll Client", "Payroll Client"),
}


class HostAppsTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        work = Path(self.work.name)
        self.database = new_database(self.work.name)
        self.app = create_app(self.database)
        self.client = Client(self.app).http
        bound = artifacts.bind(artifacts.Binding(self.app.state.artifacts))
        self.addCleanup(artifacts.unbind, bound)
        self.db = connect(self.database)
        self.addCleanup(self.db.close)
        self.home = work / "Users" / "ana.smith"
        self.found = fixture.write(work / "session", self.home)
        self.box = fixture.build(work / "session")

    def record(self):
        with self.db:
            for number, item in enumerate(fixture.CATALOG, 1):
                app_id = self.db.execute("INSERT INTO apps(name, git_url, requirements, status, surface) "
                                         "VALUES (?, '', '', 'known', 'desktop') RETURNING id", (item["name"],)).fetchone()[0]
                self.assertEqual(app_id, number)
                self.db.execute("INSERT INTO app_identities(key, app_id, surface, facts) VALUES (?, ?, 'desktop', '{}')",
                                (registry.normalise_key(item["key"]), app_id))
                auth.assign(self.db, app_id, OWNER, "owner", "test", NOW)
            code = "ABCDEFGHJK"
            self.db.execute("INSERT INTO pairing_codes(code_sha, created_by, created_at, expires_at) VALUES (?, ?, ?, ?)",
                            (registry.sha(code), OWNER, NOW, "2999-01-01T00:00:00+00:00"))
        response = self.client.post("/api/agent/pair", json={"contract_version": contract.CONTRACT_VERSION,
                                                             "code": code, "name": "Workstation 7"})
        self.assertEqual(response.status_code, 200, response.text)
        headers = {"Authorization": f"Bearer {response.json()['token']}"}
        targets = [registry.target_of(registry.normalise_key(c["key"]), "full", i, c["name"])
                   for i, c in enumerate(fixture.CATALOG, 1)]
        self.assertEqual(targets[0]["key"], "desktop:Northwind Traders/Payroll Client")
        self.assertEqual(targets[1]["exe"], "javaw.exe")
        with self.db:
            self.db.execute("INSERT INTO capture_sessions(name, machine_id, targets, created_by, created_at, status) "
                            "VALUES ('Host programs', 1, ?, ?, ?, 'offered')", (json.dumps(targets), OWNER, NOW))
        doc = agent_main.contract_references(self.box)
        self.assertEqual(contract.errors(doc, "references"), [])
        for _ in range(2):  # sent twice: nothing new the second time
            response = self.client.post("/api/agent/references", json=doc, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["discovered"], [])
        return doc

    def test_each_hosted_application_is_its_own_application_and_package(self):
        doc = self.record()
        # nothing of the person's folders or arguments is sent
        text = json.dumps(doc)
        for secret in ("ana", "Users", "4711", "args.txt", "index.js", "tkinter", "5c2e7a1b", "LocalLow"):
            self.assertNotIn(secret, text)
        self.assertEqual({a["key"] for a in doc["apps"]}, {k for k, _ in EXPECTED.values()})

        tags = fixture.tags()
        found, seen = {}, {}
        for row in self.db.execute("SELECT app_id, flow FROM recording_apps WHERE recording_id = ?", (RECORDING,)):
            for step in json.loads(row["flow"]):
                self.assertNotIn(step["event_id"], seen)
                seen[step["event_id"]] = row["app_id"]
                found.setdefault(row["app_id"], set()).add(tags[step["event_id"]])
        self.assertEqual(set(seen), set(tags))
        for app_id, owned in found.items():
            self.assertEqual(len(owned), 1, f"application {app_id} holds the steps of {sorted(owned)}")
        by_tag = {next(iter(owned)): app_id for app_id, owned in found.items()}
        self.assertEqual(set(by_tag), set(EXPECTED))

        apps = {r["id"]: r for r in self.db.execute("SELECT * FROM apps").fetchall()}
        # the catalog's two plus one per discovered application; the Java tools one got nothing
        self.assertEqual(len(apps), 2 + len(EXPECTED) - 1)
        self.assertEqual(by_tag["payroll"], 1)
        self.assertNotIn(2, found)
        for tag, (key, name) in EXPECTED.items():
            app = apps[by_tag[tag]]
            self.assertEqual((app["name"], app["surface"]), (name, "desktop"), tag)
            keys = [r[0] for r in self.db.execute("SELECT key FROM app_identities WHERE app_id = ?", (app["id"],))]
            self.assertIn(key, keys, tag)
        self.assertEqual(len({a["name"].casefold() for a in apps.values()}), len(apps))

        shas = set()
        for tag, app_id in by_tag.items():
            selection = json.loads(self.db.execute("SELECT selection FROM evidence WHERE app_id = ? AND recording_id = ?",
                                                   (app_id, RECORDING)).fetchone()[0])
            self.assertEqual(selection, sorted(e for e, t in tags.items() if t == tag), tag)
            scenarios.save(self.db, app_id, {
                "name": f"{tag} scenario", "starting": "", "actions": f"{tag}-start", "expected": "", "failures": "",
                "examples": "", "questions": "", "notes": "",
                "events": json.dumps([scenarios.event_ref(RECORDING, e) for e in selection])})
            draft = buildpack.draft(self.db, app_id, VERSION_PATH)
            with self.db:
                rev = packages.freeze(self.db, app_id, OWNER, draft.sha256, "", VERSION_PATH)
            row = packages.get(self.db, app_id, rev)
            shas.add(row["sha256"])
            files = buildpack.read(packages.open_package(self.db, row["storage_ref"]))
            evidence = json.loads(files["evidence.json"])
            self.assertEqual((evidence["app"]["id"], evidence["app"]["name"]), (app_id, apps[app_id]["name"]))
            self.assertEqual({e["event_id"] for r in evidence["evidence"] for e in r["events"]},
                             {e for e, t in tags.items() if t == tag})
            package_text = "\n".join(c.decode("utf-8", "replace") for c in files.values())
            for other in EXPECTED:
                if other != tag:
                    self.assertNotIn(f"{other}-start", package_text, f"{tag}'s package holds {other}")
        self.assertEqual(len(shas), len(EXPECTED))

    def test_hosts_without_an_application_stay_presence(self):
        for pid in fixture.PRESENCE_PIDS:
            ident, level = self.found[pid]
            self.assertEqual(level, "presence", pid)
            self.assertTrue(ident["host"] and ident["reason"], pid)
            self.assertNotIn("ana", ident["reason"])
        self.record()
        keys = {r[0] for r in self.db.execute("SELECT key FROM app_identities")}
        for host in ("python.exe", "node.exe", "java.exe"):
            self.assertNotIn(f"desktop:exe/{host}", keys)
        self.assertEqual(sum(1 for r in self.box["timeline"] if r["app"] == "presence"), 1)

    def test_a_hosted_application_is_named_by_its_product_else_its_file(self):
        item = {"key": "desktop:java/new-tool.jar", "surface": "desktop", "exe": "javaw.exe", "level": "full",
                "display": "new-tool.jar"}
        self.assertEqual(registry.discovered_name(self.db, item), "new-tool.jar")
        with self.db:
            self.db.execute("INSERT INTO apps(name, git_url, requirements, status, surface) "
                            "VALUES ('new-tool.jar', '', '', 'discovered', 'desktop')")
        # a clash gets the runtime as qualifier, not the host's executable name
        self.assertEqual(registry.discovered_name(self.db, item), "new-tool.jar (java)")
        titled = dict(item, key="desktop:Fabrikam/Rates", display="Rates", company="Fabrikam", product="Rates")
        self.assertEqual(registry.discovered_name(self.db, titled), "Rates")


if __name__ == "__main__":
    unittest.main()
