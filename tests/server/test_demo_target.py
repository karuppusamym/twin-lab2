"""The demo's data backend (chosen or predicted from its DDL), its data model and its OpenShift
deployment files in the build pack (demo_target)."""

import json
import tempfile
import unittest

import yaml

from twinlab2 import artifacts, buildpack, demo_target
from twinlab2.portal import VERSION_PATH, create_app
from support import connect, new_database

SQLSERVER = """SET ANSI_NULLS ON
GO
CREATE TABLE [dbo].[Invoice] (Id INT IDENTITY(1,1) PRIMARY KEY, Customer NVARCHAR(80), Due DATETIME2)
GO
CREATE TABLE [dbo].[InvoiceLine] (Id INT IDENTITY(1,1), InvoiceId INT, Amount DECIMAL(10,2))
GO
"""
POSTGRES = "CREATE TABLE IF NOT EXISTS payroll_run (id BIGSERIAL PRIMARY KEY, data JSONB, at TIMESTAMPTZ DEFAULT now());"
SQLITE = "PRAGMA foreign_keys = ON;\nCREATE TABLE case_note (id INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT);"


class DialectTest(unittest.TestCase):
    def test_the_dialect_of_the_ddl_is_recognised(self):
        self.assertEqual(demo_target.dialect([SQLSERVER])[0], "sqlserver")
        self.assertEqual(demo_target.dialect([POSTGRES])[0], "postgresql")
        self.assertEqual(demo_target.dialect([SQLITE])[0], "sqlite")
        self.assertEqual(demo_target.dialect(["CREATE TABLE t (id INT, name VARCHAR(20));"]), (None, 0))
        self.assertEqual(demo_target.dialect([]), (None, 0))

    def test_tables_are_read_from_create_table(self):
        self.assertEqual(demo_target.tables([SQLSERVER, POSTGRES, "create temp table if not exists `x` (a int)"]),
                         ["dbo.Invoice", "dbo.InvoiceLine", "payroll_run", "x"])

    def test_resource_names_are_dns_labels(self):
        self.assertEqual(demo_target.resource_name({"id": 2, "name": "Billing Admin"}), "billing-admin-2")
        self.assertEqual(demo_target.resource_name({"id": 4, "name": "請求 システム"}), "demo-4")
        self.assertEqual(demo_target.resource_name({"id": 9, "name": "3D Viewer"}), "demo-9")
        self.assertEqual(demo_target.resource_name({"id": 7, "name": "Zählerstand Portal – Nord"}),
                         "z-hlerstand-portal-nord-7")
        self.assertEqual(demo_target.public_git_url("https://bot:ghp_secret@github.com/acme/demo.git?x=1#y"),
                         "https://github.com/acme/demo.git")
        self.assertEqual(demo_target.public_git_url("file:///etc/passwd"), "")


