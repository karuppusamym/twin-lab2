"""recording_export: one ZIP with the builder packages of every application a recording covers,
filtered by the caller's role. Built on the ten-application recording of test_ten_apps."""

import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock
from zipfile import ZipFile

from twinlab2 import auth, packages, recording_export, scenarios
import test_ten_apps as ten

NOW, OWNER, RECORDING = ten.NOW, ten.OWNER, ten.RECORDING


def unzip(data):
    with ZipFile(io.BytesIO(data)) as archive:
        return {info.filename: archive.read(info) for info in archive.infolist()}, \
            [(i.filename, i.date_time) for i in archive.infolist()]


class RecordingExportTest(ten.TenAppRecordingTest):
    # the route over this module is tested in test_ten_apps (TenAppRecordingTest)
    test_the_recording_export_holds_the_packages_the_caller_builds = None
    test_ten_applications_get_ten_separate_packages = None
    test_the_download_route_names_the_package_by_its_application = None

    def setUp(self):
        super().setUp()
        self.by_tag = self.record()
        self.frozen = {}
        for tag, app_id in self.by_tag.items():
            self.prepare(app_id, tag)
        for tag in ("billing", "admin", "meter", "invoice", "orders", "hr", "contoso", "plain", "kasse",
                    "ledger", "receivable", "payroll"):
            self.frozen[tag] = self.freeze(self.by_tag[tag])
        # the mainframe application has a scenario and a draft, but nothing frozen

    def test_a_builder_of_every_application_gets_one_folder_per_frozen_package(self):
        result = recording_export.export(self.db, RECORDING, OWNER)
        files, infos = unzip(result.data)
        manifest = json.loads(files.pop("manifest.json"))
        self.assertEqual(manifest, result.manifest)
        self.assertEqual(manifest["format"], recording_export.FORMAT)
        self.assertEqual(manifest["recording"], {"id": RECORDING, "name": "Month-end close"})
        self.assertEqual(manifest["counts"], {"included": 12, "not_available": 0, "no_package": 1})
        entries = {e["app_id"]: e for e in manifest["applications"]}
        self.assertEqual(len(entries), 13)
        self.assertEqual([e["slug"] for e in manifest["applications"]],
                         sorted(e["slug"] for e in manifest["applications"]))
        self.assertEqual(sorted(e["kind"] for e in entries.values()), ["desktop"] * 3 + ["mainframe"] + ["web"] * 9)
        folders = {path.split("/", 1)[0] for path in files}
        self.assertEqual(len(folders), 12)
        for tag, row in self.frozen.items():
            app_id = self.by_tag[tag]
            app = self.db.execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone()
            entry = entries[app_id]
            path = f"{packages.slug(app)}/{packages.download_name(app, row)}"
            self.assertEqual(entry["path"], path)
            self.assertEqual(files[path], packages.open_package(self.db, row["storage_ref"]))
            self.assertEqual((entry["name"], entry["slug"], entry["package_revision"], entry["sha256"],
                              entry["revision"], entry["status"]),
                             (app["name"], packages.slug(app), 1, row["sha256"], row["revision"], "included"))
            flow = self.db.execute("SELECT flow FROM recording_apps WHERE app_id = ? AND recording_id = ?",
                                   (app_id, RECORDING)).fetchone()[0]
            self.assertEqual(entry["step_count"], len(json.loads(flow)))
            self.assertTrue(entry["includes_this_recording"])
            self.assertEqual([p for p in files if p.startswith(packages.slug(app) + "/")], [path])
        mainframe = entries[self.by_tag["mainframe"]]
        self.assertEqual((mainframe["status"], mainframe["kind"], mainframe["step_count"]), ("no_package", "mainframe", 3))
        self.assertNotIn("path", mainframe)
        self.assertEqual(sorted(result.included), sorted((self.by_tag[t], 1) for t in self.frozen))
        # deterministic: the same state gives the same bytes, with fixed times
        self.assertEqual(recording_export.export(self.db, RECORDING, OWNER).data, result.data)
        self.assertEqual({t for _, t in infos}, {(1980, 1, 1, 0, 0, 0)})
        self.assertRegex(result.filename, r"^ws-20261120t140000z-10a9b8c7-packages-[0-9a-f]{12}\.zip$")

    def test_only_applications_the_user_builds_are_included(self):
        meter, kasse, orders, mainframe = (self.by_tag[t] for t in ("meter", "kasse", "orders", "mainframe"))
        with self.db:
            auth.assign(self.db, meter, "bo", "builder", OWNER, NOW)
            auth.assign(self.db, kasse, "bo", "builder", OWNER, NOW)
            auth.assign(self.db, mainframe, "bo", "builder", OWNER, NOW)
            auth.assign(self.db, orders, "bo", "viewer", OWNER, NOW)  # a viewer is not given packages
        result = recording_export.export(self.db, RECORDING, "bo")
        files, _ = unzip(result.data)
        manifest = json.loads(files.pop("manifest.json"))
        self.assertEqual(manifest["counts"], {"included": 2, "not_available": 10, "no_package": 1})
        status = {e["app_id"]: e["status"] for e in manifest["applications"]}
        self.assertEqual({k for k, v in status.items() if v == "included"}, {meter, kasse})
        self.assertEqual(status[mainframe], "no_package")
        self.assertEqual(status[orders], "not_available")
        self.assertEqual({p.split("/", 1)[0] for p in files},
                         {packages.slug(self.db.execute("SELECT * FROM apps WHERE id = ?", (i,)).fetchone())
                          for i in (meter, kasse)})
        for entry in manifest["applications"]:
            if entry["status"] == "not_available":
                self.assertEqual(set(entry), {"app_id", "name", "slug", "kind", "status", "note"})
                self.assertIn("Not available to you", entry["note"])
        # nothing of an application bo does not build is in the ZIP besides its manifest entry
        text = b"".join(files.values())
        for tag, row in self.frozen.items():
            if self.by_tag[tag] not in (meter, kasse):
                self.assertNotIn(row["sha256"].encode(), text)
                self.assertNotIn(f"{tag}-attachment".encode(), text)
        # someone with no role gets the list and no package
        nobody = recording_export.export(self.db, RECORDING, "eve")
        self.assertEqual(set(unzip(nobody.data)[0]), {"manifest.json"})
        self.assertEqual(nobody.manifest["counts"], {"included": 0, "not_available": 13, "no_package": 0})
        self.assertEqual(recording_export.export(self.db, RECORDING, "").manifest["counts"]["included"], 0)

    def test_only_frozen_revisions_go_in_never_the_current_draft(self):
        app_id = self.by_tag["orders"]
        scenarios.save(self.db, app_id, {"name": "Not yet approved", "expected": "draft only"})
        result = recording_export.export(self.db, RECORDING, OWNER)
        entry = next(e for e in result.manifest["applications"] if e["app_id"] == app_id)
        self.assertEqual(entry["package_revision"], 1)
        self.assertNotIn(b"Not yet approved", unzip(result.data)[0][entry["path"]])
        second = self.freeze(app_id)
        entry = next(e for e in recording_export.export(self.db, RECORDING, OWNER).manifest["applications"]
                     if e["app_id"] == app_id)
        self.assertEqual((entry["package_revision"], entry["sha256"]), (2, second["sha256"]))

    def test_a_package_that_does_not_match_its_approved_sha256_is_refused(self):
        real = packages.open_package

        def tampered(db, ref):
            return real(db, ref) + b"x"

        with mock.patch("twinlab2.packages.open_package", tampered):
            with self.assertRaises(ValueError):
                recording_export.export(self.db, RECORDING, OWNER)
        with self.assertRaises(LookupError):
            recording_export.export(self.db, "ws-nothing", OWNER)


class FrameworkFreeTest(unittest.TestCase):
    def test_the_module_imports_no_web_framework(self):
        root = Path(__file__).resolve().parents[2]
        code = ("import sys; sys.path[:0] = [%r, %r]; import twinlab2.recording_export; "
                "print(sorted(m for m in ('flask', 'werkzeug', 'fastapi', 'starlette') if m in sys.modules))"
                % (str(root / "server"), str(root / "contract")))
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
        self.assertEqual(out.strip(), "[]")


if __name__ == "__main__":
    unittest.main()
