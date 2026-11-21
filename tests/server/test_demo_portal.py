"""The demo portal (`tools/demo_portal.py`) still seeds: ten recorded applications in one recording,
Billing published, Accounts Receivable with a frozen package. It runs the same APIs a person
would, so a change that breaks the demo breaks here first."""

import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from support import Client

TOOL = Path(__file__).resolve().parents[2] / "tools" / "demo_portal.py"


def load():
    spec = importlib.util.spec_from_file_location("demo_portal", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DemoPortalTest(unittest.TestCase):
    def test_the_demo_seeds_the_whole_journey(self):
        demo = load()
        with tempfile.TemporaryDirectory() as state, mock.patch.dict(os.environ, {}):
            app, _world, _ctx, _database = demo.build(state)
            alice = Client(app, remote="10.0.0.9")
            self.assertEqual(alice.post("/api/v1/session", json={"user": "alice", "password": "alice-demo"}).status_code, 200)
            catalog = alice.get("/api/v1/apps").json()
            apps = {a["name"]: a for a in catalog["apps"]}
            # seven catalog applications plus six the recording discovered (two desktop programs
            # both titled "Billing", a TN3270 host, two more web origins, a till)
            self.assertEqual(len(apps), 13, sorted(apps))
            self.assertIn("Billing (Contoso Ltd)", apps)
            self.assertIn("Billing (billing.exe)", apps)
            self.assertEqual(sum(a["status"] == "discovered" for a in apps.values()), 6)
            recording = catalog["recordings"][0]["id"]

            billing = alice.get(f"/api/v1/apps/{apps['Billing']['id']}").json()
            self.assertEqual([r["state"] for r in billing["releases"]], ["published"])
            receivable = apps["Accounts Receivable"]["id"]
            self.assertEqual(len(alice.get(f"/api/v1/apps/{receivable}/packages").json()["packages"]), 1)
            # both seeded packages are complete: frozen without a known-gaps override
            for app_id in (apps["Billing"]["id"], receivable):
                revision = alice.get(f"/api/v1/apps/{app_id}/packages/1").json()
                self.assertTrue(revision["completeness"]["passed"], revision["completeness"]["gaps"])
                self.assertIsNone(revision["known_gaps"])

            export = alice.get(f"/api/v1/recordings/{recording}/packages/download")
            self.assertEqual(export.status_code, 200)

            # bob builds Billing and sees no other application's packages
            bob = Client(app, remote="10.0.0.9")
            self.assertEqual(bob.post("/api/v1/session", json={"user": "bob", "password": "bob-demo"}).status_code, 200)
            self.assertEqual(bob.get(f"/api/v1/apps/{receivable}/packages").status_code, 403)


if __name__ == "__main__":
    unittest.main()
