"""Verify on every surface: the mainframe (3270) replay against Twin Lab's synthetic TN3270 host
and a deliberately wrong rebuild, the desktop replay on recorded window trees, a recording that
spans two applications on one host (each to its own deployment), and the visual comparison at the
recording's own size."""

from contextlib import closing
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

import yaml

from twinlab2 import independent, portal
from twinlab2.replay import fidelity, tn3270, visual
import test_verification as tv

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
V1 = "/api/v1"


def demo_host():
    spec = importlib.util.spec_from_file_location("tn3270_demo_host", FIXTURES / "tn3270_demo_host.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HOST = demo_host()
VALUES = {"USERID": ["TESTER"], "PASSWORD": ["WRONG", "DEMO123"], "ACCOUNT NO": ["1000001", "1999999", "1000002"]}
CASES = [
    {"id": "wrong-password-refused", "after": 4, "contains": "SIGN ON FAILED: CHECK THE PASSWORD"},
    {"id": "signed-on", "after": 7, "contains": "ACCOUNT INQUIRY"},
    {"id": "anna", "after": 10, "contains": "ANNA SAMPLE"},
    {"id": "unknown-account", "after": 13, "contains": "ACCOUNT NOT FOUND"},
    {"id": "omar", "after": 16, "contains": "OMAR EXAMPLE"},
    {"id": "signed-off", "after": "end", "contains": "SIGNED OFF"},
]


def record_mainframe(out: Path) -> Path:
    """A real recording of the synthetic host: the recorder's own TN3270 proxy in front of it,
    a scripted terminal running Twin Lab's flow. Judge files (values, acceptance) beside it."""
    from twinlab2_recorder.workstation import tn3270 as recorder

    server, port = HOST.serve()
    try:
        proxy = recorder.Proxy("127.0.0.1", port, out, listen=0, app_key=f"mainframe:127.0.0.1:{port}")
        proxy.start()
        HOST.flow(proxy.listen)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and sum(1 for _ in (out / "steps.jsonl").open(encoding="utf-8")) < 19:
            time.sleep(0.05)
        proxy.stop()
        proxy.join(5)
        proxy.recorder.close()
    finally:
        server.close()
    (out / "bindings.yaml").write_text(yaml.safe_dump({"fields": VALUES}), encoding="utf-8")
    (out / "acceptance.yaml").write_text(yaml.safe_dump({"version": 1, "cases": CASES}), encoding="utf-8")
    return out


class MainframeReplayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory()
        cls.folder = record_mainframe(Path(cls.work.name) / "ws-mainframe" / "mainframe")

    @classmethod
    def tearDownClass(cls):
        cls.work.cleanup()

    def host(self, wrong=False):
        server, port = HOST.serve(wrong=wrong)
        self.addCleanup(server.close)
        return f"127.0.0.1:{port}"

    def test_the_recording_is_the_flow(self):
        steps = [json.loads(line) for line in (self.folder / "steps.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(steps), 19)
        self.assertEqual([s["screen"]["title"] for s in steps if s["action"] == "screen"],
                         ["DEMO SIGN ON", "DEMO SIGN ON", *["ACCOUNT INQUIRY"] * 4, "SIGNED OFF"])

    def test_the_synthetic_host_passes(self):
        report = tn3270.replay(self.folder, self.host())
        self.assertEqual(report["status"], "pass", report["problems"])
        self.assertEqual(report["verdict"], "PASS WITHIN DECLARED SCOPE")
        self.assertEqual(report["coverage"]["compared"], 7)
        self.assertEqual([c["status"] for c in report["cases"]], ["PASS"] * 6)
        self.assertEqual(report["categories"]["replay"]["status"], "PASS")

    def test_a_wrong_rebuild_fails_on_its_title_and_fields(self):
        report = tn3270.replay(self.folder, f"tn3270://{self.host(wrong=True)}")
        self.assertEqual(report["status"], "fail")
        first = next(s for s in report["steps"] if s["status"] == "fail")
        self.assertEqual(first["seq"], 8)
        checks = {d["check"]: d for d in first["differences"]}
        self.assertEqual((checks["title"]["expected"], checks["title"]["actual"]), ("ACCOUNT INQUIRY", "ACCOUNT ENQUIRY"))
        self.assertEqual(checks["input fields (row, col, len, numeric, hidden)"]["expected"], [[4, 24, 10, True, False]])
        self.assertEqual(checks["input fields (row, col, len, numeric, hidden)"]["actual"], [[4, 24, 9, True, False]])
        self.assertEqual(report["categories"]["replay"]["status"], "FAIL")

    def test_a_business_check_that_does_not_hold_fails(self):
        folder = Path(tempfile.mkdtemp(dir=self.work.name)) / "mainframe"
        shutil.copytree(self.folder, folder)
        cases = [*CASES[:2], {"id": "anna", "after": 10, "contains": "ANNA EXAMPLE"}]
        (folder / "acceptance.yaml").write_text(yaml.safe_dump({"cases": cases}), encoding="utf-8")
        report = tn3270.replay(folder, self.host())
        self.assertEqual(report["status"], "fail")
        self.assertEqual([c["status"] for c in report["cases"]], ["PASS", "PASS", "FAIL"])

    def test_a_password_without_a_bound_value_is_blocked(self):
        folder = Path(tempfile.mkdtemp(dir=self.work.name)) / "mainframe"
        shutil.copytree(self.folder, folder)
        (folder / "bindings.yaml").unlink()
        report = tn3270.replay(folder, self.host())
        self.assertEqual(report["status"], "blocked")
        blocked = next(s for s in report["steps"] if s["status"] == "blocked")
        self.assertEqual(blocked["seq"], 3)
        self.assertIn("non-display field (PASSWORD)", blocked["note"])
        self.assertTrue(all(s["status"] == "skipped" for s in report["steps"] if s["seq"] > 3))

    def test_no_host_fails_never_passes(self):
        server, port = HOST.serve()
        server.close()
        report = tn3270.replay(self.folder, f"127.0.0.1:{port}")
        self.assertNotEqual(report["status"], "pass")

    def test_verify_runs_it_in_its_own_process(self):
        runner = independent.TwinLabChecks(recordings=self.work.name, timeout=120)
        self.assertEqual(runner.surfaces("ws-mainframe"), ["mainframe"])
        scenarios = [{"id": "scenario-1", "name": "Account inquiry"}]
        manifest = {"scenarios": [{"name": "Account inquiry", "events": [{"recording_id": "ws-mainframe"}]}]}
        report = independent.run(runner, scenarios, manifest, None, "",
                                 plan={"surfaces": ["mainframe"], "mainframe": self.host()})
        self.assertEqual(report["scenarios"][0]["result"], "pass", report["scenarios"])
        self.assertEqual(report["scenarios"][0]["replays"][0]["surface"], "mainframe")
        dims = report["dimensions"]
        self.assertEqual(dims["structural"]["status"], "pass")
        self.assertIn("title line and input fields", dims["structural"]["scope"])
        self.assertEqual((dims["behavioural"]["status"], dims["behavioural"]["passed"]), ("pass", 6))
        self.assertEqual(dims["visual"]["status"], "not-checked")
        wrong = independent.run(runner, scenarios, manifest, None, "",
                                plan={"surfaces": ["mainframe"], "mainframe": self.host(wrong=True)})
        self.assertEqual(wrong["scenarios"][0]["result"], "fail")
        self.assertEqual(wrong["dimensions"]["structural"]["status"], "fail")

    def test_a_web_recording_is_not_replayed_for_a_mainframe_application(self):
        runner = independent.TwinLabChecks(recordings=self.work.name, timeout=60)
        manifest = {"scenarios": [{"name": "x", "events": [{"recording_id": "ws-mainframe"}]}]}
        report = independent.run(runner, [{"id": "scenario-1", "name": "x"}], manifest, "http://app.test/", "",
                                 plan={"surfaces": ["web"]})
        self.assertEqual(report["scenarios"][0]["result"], "not-run")
        self.assertIn("no steps on this application's surface (web)", report["scenarios"][0]["why"])


class ResetPasses(independent.TwinLabChecks):
    """The real replay; the lab agent's reset (another HTTP service) as passed."""

    def reset(self, lab_url):
        return {"result": "pass", "notes": "reset restored the baseline (1 table(s))"}


class MainframePortalTest(tv.VerificationWorld):
    """The portal's real Verify (a job, the real runner) for a mainframe application: Tested
    against the synthetic host, Packaged against the wrong rebuild."""

    def setUp(self):
        super().setUp()
        self.recordings = tempfile.TemporaryDirectory()
        self.addCleanup(self.recordings.cleanup)
        record_mainframe(Path(self.recordings.name) / self.recording_id / "mainframe")
        self.app.config["TWINLAB_RUNNER"] = ResetPasses(recordings=self.recordings.name, timeout=120)
        self.settings(target_platform="mainframe")

    def verify_against(self, wrong):
        server, port = HOST.serve(wrong=wrong)
        self.addCleanup(server.close)
        self.green()
        release_id = self.verify()
        self.deploy(release_id)
        return self.post(f"{V1}/releases/{release_id}/verify", targets={"mainframe": f"tn3270://127.0.0.1:{port}"}), release_id

    def test_tested_against_the_synthetic_host(self):
        done, release_id = self.verify_against(wrong=False)
        self.assertEqual(done["state"], "passed", done["reasons"])
        self.assertEqual(self.state(release_id), "tested")
        self.assertEqual(done["progress"]["scenarios"][0]["surface"], "mainframe")
        self.assertEqual(done["dimensions"]["structural"]["status"], "pass")
        self.assertEqual(done["scope"]["platform"], "Mainframe (3270)")

    def test_not_tested_against_a_wrong_rebuild(self):
        done, release_id = self.verify_against(wrong=True)
        self.assertEqual(done["state"], "failed")
        self.assertEqual(self.state(release_id), "packaged")
        self.assertIn("Scenario “Invoice is saved”: fail", " ".join(done["reasons"]))
        report = json.loads(self.client.get(done["report"]["url"]).content)
        replay = report["twinlab"]["scenarios"][0]["replays"][0]
        self.assertEqual((replay["surface"], replay["status"]), ("mainframe", "fail"))


# --- desktop ----------------------------------------------------------------------------------------

DESK = FIXTURES / "desktop-casedesk-rec"


def recorded(seq):
    return json.loads((DESK / "trees" / f"{seq:04d}.json").read_text(encoding="utf-8"))


class FakeLive:
    """The built app's windows as the recording shows them after each step (a perfect rebuild),
    or changed by `mutate(tree)` (a wrong one)."""

    def __init__(self, rec, mutate=None):
        self.rec, self.mutate, self.open = rec, mutate or (lambda tree: tree), {}

    def advance(self, step):
        sel = (step.get("window") or {}).get("selector")
        if step.get("action") in ("focus", "menu_open", "expand", "collapse"):
            return  # their trees show a popup or a passing state: the replay skips these steps
        if step.get("action") == "window_close":
            self.open.pop(sel, None)
        elif sel and step.get("tree"):
            self.open[sel] = self.mutate(json.loads(json.dumps(self.rec.tree(step["tree"]))))

    def windows(self):
        return [(n, tree) for n, tree in enumerate(self.open.values(), 1)]

    def window(self, selector, timeout=0):
        from twinlab2_recorder.desktop import locator

        want = locator.parse(selector)
        hits = [(h, t) for h, t in self.windows() if locator.matches(t, want)]
        return hits[0] if len(hits) == 1 else (None, len(hits))


class Steps(list):
    def __init__(self, items, live):
        super().__init__(items)
        self.live = live

    def __iter__(self):
        for step in list.__iter__(self):
            self.live.advance(step)
            yield step


def run_desktop(mutate=None, cases=None):
    """twinlab2.replay.desktop.verify on the CaseDesk recording, with the app, UI Automation and
    its actions replaced by the recorded trees: no window opens."""
    from twinlab2.replay import desktop

    holder = {}

    class Rec(desktop.Recording):
        def __init__(self, path):
            super().__init__(path)
            holder["live"] = FakeLive(self, mutate)
            self.steps = Steps(self.steps, holder["live"])

    class Proc:
        pid = 4242

        def poll(self):
            return 0

    fake_win = mock.MagicMock()
    fake_win.top_windows.return_value = [1]
    with mock.patch.object(desktop, "require_live"), \
            mock.patch("twinlab2_recorder.desktop.require", create=True), \
            mock.patch("twinlab2_recorder.desktop.win", fake_win, create=True), \
            mock.patch.object(desktop, "Recording", Rec), \
            mock.patch.object(desktop.subprocess, "Popen", return_value=Proc()), \
            mock.patch.object(desktop, "_Live", side_effect=lambda pids: holder["live"]), \
            mock.patch.object(desktop, "_act", return_value="acted"), \
            mock.patch.object(desktop.time, "sleep"):
        observe = {"after": {int(c["after"]) for c in cases or [] if str(c["after"]).isdigit()}}
        report = desktop.verify("CaseDesk.exe", DESK, observe=observe)
        return desktop.finish(report, desktop.judge_cases(cases or [], observe.get("trees") or {}))


def without(automation_id):
    def mutate(tree):
        def walk(node):
            node["children"] = [walk(c) for c in node.get("children") or [] if c.get("aid") != automation_id
                                and c.get("automation_id") != automation_id and c.get("AutomationId") != automation_id]
            return node
        return walk(tree)
    return mutate


class DesktopReplayTest(unittest.TestCase):
    def test_trees_compare(self):
        from twinlab2.replay import desktop

        tree = recorded(19)
        self.assertEqual(desktop.compare_trees(tree, tree), [])
        diffs = desktop.compare_trees(tree, without(self.save_id())(json.loads(json.dumps(tree))))
        self.assertTrue(any(d.check == "missing" and "btnSave" in d.element for d in diffs), diffs)
        extra = desktop.compare_trees(without(self.save_id())(json.loads(json.dumps(tree))), tree)
        self.assertTrue(any(d.check == "extra" and "btnSave" in d.element for d in extra), extra)

    def save_id(self):
        return "btnSave"

    def test_the_recorded_app_passes(self):
        cases = [{"id": "signed-in", "after": 11, "check": "window", "selector": "uia:AutomationId=MainForm;ControlType=Window"},
                 {"id": "save-shown", "after": 19, "check": "control", "selector": "uia:AutomationId=btnSave;ControlType=Button"},
                 {"id": "login-closed", "after": 13, "check": "window", "expect": "absent",
                  "selector": "uia:AutomationId=LoginForm;ControlType=Window"}]
        report = run_desktop(cases=cases)
        self.assertEqual(report["status"], "pass", [s for s in report["steps"] if s["status"] == "fail"][:2])
        self.assertEqual(report["surface"], "desktop")
        self.assertEqual([c["status"] for c in report["cases"]], ["PASS", "PASS", "PASS"], report["cases"])
        self.assertGreater(report["coverage"]["compared"], 20)

    def test_a_rebuild_without_the_save_button_fails(self):
        report = run_desktop(mutate=without("btnSave"),
                             cases=[{"id": "save-shown", "after": 19, "check": "control",
                                     "selector": "uia:AutomationId=btnSave;ControlType=Button"}])
        self.assertEqual(report["status"], "fail")
        failed = [s for s in report["steps"] if s["status"] == "fail"]
        self.assertTrue(any(d["check"] == "missing" and "btnSave" in d["element"] for s in failed for d in s["differences"]))
        self.assertEqual(report["cases"][0]["status"], "FAIL")
        self.assertEqual(report["categories"]["replay"]["status"], "FAIL")

    def test_cases(self):
        from twinlab2.replay import desktop

        windows = [recorded(11)]
        main = "uia:AutomationId=MainForm;ControlType=Window"
        self.assertEqual(desktop.judge_case({"check": "window", "selector": main}, windows)["status"], "PASS")
        self.assertEqual(desktop.judge_case({"check": "window", "selector": main, "expect": "absent"}, windows)["status"], "FAIL")
        self.assertEqual(desktop.judge_case({"check": "window", "selector": main}, None)["status"], "BLOCKED")
        self.assertEqual(desktop.judge_case({"check": "control", "selector": "nope"}, windows)["status"], "BLOCKED")
        self.assertEqual(desktop.judge_cases([{"after": "soon", "selector": main}], {})[0]["status"], "BLOCKED")

    def test_a_live_replay_needs_a_windows_verifier_that_allows_it(self):
        from twinlab2.replay import desktop
        from twinlab2.replay import __main__ as entry

        with mock.patch.dict("os.environ", {"TWINLAB2_DESKTOP_VERIFY": ""}):
            with self.assertRaises(desktop.DesktopUnavailable):
                desktop.require_live()
            with tempfile.TemporaryDirectory() as work:
                out = Path(work) / "r.json"
                self.assertEqual(entry.main([str(DESK), "--surface", "desktop", "--target", "x.exe", "--json", str(out)]), 1)
                report = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "not-run")
        self.assertIn("TWINLAB2_DESKTOP_VERIFY=1", report["problems"][0])
        with mock.patch.object(sys, "platform", "linux"), mock.patch.dict("os.environ", {"TWINLAB2_DESKTOP_VERIFY": "1"}):
            with self.assertRaises(desktop.DesktopUnavailable):
                desktop.require_live()

    def test_the_executable_comes_from_the_verifiers_builds_folder(self):
        with tempfile.TemporaryDirectory() as root:
            exe = Path(root) / "casedesk" / "CaseDesk.exe"
            exe.parent.mkdir()
            exe.write_bytes(b"MZ")
            with mock.patch.dict("os.environ", {"TWINLAB2_DESKTOP_BUILDS": ""}):
                self.assertIn("not set up", portal.desktop_target({"desktop": "casedesk/CaseDesk.exe"})[2])
            with mock.patch.dict("os.environ", {"TWINLAB2_DESKTOP_BUILDS": root}):
                path, commit, why = portal.desktop_target({"desktop": "casedesk/CaseDesk.exe"})
                self.assertEqual((Path(path), commit, why), (exe.resolve(), None, None))
                (exe.parent / ".twinlab").mkdir()
                (exe.parent / ".twinlab" / "version").write_text(json.dumps({"commit": tv.SHA}), encoding="utf-8")
                self.assertEqual(portal.desktop_target({"desktop": "casedesk/CaseDesk.exe"})[1], tv.SHA)
                outside = Path(root).parent / "elsewhere.exe"
                self.assertIn("is not an executable", portal.desktop_target({"desktop": str(outside)})[2])
                self.assertIn("is not an executable", portal.desktop_target({"desktop": "../x.exe"})[2])


# --- two applications on one host, told apart by path ---------------------------------------------------

SHARED = "http://shop.example.test:18761"


class OriginMapTest(unittest.TestCase):
    def test_each_path_prefix_goes_to_its_own_replica(self):
        omap = fidelity.OriginMap({SHARED: "http://127.0.0.1:19211", f"{SHARED}/billing": "http://127.0.0.1:19221"},
                                  strict=True)
        self.assertEqual(omap.rebase(f"{SHARED}/orders/new?x=1"), "http://127.0.0.1:19211/orders/new?x=1")
        self.assertEqual(omap.rebase(f"{SHARED}/billing/lookup"), "http://127.0.0.1:19221/billing/lookup")
        self.assertEqual(omap.rebase(f"{SHARED}/billing"), "http://127.0.0.1:19221/billing")
        self.assertEqual(omap.rebase(f"{SHARED}/billingx"), "http://127.0.0.1:19211/billingx")  # whole segments
        self.assertEqual(omap.allowed(), {"http://127.0.0.1:19211", "http://127.0.0.1:19221"})
        self.assertEqual(omap.sources(), {SHARED})
        self.assertEqual(omap.described(), {SHARED: "http://127.0.0.1:19211", f"{SHARED}/billing": "http://127.0.0.1:19221"})

    def test_a_replica_with_a_path_takes_the_prefixs_place(self):
        omap = fidelity.OriginMap({f"{SHARED}/billing": "https://demo.apps.example.test/billing-demo/"}, strict=True)
        self.assertEqual(omap.rebase(f"{SHARED}/billing/lookup#top"), "https://demo.apps.example.test/billing-demo/lookup#top")
        self.assertIsNone(omap.rebase(f"{SHARED}/orders"))  # no replica for the rest of the host

    def test_the_command_line_map(self):
        rec = mock.Mock(path=Path("."), steps=[])
        omap = fidelity.origin_map(rec, None, [f"{SHARED}/billing=http://127.0.0.1:2", f"{SHARED}=http://127.0.0.1:1"], False)
        self.assertEqual(omap.rebase(f"{SHARED}/billing/x"), "http://127.0.0.1:2/billing/x")
        self.assertEqual(omap.rebase(f"{SHARED}/"), "http://127.0.0.1:1/")
        self.assertTrue(omap.strict)

    def test_names_by_prefix(self):
        omap = fidelity.OriginMap({SHARED: "http://127.0.0.1:1", f"{SHARED}/billing": "http://127.0.0.1:2"},
                                  {SHARED: "orders", f"{SHARED}/billing": "billing"}, strict=True)
        self.assertEqual(omap.name_of(f"{SHARED}/billing/x"), "billing")
        self.assertEqual(omap.name_of(f"{SHARED}/orders"), "orders")


class Capture(tv.FakeTwinLab):
    def replay(self, recording_id, base_url, surface="web", target=None, origins=None):
        self.calls.append(("replay", recording_id, base_url, surface, dict(origins or {})))
        return self.default


class TwoAppsTest(tv.VerificationWorld):
    """Orders and Billing share one host, told apart by path. A recording of both, verified for
    Orders: Orders' steps go to this release's deployment, Billing's to Billing's own."""

    def setUp(self):
        super().setUp()
        self.capture = Capture()
        self.app.config["TWINLAB_RUNNER"] = self.capture
        self.billing_server = tv.Deployment(tv.OTHER)
        self.addCleanup(self.billing_server.close)
        billing = self.ok(self.client.post(f"{V1}/apps", json={"name": "Billing (path app)"}), 201)["id"]
        with closing(tv.connect(self.database)) as db:
            for app_id, key in ((1, f"web:{SHARED}"), (billing, f"web:{SHARED}/billing")):
                db.execute("INSERT INTO app_identities (key, app_id, surface) VALUES (?, ?, 'web')", (key, app_id))
                db.execute("INSERT INTO recording_apps (recording_id, app_id, key, level, flow) VALUES (?, ?, ?, 'replicate', '[]')",
                           (self.recording_id, app_id, key))
            brief = db.execute("SELECT revision FROM briefs WHERE app_id = 1 LIMIT 1").fetchone()["revision"]
            db.execute("INSERT INTO releases (app_id, version, revision, commit_sha, archive, archive_sha) "
                       "VALUES (?, '1', ?, ?, ?, 'x')", (billing, brief, tv.OTHER, b"zip"))
            rid = db.execute("SELECT id FROM releases WHERE app_id = ?", (billing,)).fetchone()["id"]
            db.execute("INSERT INTO deployments (release_id, environment, url, username, secret_ref, verification) "
                       "VALUES (?, 'Demo', ?, '', '', 'verified')", (rid, self.billing_server.url))
            db.commit()

    def test_each_application_is_replayed_against_its_own_deployment(self):
        self.green()
        release_id = self.verify()
        self.deploy(release_id)
        done = self.post(f"{V1}/releases/{release_id}/verify")
        self.assertEqual(done["state"], "passed", done["reasons"])
        call = [c for c in self.capture.calls if c[0] == "replay"][-1]
        self.assertEqual(call[3], "web")
        self.assertEqual(call[4], {SHARED: self.deployment.url, f"{SHARED}/billing": self.billing_server.url})
        self.assertEqual(done["dimensions"]["deployment"]["origins"], call[4])

    def test_the_runner_passes_the_map_to_the_replay(self):
        runner = independent.TwinLabChecks(recordings=self.work.name)
        folder = Path(self.work.name) / "r1"
        folder.mkdir()
        (folder / "steps.jsonl").write_text("{}\n", encoding="utf-8")
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            Path(cmd[cmd.index("--json") + 1]).write_text('{"status": "pass", "cases": []}', encoding="utf-8")
            return mock.Mock(stdout="", stderr="")

        with mock.patch.object(independent.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(independent.importlib.util, "find_spec", return_value=object()):
            runner.replay("r1", None, origins={SHARED: "http://127.0.0.1:1", f"{SHARED}/billing": "http://127.0.0.1:2"})
        cmd = seen["cmd"]
        self.assertIn(f"{SHARED}=http://127.0.0.1:1", cmd)
        self.assertIn(f"{SHARED}/billing=http://127.0.0.1:2", cmd)
        self.assertNotIn("--base-url", cmd)

    def test_the_entry_point_gives_the_map_to_the_replay(self):
        from twinlab2.replay import __main__ as entry

        report = mock.Mock(status="pass")
        report.model_dump_json.return_value = "{}"
        report.markdown.return_value = ""
        with mock.patch.object(fidelity, "replay", return_value=report) as replay:
            entry.main([self.work.name, "--map", f"{SHARED}/billing=http://127.0.0.1:2", "--json",
                        str(Path(self.work.name) / "out.json")])
        self.assertEqual(replay.call_args.kwargs["origins"], [f"{SHARED}/billing=http://127.0.0.1:2"])


# --- visual at the recording's size ---------------------------------------------------------------------


def web_recording(folder: Path, size=(1258, 570), settled=None, quiet=400, viewport=None):
    from PIL import Image

    (folder / "screens").mkdir(parents=True)
    steps = []
    for seq, (gap_ms, picture) in enumerate([(0, True), (100, True), (900, True), (200, False)], 1):
        stamp = f"2026-11-21T05:50:{10 + seq:02d}.{gap_ms:03d}Z"
        step = {"seq": seq, "ts": stamp, "action": "click" if seq > 1 else "goto", "url": f"{SHARED}/orders"}
        if picture:
            Image.new("RGB", size, (250, 250, 250)).save(folder / "screens" / f"{seq:04d}.png")
            step["screenshot"] = f"screens/{seq:04d}.png"
        if settled and seq in settled:
            step["picture_settled"] = False
            step["picture_note"] = "the next click had begun"
        steps.append(step)
    (folder / "steps.jsonl").write_text("".join(json.dumps(s) + "\n" for s in steps), encoding="utf-8")
    (folder / "recording.json").write_text(json.dumps({"mode": "attach", "viewport": viewport,
                                                       "settle": {"quiet_ms": quiet}}), encoding="utf-8")
    return folder


class VisualTest(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work, True)

    def test_viewport_sizes(self):
        self.assertEqual(visual.viewport_size("desktop"), {"width": 1280, "height": 720})
        self.assertEqual(visual.viewport_size("1258x570"), {"width": 1258, "height": 570})
        self.assertEqual(visual.viewport_size({"width": 800, "height": 600}), {"width": 800, "height": 600})
        self.assertIsNone(visual.viewport_size("huge"))
        self.assertEqual(fidelity._viewport("1258x570", None), ({"width": 1258, "height": 570}, "1258x570"))

    def test_the_viewport_comes_from_the_recording(self):
        attached = web_recording(self.work / "a")
        self.assertEqual(visual.recording_viewport(attached), "1258x570")  # recording.json says null: the pictures
        with (attached / "steps.jsonl").open("a", encoding="utf-8") as steps:  # a last picture never written
            steps.write(json.dumps({"seq": 9, "action": "click", "screenshot": "screens/0009.png"}) + "\n")
        self.assertEqual(visual.recording_viewport(attached), "1258x570")
        launched = web_recording(self.work / "b", viewport={"width": 1280, "height": 720})
        self.assertEqual(visual.recording_viewport(launched), "1280x720")
        empty = self.work / "c"
        empty.mkdir()
        self.assertIsNone(visual.recording_viewport(empty))

    def test_unsettled_pictures_are_restored(self):
        said = web_recording(self.work / "a", settled={2})
        self.assertEqual(visual.unsettled_pictures(said), {2: "the next click had begun"})
        older = web_recording(self.work / "b")  # an older recorder: the quiet time decides
        found = visual.unsettled_pictures(older)
        self.assertEqual(sorted(found), [3])  # step 4 began 300 ms after step 3
        self.assertIn("within the recorder's quiet time (400 ms)", found[3])

    def test_the_template_is_at_the_recordings_size(self):
        from twinlab2_contract.evidence import recording as recmod

        folder = web_recording(self.work / "a", settled={2})
        text = visual.template(recmod.load(folder))
        body = yaml.safe_load(text)
        self.assertEqual(body["viewport"], "1258x570")
        self.assertIn("1258x570", text)
        by = {s["after"]: s for s in body["states"]}
        self.assertEqual(sorted(by), [1, 2, 3])
        self.assertFalse(by[2]["required"])  # not settled: reported, never deciding
        self.assertTrue(by[3]["required"])
        (folder / "visual.yaml").write_text(text, encoding="utf-8")
        spec = visual.load(folder / "visual.yaml")
        self.assertEqual(spec.size(), {"width": 1258, "height": 570})
        self.assertTrue(all(st.problem is None for st in spec.states))
        (folder / "screens" / "0002.png").unlink()  # a picture the recorder dropped: no state, said so
        text = visual.template(recmod.load(folder))
        self.assertEqual(sorted(s["after"] for s in yaml.safe_load(text)["states"]), [1, 3])
        self.assertIn("Not in the recording, so not compared: the pictures of step(s) 2.", text)
        report_only = yaml.safe_load(visual.template(recmod.load(folder), required=False))
        self.assertFalse(any(s["required"] for s in report_only["states"]))

    def test_verify_generates_a_report_only_template(self):
        folder = web_recording(self.work / "r1" / "web")
        runner = independent.TwinLabChecks(recordings=self.work)
        path, how = runner._visual(folder, self.work)
        self.assertEqual(how, "generated")
        self.assertFalse(any(s["required"] for s in yaml.safe_load(path.read_text(encoding="utf-8"))["states"]))
        (folder / "visual.yaml").write_text("version: 1\nstates: []\n", encoding="utf-8")
        self.assertEqual(runner._visual(folder, self.work), (None, "declared"))

    def test_the_visual_dimension(self):
        states = [{"id": "after-step-1", "status": "PASS", "required": False, "viewport": "1258x570", "compared": 10, "ratio": 0.0},
                  {"id": "after-step-2", "status": "FAIL", "required": False, "viewport": "1258x570", "compared": 10, "ratio": 0.3}]
        reported = independent.dimensions([{"status": "pass", "visual": states, "visual_declaration": "generated"}], [])
        self.assertEqual(reported["visual"]["status"], "reported")
        self.assertIn("reported only, they do not decide Tested", reported["visual"]["scope"])
        self.assertEqual(reported["visual"]["viewports"], ["1258x570"])
        required = [dict(s, required=True) for s in states]
        decided = independent.dimensions([{"status": "fail", "visual": required}], [])
        self.assertEqual(decided["visual"]["status"], "fail")
        self.assertIn("2 required", decided["visual"]["scope"])
        self.assertEqual(independent.dimensions([], [])["visual"]["status"], "not-checked")


if __name__ == "__main__":
    unittest.main()
