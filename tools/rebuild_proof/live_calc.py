"""The live Calculator proof: the built recorder exe records the real Windows Calculator into a
local portal, the package is frozen from it, a blind rebuild is replayed by the portal's Verify.
OPENS CALCULATOR'S WINDOW (the user approved one live run on 2026-11-21).

    python tools/rebuild_proof/live_calc.py serve     # the portal (leave it running; `stop` ends it)
    python tools/rebuild_proof/live_calc.py setup     # app, identity, pairing (the exe), offered session
    python tools/rebuild_proof/live_calc.py record    # the exe records while calc_live.py drives Calculator
    python tools/rebuild_proof/live_calc.py check     # what the desktop channel holds; nothing outside Calculator
    python tools/rebuild_proof/live_calc.py send      # references and upload, through the exe
    python tools/rebuild_proof/live_calc.py stop      # ends `serve`

Evidence: `<evidence>/live-calc/` (REBUILD_PROOF_EVIDENCE, default ../rebuild-proof). The portal is
`python -m twinlab2.portal` on 127.0.0.1:8797 with its own catalog, no password, no worker, no
outside service. The recorder is `dist/TwinLabRecorder/TwinLabRecorder.exe` (python recorder/build.py)
with LOCALAPPDATA pointed at the evidence folder, so nothing lands in the user's own recorder folder.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

os.environ.setdefault("REBUILD_PROOF_PORTAL_PORT", "8797")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import EVIDENCE, ROOT, read_json, run, spawn, write_json  # noqa: E402
import portal_api  # noqa: E402

CASE = "live-calc"
BASE = EVIDENCE / CASE
EXE = ROOT / "dist" / "TwinLabRecorder" / "TwinLabRecorder.exe"
DATA = BASE / "recorder-data"
IDENTITY = "desktop:Microsoft Corporation/Microsoft Calculator"
PURPOSE = ("Windows Calculator: standard arithmetic, a scientific function, the history of "
           "calculations, switching between modes")
SESSION = "Calculator: sums, a square root and the history"


def portal() -> portal_api.Portal:
    p = portal_api.Portal(CASE)
    token = p.http.get("/api/v1/session").json().get("csrf_token")
    p.http.headers["X-CSRF-Token"] = token or ""
    return p


def exe(*args, check=True, log="recorder.log", timeout=1800):
    return run([str(EXE), *args], BASE / "logs" / log, extra_env={"LOCALAPPDATA": str(DATA)},
               check=check, timeout=timeout)


def recordings() -> Path:
    return DATA / "TwinLabRecorder" / "recordings"


def serve() -> int:
    p = portal_api.Portal(CASE).start()
    print(f"portal {portal_api.URL} (pid {p.proc.pid}); `live_calc.py stop` ends it", flush=True)
    stop = BASE / "portal" / "stop"
    stop.unlink(missing_ok=True)
    try:
        while not stop.exists() and p.proc.poll() is None:
            time.sleep(1)
    finally:
        p.stop()
    return 0


def setup() -> int:
    p = portal()
    app_id = p.app("Calculator", PURPOSE, IDENTITY)
    code = p.pairing_code()
    print(exe("pair", "--portal", portal_api.URL, "--code", code, "--name", "Proof workstation").stdout)
    session = p.request_recording(SESSION, [app_id])
    print(exe("sessions").stdout)
    write_json(BASE / "record" / "setup.json", {"app_id": app_id, "session": session})
    print(f"app {app_id}, offered session {session['id']}")
    return 0


def record() -> int:
    import calc_live

    st = read_json(BASE / "record" / "setup.json")
    log = BASE / "logs" / "recorder-record.log"
    before = {p.name for p in recordings().glob("*")} if recordings().is_dir() else set()
    rec = spawn([str(EXE), "record", "--session", str(st["session"]["id"]), "--pictures", "--minutes", "10"],
                log, extra_env={"LOCALAPPDATA": str(DATA)})
    folder = None
    end = time.monotonic() + 120
    while folder is None and time.monotonic() < end:
        time.sleep(0.5)
        new = [p for p in recordings().glob("*T*Z-*") if p.is_dir() and p.name not in before] if recordings().is_dir() else []
        if new:
            folder = new[0]
        if rec.poll() is not None and folder is None:
            raise SystemExit("the recorder stopped before recording: " + log.read_text(encoding="utf-8")[-2000:])
    if folder is None:
        rec.kill()
        raise SystemExit("the recorder did not start a session")
    time.sleep(4)  # the desktop recorder's hooks and UI Automation handlers
    calc = None
    try:
        calc, _hwnd = calc_live.launch()
        print(f"recording {folder.name}; Calculator pid {calc}", flush=True)
        time.sleep(3)  # the watcher admits Calculator (every 2 s it scans for target windows)
        doc = calc_live.drive(calc, BASE / "record" / "driver.json")
        print(json.dumps(doc["seen"]))
        calc_live.shot(calc, BASE / "shots" / "calculator-real.png")
        time.sleep(2)
    finally:
        if calc:
            calc_live.close(calc)
        time.sleep(2)
        (folder / "stop").write_text("", encoding="utf-8")
        try:
            rec.wait(300)
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(rec.pid)], capture_output=True)
            raise SystemExit("the recorder did not stop within 5 minutes") from None
    write_json(BASE / "record" / "session.json", {"session_id": folder.name, "recorder_exit": rec.returncode})
    print(f"recorded {folder.name} (exit {rec.returncode})")
    return 0


def lines(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def check() -> int:
    sid = read_json(BASE / "record" / "session.json")["session_id"]
    folder = recordings() / sid
    desk = folder / "desktop"
    steps = lines(desk / "steps.jsonl")
    journal = lines(folder / "journal.jsonl")
    box = read_json(folder / "portal-outbox.json")
    trees = sorted((desk / "trees").glob("*.json")) if (desk / "trees").is_dir() else []
    pictures = sorted((desk / "pictures").glob("*.png")) if (desk / "pictures").is_dir() else []
    controls = {}
    for t in trees:
        def walk(n):
            if n.get("aid") or n.get("name"):
                controls[(n.get("aid") or "", n.get("type") or "")] = n.get("name")
            for c in n.get("children") or []:
                walk(c)
        walk(read_json(t))
    processes = sorted({s.get("process") for s in steps})
    apps = [r for r in journal if r.get("kind") == "app"]
    presence = [r for r in journal if r.get("kind") == "presence"]
    from collections import Counter

    out = {
        "session": sid, "steps": len(steps), "actions": dict(Counter(s.get("action") for s in steps)),
        "processes_in_steps": processes, "windows": sorted({(s.get("window") or {}).get("selector") or "" for s in steps}),
        "trees": len(trees), "pictures": len(pictures), "controls_with_ids": len([k for k in controls if k[0]]),
        "control_types": dict(Counter(k[1] for k in controls)),
        "sample_controls": sorted(k[0] for k in controls if k[0])[:80],
        "apps_admitted": [{k: r.get(k) for k in ("key", "exe", "level")} for r in apps],
        "presence_rows": dict(Counter(r.get("exe") for r in presence)),
        "outbox_apps": [{"key": a.get("key"), "steps": len(a.get("steps") or [])} for a in box.get("apps") or []],
        "outbox_screens": len(box.get("screens") or []),
        "completeness": (box.get("completeness") or {}).get("channels"),
        "desktop_json_notes": read_json(desk / "desktop.json").get("notes") if (desk / "desktop.json").is_file() else None,
        "pictures_json": read_json(desk / "pictures.json") if (desk / "pictures.json").is_file() else None,
    }
    write_json(BASE / "record" / "check.json", out)
    print(json.dumps(out, indent=1)[:6000])
    return 0


def send() -> int:
    sid = read_json(BASE / "record" / "session.json")["session_id"]
    print(exe("show", sid).stdout[:3000])
    print(exe("send", sid, "--yes").stdout)
    print(exe("upload", sid, check=False).stdout[-1500:])
    p = portal()
    page = p.get("/api/v1/recordings")
    rows = page.get("recordings") if isinstance(page, dict) else page
    write_json(BASE / "record" / "portal-recording.json", p.get(f"/api/v1/recordings/{rows[0]['id']}"))
    print(f"portal recording {rows[0]['id']}")
    return 0


RUN = ("Run settings: a Windows desktop application. The original is Windows Calculator (a UWP / "
       "WinUI app); the copy is a .NET 6 WPF or WinForms program, one executable built with "
       "`dotnet build -c Release`, that opens one main window titled Calculator. It keeps nothing "
       "between starts: every start shows Standard mode, 0 on the display and an empty history.")


def scenarios() -> list[dict]:
    """What the owner saw while the session was recorded (the display read by eye), written as
    a careful owner would: no source code, nothing that was not on the screen."""
    return [
        {"name": "Standard arithmetic: a sum, a product of the result, clear",
         "starting": "Calculator is open in Standard mode with 0 on the display.",
         "actions": "Open the navigation (the menu button at the top left) and choose Standard (already "
                    "the mode). Press 1, 2, +, 7, =. Then x, 3, =. Then C (Clear).",
         "expected": "After 1 2 + 7 = the display shows 19 and the expression line above it shows "
                     "'12 + 7 ='. After x 3 = the display shows 57 ('19 x 3 ='). C sets the display back "
                     "to 0 and empties the expression line. Each finished calculation is added to the "
                     "History list beside the keys (newest first): '12 + 7 =' 19 and '19 x 3 =' 57.",
         "failures": "Not tried in this session (division by zero shows 'Cannot divide by zero' in the "
                     "real Calculator, but it was not recorded here).",
         "examples": "12 + 7 = 19; 19 x 3 = 57.",
         "notes": RUN},
        {"name": "Scientific mode: a square root, the history, back to Standard",
         "starting": "Standard mode, display 0, two calculations in the history.",
         "actions": "Open the navigation and choose Scientific. Press 9 then the square root key. Look "
                    "at the Memory tab beside the keys, then the History tab. Open the navigation and "
                    "choose Standard again.",
         "expected": "Scientific mode shows the header 'Scientific' and the extra keys (trigonometry, "
                     "x², x^y, 10^x, log, ln, |x|, n!, ( ), mod, exp, π, e, 2nd, DEG/F-E). 9 then the "
                     "square root shows 3, with 'sqrt(9)' on the expression line. The History tab lists "
                     "'sqrt(9) =' 3 above the two earlier calculations; the Memory tab says there is "
                     "nothing saved in memory. Choosing Standard brings back the Standard keys; the "
                     "History tab still lists the three calculations.",
         "failures": "Not tried: square root of a negative number ('Invalid input' in the real Calculator).",
         "examples": "sqrt(9) = 3.",
         "questions": "The navigation lists more modes (Graphing, Programmer, Date calculation and the "
                      "converters); only Standard and Scientific were used. A copy may list the others "
                      "without implementing them.",
         "notes": RUN},
    ]


def owner() -> int:
    """The owner's part through /api/v1: accept the partial recording (the desktop channel ended
    on an error after the last step), assign every event to Calculator, two scenarios, the
    package draft (with its completeness), freeze, download the ZIP."""
    p = portal()
    st = read_json(BASE / "record" / "setup.json")
    app_id = st["app_id"]
    recs = p.get("/api/v1/recordings")
    rec_id = (recs.get("recordings") if isinstance(recs, dict) else recs)[0]["id"]
    detail = p.get(f"/api/v1/recordings/{rec_id}")
    if detail["status"].get("attention") and not detail["status"].get("accepted"):
        p.post(f"/api/v1/recordings/{rec_id}/completeness/accept")
    app = next(a for a in detail["refs"]["apps"] if a.get("app_id") == app_id)
    events = [s["event_id"] for s in app["steps"]]
    p.put(f"/api/v1/recordings/{rec_id}/assignments/{app_id}", json={
        "events": events, "notes": "Calculator: every recorded step of this session belongs to it (reviewed)."})
    cut = next((i for i, s in enumerate(app["steps"]) if s.get("label") == "TogglePaneButton" and i > 2), len(events))
    links = [events[:cut], events[cut:] or events]
    have = p.get(f"/api/v1/apps/{app_id}/scenarios")
    if not (have.get("scenarios") if isinstance(have, dict) else have):
        for sc, linked in zip(scenarios(), links):
            p.post(f"/api/v1/apps/{app_id}/scenarios", json={**sc, "events": [f"{rec_id}#{e}" for e in linked]})
    draft = p.get(f"/api/v1/apps/{app_id}/package-draft")
    write_json(BASE / "packages" / "draft.json", draft)
    comp = draft.get("completeness") or {}
    print(json.dumps({"passed": comp.get("passed"), "score": comp.get("score"),
                      "gaps": [(g.get("item"), g.get("severity")) for g in comp.get("gaps") or []]}, indent=1))
    body = {"sha256": draft["sha256"], "approve": True, "summary": f"Calculator: as recorded in '{detail['name']}'"}
    if not comp.get("passed", True):
        body["override_reason"] = "live proof: freeze with the gaps the validator lists, to measure them"
    frozen = p.post(f"/api/v1/apps/{app_id}/packages", json=body, ok=(200, 201))
    data = p.get(f"/api/v1/apps/{app_id}/packages/{frozen['rev']}/download")
    z = BASE / "packages" / "calculator.zip"
    z.write_bytes(data)
    write_json(BASE / "packages" / "summary.json", {"recording": rec_id, "app_id": app_id, "rev": frozen["rev"],
                                                    "sha256": frozen.get("sha256"), "events": len(events),
                                                    "zip": str(z), "bytes": len(data)})
    print(f"frozen rev {frozen['rev']}: {len(events)} events -> {z} ({len(data)} bytes)")
    return 0


REPO = BASE / "build" / "repo"
EXE_NAME = "Calculator.exe"
# judge side (no builder saw it): each form the scenarios name is shown where the recording
# showed it. Calculator's results are data to the recorder (the display's text is withheld), so
# no case can say "the display shows 57": see REPORT.md.
SCIENTIFIC_ONLY = "uia:AutomationId=logBase10Button;ControlType=Button"


def acceptance(steps: list[dict]) -> dict:
    by_label = [(s["seq"], (s.get("target") or {}).get("props", {}).get("AutomationId")) for s in steps]
    after_sqrt = next((q for q, a in by_label if a == "squareRootButton"), "end")
    after_clear = next((q for q, a in by_label if a == "clearButton"), "end")
    window = (steps[0].get("window") or {}).get("selector")
    cases = [
        {"id": "main-window", "after": 1, "check": "window", "selector": window,
         "note": "the Calculator window opens"},
        {"id": "standard-keys", "after": after_clear, "check": "control",
         "selector": "uia:AutomationId=equalButton;ControlType=Button", "note": "Standard mode has its keys"},
        {"id": "standard-has-no-log", "after": after_clear, "check": "control", "selector": SCIENTIFIC_ONLY,
         "expect": "absent", "note": "Standard mode does not show the scientific keys"},
        {"id": "scientific-keys", "after": after_sqrt, "check": "control", "selector": SCIENTIFIC_ONLY,
         "note": "after choosing Scientific, its keys (log) are shown"},
        {"id": "history-tab", "after": "end", "check": "control",
         "selector": "uia:AutomationId=HistoryLabel;ControlType=TabItem", "note": "History is beside the keys"},
    ]
    return {"version": 1, "cases": cases}


def judge_folder(rec_id: str, folder: Path) -> Path:
    import shutil

    import yaml

    root = BASE / "judge-desktop"
    out = root / rec_id / "desktop"
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(folder / "desktop", out)
    steps = lines(out / "steps.jsonl")
    (out / "acceptance.yaml").write_text(yaml.safe_dump(acceptance(steps), sort_keys=False), encoding="utf-8")
    (out / "bindings.yaml").write_text(yaml.safe_dump({"fields": {}}), encoding="utf-8")
    return root


def verify() -> int:
    """The portal's real Verify of the rebuild: a Packaged release of the rebuild's commit, its
    exe in the verifier's builds folder with `.twinlab/version`, `POST .../verify` with
    targets.desktop, worked by `python -m twinlab2.worker --types verify` with
    TWINLAB2_DESKTOP_VERIFY=1. OPENS THE REBUILT APP'S WINDOW (no other window)."""
    import shutil

    commit = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True,
                            check=True).stdout.strip()
    subprocess.run(["dotnet", "build", "-c", "Release", "-nologo", "-v", "q"], cwd=REPO, check=True,
                   capture_output=True, creationflags=0x08000000)
    found = sorted(p for p in REPO.rglob(EXE_NAME) if "bin" in p.parts and "Release" in p.parts)
    if not found:
        raise SystemExit(f"no {EXE_NAME} under {REPO}")
    builds = BASE / "verifier-builds"
    target = builds / "calculator"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(found[0].parent, target)
    (target / ".twinlab").mkdir(exist_ok=True)
    (target / ".twinlab" / "version").write_text(json.dumps({"commit": commit}), encoding="utf-8")
    p = portal()
    st = read_json(BASE / "record" / "setup.json")
    app_id = st["app_id"]
    sid = read_json(BASE / "record" / "session.json")["session_id"]
    rec_id = "ws-" + sid
    judges = judge_folder(rec_id, recordings() / sid)
    catalog = BASE / "portal" / "catalog.db"
    extra = {"TWINLAB2_STATE": str(BASE / "portal" / "state"), "DATABASE_URL": "", "TWINLAB2_PASSWORD": ""}
    rel = p.get(f"/api/v1/apps/{app_id}")["releases"]
    if not any(r["commit_sha"] == commit for r in rel):
        rev = p.get(f"/api/v1/apps/{app_id}/packages")["packages"][0]["revision"]
        done = run([sys.executable, "-m", "twinlab2.portal", "--database", str(catalog), "release", str(app_id),
                    commit[:12], rev, str(REPO)], BASE / "logs" / "release.log", extra_env=extra, cwd=ROOT)
        print(done.stdout.strip())
    release_id = next(r["id"] for r in p.get(f"/api/v1/apps/{app_id}")["releases"] if r["commit_sha"] == commit)
    out = BASE / "verify"
    out.mkdir(parents=True, exist_ok=True)
    worker = spawn([sys.executable, "-m", "twinlab2.worker", "--database", str(catalog), "--types", "verify",
                    "--idle", "0.5"], out / "worker.log",
                   extra_env={**extra, "TWINLAB2_RECORDINGS": str(judges), "TWINLAB2_LAB_TOKEN": "",
                              "TWINLAB2_DESKTOP_VERIFY": "1", "TWINLAB2_DESKTOP_BUILDS": str(builds)})
    try:
        queued = p.post(f"/api/v1/releases/{release_id}/verify", ok=(202,), json={
            "request_key": f"live-calc-{commit[:12]}-{int(time.time())}",
            "targets": {"desktop": f"calculator/{EXE_NAME}"}})
        print(f"POST /api/v1/releases/{release_id}/verify -> 202, verification {queued['id']}")
        verification, seen, end = queued, None, time.monotonic() + 1800
        while verification["state"] in ("queued", "running"):
            if time.monotonic() > end:
                raise SystemExit("the verification did not end within 30 minutes")
            time.sleep(1.0)
            verification = p.get(f"/api/v1/verifications/{queued['id']}")
            line = (verification["state"], verification["progress"]["stage"], verification["progress"]["done"],
                    verification["progress"]["total"])
            if line != seen:
                print(f"  {line[0]}: {line[1]} ({line[2]} of {line[3]})", flush=True)
                seen = line
        detail = p.get(f"/api/v1/releases/{release_id}")
        report = p.get(verification["report"]["url"]) if verification.get("report") else None
        write_json(out / "verification.json", verification)
        write_json(out / "release.json", detail)
        if report is not None:
            write_json(out / "report.json", json.loads(report) if isinstance(report, bytes) else report)
        print(json.dumps({"verification": verification["id"], "state": verification["state"],
                          "release_state": detail["release"]["state"],
                          "scenarios": [(s["name"], s["result"]) for s in verification["progress"]["scenarios"]],
                          "dimensions": {k: v.get("status") for k, v in (verification["dimensions"] or {}).items()},
                          "reasons": verification["reasons"]}, indent=1))
    finally:
        if worker.poll() is None:
            worker.terminate()
            try:
                worker.wait(20)
            except subprocess.TimeoutExpired:
                worker.kill()
    return 0


def main(argv) -> int:
    what = argv[1] if len(argv) > 1 else ""
    (BASE / "logs").mkdir(parents=True, exist_ok=True)
    if what == "stop":
        (BASE / "portal" / "stop").write_text("", encoding="utf-8")
        return 0
    fn = {"serve": serve, "setup": setup, "record": record, "check": check, "send": send,
          "owner": owner, "verify": verify}.get(what)
    if fn is None:
        raise SystemExit(__doc__)
    return fn()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