class PackTargetTest(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        database = new_database(work.name)
        app = create_app(database)
        bound = artifacts.bind(artifacts.Binding(app.state.artifacts))
        self.addCleanup(artifacts.unbind, bound)
        self.db = connect(database)
        self.addCleanup(self.db.close)

    def app(self, name, surface, git_url="", platform="", backend=""):
        with self.db:
            return self.db.execute(
                "INSERT INTO apps(name, git_url, requirements, status, surface, target_platform, data_backend) "
                "VALUES (?, ?, '', 'known', ?, ?, ?) RETURNING id", (name, git_url, surface, platform, backend)).fetchone()[0]

    def attach(self, app_id, text, share="synthetic", kind="Database", extension=".sql"):
        content = text.encode("utf-8")
        with self.db:
            ref = artifacts.save(self.db, artifacts.put(content, kind="restricted_evidence", app_id=app_id,
                                                        content_type="text/plain"))
            return self.db.execute("INSERT INTO attachments(app_id, title, kind, extension, content, artifact_id, share, "
                                   "added_by) VALUES (?, 'Schema', ?, ?, ?, ?, ?, 'ana') RETURNING id",
                                   (app_id, kind, extension, b"", ref.id, share)).fetchone()[0]

    def pack(self, app_id):
        draft = buildpack.draft(self.db, app_id, VERSION_PATH)
        return draft, json.loads(draft.manifest)

    def test_a_web_demo_gets_its_predicted_backend_tables_and_openshift_files(self):
        app_id = self.app("Billing Desk", "web", git_url="https://bot:ghp_secret@github.com/acme/billing.git")
        attachment = self.attach(app_id, SQLSERVER)
        draft, data = self.pack(app_id)
        target = data["target"]
        self.assertEqual(target["kind"], "web")
        self.assertEqual((target["data_backend"]["engine"], target["data_backend"]["source"]), ("sqlserver", "predicted"))
        self.assertEqual(target["data_model"], {"files": [f"attachments/{attachment}.sql"],
                                                "tables": ["dbo.Invoice", "dbo.InvoiceLine"]})
        name = f"billing-desk-{app_id}"
        self.assertEqual(target["deploy"]["files"], [f"deploy/ocp/{name}/README.md", f"deploy/ocp/{name}/openshift.yaml"])
        self.assertTrue(set(target["deploy"]["files"]) <= set(draft.files))
        resources = yaml.safe_load(draft.files[f"deploy/ocp/{name}/openshift.yaml"])
        kinds = [item["kind"] for item in resources["items"]]
        self.assertEqual(kinds, ["ImageStream", "BuildConfig", "Deployment", "Service", "Route"])
        build = resources["items"][1]["spec"]
        self.assertEqual(build["source"]["git"], {"uri": "https://github.com/acme/billing.git", "ref": "main"})
        self.assertEqual(build["strategy"]["type"], "Docker")
        deployment = resources["items"][2]
        trigger = json.loads(json.dumps(yaml.safe_load(deployment["metadata"]["annotations"]["image.openshift.io/triggers"])))
        self.assertEqual(trigger[0]["from"], {"kind": "ImageStreamTag", "name": f"{name}:latest"})
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["readinessProbe"]["httpGet"]["path"], "/" + VERSION_PATH)
        database = next(e for e in container["env"] if e["name"] == "DATABASE_URL")
        self.assertEqual(database, {"name": "DATABASE_URL", "valueFrom": {"secretKeyRef": {"name": f"{name}-db", "key": "url"}}})
        text = b"".join(draft.files.values()).decode("utf-8")
        self.assertNotIn("ghp_secret", text)
        task = draft.files["BUILD_TASK.md"].decode("utf-8")
        self.assertIn("## Data", task)
        self.assertIn("Backend: **SQL Server** (predicted", task)
        self.assertIn("`dbo.Invoice`", task)
        self.assertIn(f"Commit `deploy/ocp/{name}/`", task)
        self.assertIn(f"oc create secret generic {name}-db", draft.files[f"deploy/ocp/{name}/README.md"].decode())

    def test_the_owners_choice_wins_and_sqlite_runs_on_the_pods_disk(self):
        app_id = self.app("Payroll", "web", backend="sqlite")
        self.attach(app_id, POSTGRES)
        draft, data = self.pack(app_id)
        self.assertEqual(data["target"]["data_backend"]["source"], "chosen")
        self.assertEqual(data["target"]["data_backend"]["engine"], "sqlite")
        name = f"payroll-{app_id}"
        pod = yaml.safe_load(draft.files[f"deploy/ocp/{name}/openshift.yaml"])["items"][2]["spec"]["template"]["spec"]
        self.assertEqual(pod["volumes"], [{"name": "data", "emptyDir": {}}])
        self.assertIn({"name": "DATABASE_URL", "value": "sqlite:////data/demo.sqlite"}, pod["containers"][0]["env"])
        self.assertIn("The demo repository is not set yet", draft.files["BUILD_TASK.md"].decode())

    def test_without_ddl_sqlite_is_predicted_and_tables_are_inferred(self):
        app_id = self.app("Orders", "web")
        draft, data = self.pack(app_id)
        self.assertEqual(data["target"]["data_backend"],
                         {"engine": "sqlite", "source": "predicted",
                          "reason": "No data model is attached: SQLite, the simplest to reset."})
        self.assertEqual(data["target"]["data_model"], {"files": [], "tables": []})
        self.assertIn("infer the tables from the recorded screens", draft.files["BUILD_TASK.md"].decode())

    def test_a_restricted_ddl_predicts_but_adds_no_tables(self):
        app_id = self.app("Ledger", "web")
        self.attach(app_id, POSTGRES, share="restricted")
        _draft, data = self.pack(app_id)
        self.assertEqual(data["target"]["data_backend"]["engine"], "postgresql")
        self.assertEqual(data["target"]["data_model"], {"files": [], "tables": []})

    def test_a_desktop_demo_is_installed_locally(self):
        app_id = self.app("CaseDesk", "desktop")
        draft, data = self.pack(app_id)
        self.assertEqual(data["target"]["deploy"]["kind"], "local")
        self.assertFalse([p for p in draft.files if p.startswith("deploy/")])
        windows = self.app("Kasse", "", platform="windows-desktop")
        self.assertEqual(self.pack(windows)[1]["target"]["kind"], "desktop")
        self.assertEqual(self.pack(self.app("CICS", "mainframe"))[1]["target"]["deploy"]["kind"], "openshift")


if __name__ == "__main__":
    unittest.main()
