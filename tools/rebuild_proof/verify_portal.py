"""Step 5 of the proof: the portal's own Verify, as an owner runs it, takes a blind rebuild to Tested.

    python tools/rebuild_proof/verify_portal.py a      # case A: Orders (web)
    python tools/rebuild_proof/verify_portal.py b      # case B: Account Inquiry (mainframe, TN3270)
    python tools/rebuild_proof/verify_portal.py c      # case C: Orders, with Billing on the same host by path

Headless: the web replay is headless Chromium, the 3270 replay a socket terminal. For a case:

1. the case's portal catalog (from record.py and owner.py: the application, its scenarios and the
   frozen package the builder got) is copied to `<evidence>/<case>/verify-portal/` and migrated;
2. the rebuild's committed Git tree is registered as a Packaged release
   (`python -m twinlab2.portal release`: no GitHub, so no CI gate);
3. the rebuilt app is started as its builder said (`replay.launch`), with a **lab agent stand-in**
   beside it (the rebuilds have no `/_lab` API): reset restarts the app on a fresh copy of its
   seed, `/_lab/data` is its SQLite tables, the baseline is those tables right after the first
   start. So the reset check really checks that a reset brings the seed back after the replay;
4. the owner registers the deployment (it must report the release's commit at
   /.twinlab/version), sets the verification settings (target platform, lab URL, launch) and
   presses **Verify this build**: `POST /api/v1/releases/{id}/verify` answers 202;
5. a real worker (`python -m twinlab2.worker --types verify`) runs it with the judge-side
   recordings (`TWINLAB2_RECORDINGS`: the recording with bindings.yaml and acceptance.yaml beside
   it, which no builder saw), while this script polls `GET /api/v1/verifications/{id}` and prints
   the progress;
   Case C's recording spans Orders and Billing, which share one host told apart by path
   (`/billing`). Billing's rebuild is released and deployed on a server of its own; Verify of
   Orders replays Orders' steps against Orders' deployment and Billing's against Billing's (the
   portal builds that map from the applications' identities: no path proxy, no replicas.yaml);
6. the release must end Tested. The verification, the release and the stored report go to
   `<evidence>/<case>/verify-portal/` (`verification.json`, `release.json`, `report.json`).
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import threading
import time

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import EVIDENCE, ROOT, env, read_json, spawn, write_json  # noqa: E402
import portal_api  # noqa: E402
import replay  # noqa: E402

LAB_PORT = {"a": 19281, "b": 19282, "c": 19283, "w": 19284}


def judge_mainframe(case: str) -> Path:
    """<evidence>/<case>/judge-mf/<recording id>/mainframe: the recording, the judge's values (the
    synthetic ones the clerk typed, from drive.VALUES) and acceptance cases (the owner's scenario
    results, on the screen after each key)."""
    src = replay.recording_folder(case) / "mainframe"
    steps = replay.rows(src / "steps.jsonl")
    rec_id = "ws-" + replay.recording_folder(case).name
    out = EVIDENCE / case / "judge-mf" / rec_id / "mainframe"
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(src, out)
    aids = [s["seq"] for s in steps if s["action"] == "aid"]
    cases = []
    for n, (want, why) in enumerate(replay.MF_ACCEPT):
        after = aids[n] if n < len(aids) else "end"
        cases.append({"id": f"after-key-{n + 1}", "after": "end" if n == len(aids) - 1 else after,
                      "contains": want, "required": True, "note": why})
    (out / "bindings.yaml").write_text(yaml.safe_dump({"fields": replay.mf_values(case)}), encoding="utf-8")
    (out / "acceptance.yaml").write_text(yaml.safe_dump({"version": 1, "recording": rec_id, "cases": cases},
                                                        sort_keys=False), encoding="utf-8")
    print(f"judge {case} mainframe: {len(cases)} acceptance cases")
    return out.parent.parent


# -- the rebuilt app and its lab agent stand-in ------------------------------------------------------------


def tables(db_file: Path) -> dict:
    if not db_file.is_file():
        return {}
    with sqlite3.connect(f"file:{db_file.as_posix()}?mode=ro", uri=True) as db:
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                          "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {n: [[str(v) for v in row] for row in db.execute(f'SELECT * FROM "{n}" ORDER BY 1')] for n in names}


class Rebuild:
    """The rebuilt app on its port, restartable on a fresh copy of its seed."""

    def __init__(self, build: str, port: int, extra: dict | None = None):
        self.build, self.port, self.extra = build, port, extra or {}
        self.data = EVIDENCE / "replay-data" / build / "app.db"
        self.proc = None
        self.lock = threading.Lock()
        self.baseline = None

    def start(self):
        self.proc = replay.launch(self.build, self.port, self.extra)
        time.sleep(1.0)
        if self.baseline is None:
            self.baseline = tables(self.data)
        return self

    def stop(self):
        if self.proc is not None:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)], capture_output=True)
            try:
                self.proc.wait(20)
            except subprocess.TimeoutExpired:
                pass
            self.proc = None
        end = time.monotonic() + 20  # Windows frees the app's files a moment after it exits
        while self.data.exists() and time.monotonic() < end:
            try:
                self.data.unlink()
            except PermissionError:
                time.sleep(0.25)

    def reset(self):
        with self.lock:
            self.stop()
            self.start()


class LabStandIn(ThreadingHTTPServer):
    def __init__(self, port: int, app: Rebuild):
        self.app = app
        super().__init__(("127.0.0.1", port), _LabHandler)


class _LabHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, body, status=200):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        app = self.server.app
        if self.path == "/_lab/data":
            return self._json(tables(app.data))
        if self.path == "/_lab/data/baseline":
            return self._json({"tables": app.baseline})
        if self.path == "/_lab/health":
            return self._json({"ok": True})
        return self._json({"error": "not here"}, 404)

    def do_POST(self):
        if self.path == "/_lab/reset":
            try:
                self.server.app.reset()
            except Exception as exc:  # noqa: BLE001 - the reset check reports it
                return self._json({"error": f"reset failed: {type(exc).__name__}: {exc}"}, 500)
            return self._json({"reset": True})
        return self._json({"error": "not here"}, 404)


# -- the case ---------------------------------------------------------------------------------------------

CASES = {
    "a": {"build": "a-orders", "port": replay.WEB_APPS["a"]["orders"]["port"], "platform": "web",
          "extra": {"BILLING_URL": "http://127.0.0.1:18752"}},
    "b": {"build": "b-account-inquiry", "port": replay.MF["b"]["port"], "platform": "mainframe", "extra": {}},
    # Orders links to Billing's own deployment (on the real host: the same host, /billing)
    "c": {"app": "Orders", "build": "c-orders", "port": replay.WEB_APPS["c"]["orders"]["port"], "platform": "web",
          "extra": {"BILLING_URL": f"http://127.0.0.1:{replay.WEB_APPS['c']['billing']['port']}"},
          "others": [{"app": "Billing", "build": "c-billing", "port": replay.WEB_APPS["c"]["billing"]["port"]}]},
    # w: as c, recorded in the recorder's own headless Edge (record.py w)
    "w": {"app": "Orders", "build": "w-orders", "port": replay.WEB_APPS["w"]["orders"]["port"], "platform": "web",
          "extra": {"BILLING_URL": f"http://127.0.0.1:{replay.WEB_APPS['w']['billing']['port']}"},
          "others": [{"app": "Billing", "build": "w-billing", "port": replay.WEB_APPS["w"]["billing"]["port"]}]},
}


def register(portal, catalog, extra, app_id, repo):
    """A Packaged release of the repository's HEAD under the application's latest frozen package."""
    commit = head(repo)
    rows = portal.get(f"/api/v1/apps/{app_id}/packages")["packages"]
    done = subprocess.run([sys.executable, "-m", "twinlab2.portal", "--database", str(catalog), "release",
                           str(app_id), commit[:12], rows[0]["revision"], str(repo)],
                          capture_output=True, text=True, env={**env(), **extra}, cwd=ROOT)
    print(done.stdout.strip() or done.stderr.strip())
    return commit, next(r["id"] for r in portal.get(f"/api/v1/apps/{app_id}")["releases"] if r["commit_sha"] == commit)


def head(repo: Path) -> str:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()


def run(case: str) -> int:
    spec = CASES[case]
    out = EVIDENCE / case / "verify-portal"
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(EVIDENCE / case / "portal", out / "portal", ignore=shutil.ignore_patterns("portal.log"))
    recordings = (replay.judge_web(case).parent.parent if spec["platform"] == "web" else judge_mainframe(case))
    for replicas in recordings.glob("*/web/replicas.yaml"):
        replicas.unlink()  # the portal's Verify maps each application to its own deployment
    repo = EVIDENCE / "builds" / spec["build"] / "repo"
    commit = head(repo)
    catalog = out / "portal" / "catalog.db"
    extra = {"TWINLAB2_STATE": str(out / "portal" / "state"), "DATABASE_URL": "", "TWINLAB2_PASSWORD": ""}
    # the portal migrates the copied catalog when it starts; then the release is registered
    portal = portal_api.Portal(case)
    portal.folder = out / "portal"
    portal.start()
    rebuild, lab, worker, others = None, None, None, []
    try:
        page = portal.get("/api/v1/apps")
        apps = {a["name"]: a for a in (page.get("apps") if isinstance(page, dict) else page)}
        app = apps[spec["app"]] if spec.get("app") else next(iter(apps.values()))
        for other in spec.get("others") or []:  # the other application's release, on its own server
            o_commit, o_release = register(portal, catalog, extra, apps[other["app"]]["id"],
                                           EVIDENCE / "builds" / other["build"] / "repo")
            others.append(Rebuild(other["build"], other["port"]).start())
            portal.put(f"/api/v1/releases/{o_release}/deployments",
                       json={"environment": "Rebuild (local)", "url": f"http://127.0.0.1:{other['port']}/"})
            print(f"{other['app']} release {o_release} ({o_commit[:12]}) deployed at http://127.0.0.1:{other['port']}/")
        commit, release_id = register(portal, catalog, extra, app["id"], repo)
        rebuild = Rebuild(spec["build"], spec["port"], spec["extra"]).start()
        lab = LabStandIn(LAB_PORT[case], rebuild)
        threading.Thread(target=lab.serve_forever, daemon=True).start()
        deployment = f"http://127.0.0.1:{spec['port']}/"
        portal.put(f"/api/v1/releases/{release_id}/deployments", json={"environment": "Rebuild (local)", "url": deployment})
        setup = portal.get(f"/api/v1/apps/{app['id']}/setup")
        portal.put(f"/api/v1/apps/{app['id']}/verification-settings", json={
            "required_checks": "", "target_platform": spec["platform"], "windows_check_pattern": "",
            "lab_url": f"http://127.0.0.1:{LAB_PORT[case]}", "launch": "python run.py (PORT, HOST, DATABASE_URL)",
            "expected_revision": (setup.get("app") or setup).get("revision")})
        worker = spawn([sys.executable, "-m", "twinlab2.worker", "--database", str(catalog), "--types", "verify",
                        "--idle", "0.5"], out / "worker.log",
                       extra_env={**extra, "TWINLAB2_RECORDINGS": str(recordings), "TWINLAB2_LAB_TOKEN": ""})
        queued = portal.post(f"/api/v1/releases/{release_id}/verify", ok=(202,),
                             json={"request_key": f"proof-{case}-{commit[:12]}"})
        print(f"POST /api/v1/releases/{release_id}/verify -> 202, verification {queued['id']} ({queued['state']})")
        seen, end = None, time.monotonic() + 1800
        verification = queued
        while verification["state"] in ("queued", "running"):
            if time.monotonic() > end:
                raise SystemExit("the verification did not end within 30 minutes")
            time.sleep(1.0)
            verification = portal.get(f"/api/v1/verifications/{queued['id']}")
            line = (verification["state"], verification["progress"]["stage"], verification["progress"]["done"],
                    verification["progress"]["total"])
            if line != seen:
                print(f"  {line[0]}: {line[1]} ({line[2]} of {line[3]} scenarios done)")
                seen = line
        detail = portal.get(f"/api/v1/releases/{release_id}")
        report = portal.get(verification["report"]["url"]) if verification.get("report") else None
        write_json(out / "verification.json", verification)
        write_json(out / "release.json", detail)
        if report is not None:
            write_json(out / "report.json", json.loads(report) if isinstance(report, bytes) else report)
        print(json.dumps({"case": case, "verification": verification["id"], "state": verification["state"],
                          "release_state": detail["release"]["state"], "attempts": verification["attempts"],
                          "scenarios": [(s["name"], s["result"], s.get("surface")) for s in verification["progress"]["scenarios"]],
                          "dimensions": {k: v.get("status") for k, v in (verification["dimensions"] or {}).items()},
                          "reasons": verification["reasons"], "scope": (verification["scope"] or {}).get("label")}, indent=1))
        return 0 if detail["release"]["state"] == "tested" else 1
    finally:
        if worker is not None and worker.poll() is None:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(worker.pid)], capture_output=True)
        if lab is not None:
            lab.shutdown()
        if rebuild is not None:
            rebuild.stop()
        for other in others:
            other.stop()
        portal.stop()


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in CASES:
        raise SystemExit(__doc__)
    raise SystemExit(run(sys.argv[1]))
