"""The desktop batch (run only from desktop_batch.ps1, after the user says "go desktop"): it
OPENS WINDOWS on this PC (CaseDesk, its message boxes) and the recorder's desktop collector
hooks the keyboard and mouse while it records.

    python tools/rebuild_proof/desktop_live.py build-reference   # CaseDesk.exe into <evidence>/e/ref-bin
    python tools/rebuild_proof/desktop_live.py drive <pid-file>  # the scripted clerk (Twin Lab's flow)
    python tools/rebuild_proof/desktop_live.py replay            # Twin Lab's desktop verify, every exe
    python tools/rebuild_proof/desktop_live.py portal-verify     # the portal's own desktop Verify, every exe

- **Record** (case `e`, via record.py e): the real recorder, `record --session N` with CaseDesk as
  the session's target, its desktop collector running (UI Automation, hooks), while the
  reference CaseDesk.exe is driven by Twin Lab's own scripted flow
  (`reference/desktop/CaseDesk/flow.py: flow(pids)`, real clicks only while CaseDesk is in the
  foreground). Then the owner's step (owner.py e) and a package from the LIVE recording.
- **Replay**: the portal has no desktop replay, so Twin Lab's `labctl.desktop.verify.verify`
  (start the exe, replay the recording through UI Automation, compare every window's controls)
  runs for: the reference exe (positive control), and each blind rebuild (builds/c-casedesk,
  builds/d-casedesk), against the live recording and the earlier spliced one (casedesk-rec).
  Reports go to `<evidence>/e/replay/`.
- **Portal Verify** (`portal-verify`): the portal's own desktop replay (`twinlab2.replay.desktop`,
  run as Verify's worker runs it: `independent.TwinLabChecks.replay(..., surface="desktop")`, in
  its own process with TWINLAB2_DESKTOP_VERIFY=1) for the same exes and recordings, with the
  judge's bindings.yaml (VALUES) and acceptance.yaml (each named form opens) beside a copy of the
  recording. Reports: `<evidence>/e/replay/portal-<recording>--<exe>.json` and
  `portal-desktop-summary.json`.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import CASEDESK_REC, CASEDESK_SRC, EVIDENCE, TWINLAB, write_json  # noqa: E402

REF_BIN = EVIDENCE / "e" / "ref-bin"
# AutomationId -> values typed there, in order (judge side, as the flow typed them: flow.py's
# USER, BAD_PASSWORD, PASSWORD and the search and summary texts)
VALUES = {"txtUser": ["tester", "tester"], "txtPassword": ["wrong", "demo"], "txtSearch": ["C-100"],
          "txtSummary": ["Done", "Card resent by courier"]}
REBUILDS = {"c-casedesk": EVIDENCE / "builds" / "c-casedesk" / "repo",
            "d-casedesk": EVIDENCE / "builds" / "d-casedesk" / "repo"}


def build_reference() -> Path:
    """Build Twin Lab's reference CaseDesk into the evidence folder (the twin-lab checkout is not
    written to)."""
    REF_BIN.mkdir(parents=True, exist_ok=True)
    subprocess.run(["dotnet", "build", str(CASEDESK_SRC / "CaseDesk.csproj"), "-c", "Release", "-nologo",
                    "-v", "q", "-o", str(REF_BIN)], check=True)
    return REF_BIN / "CaseDesk.exe"


def rebuilt_exe(repo: Path) -> Path | None:
    subprocess.run(["dotnet", "build", "-c", "Release", "-nologo", "-v", "q"], cwd=repo, check=True)
    found = sorted(repo.rglob("bin/Release/net6.0-windows/CaseDesk.exe"))
    return found[0] if found else None


def drive(pid: int) -> None:
    """Twin Lab's scripted CaseDesk flow on the running reference CaseDesk."""
    sys.path.insert(0, str(TWINLAB / "lab" / "labctl"))
    from refapps import load

    flow = load(CASEDESK_SRC / "flow.py", "twinlab_casedesk_flow")
    flow.flow({pid})


def labctl_verify(exe: Path, recording: Path) -> dict:
    sys.path.insert(0, str(TWINLAB / "lab" / "labctl"))
    from labctl.desktop import verify as dverify

    report = dverify.verify(str(exe), recording, values=VALUES)
    return json.loads(report.model_dump_json())


def replay() -> int:
    out = EVIDENCE / "e" / "replay"
    out.mkdir(parents=True, exist_ok=True)
    live = next((p for p in (EVIDENCE / "e" / "recording").iterdir() if p.is_dir()), None)
    recordings = {"spliced-casedesk-rec": CASEDESK_REC}
    if live is not None and (live / "desktop" / "steps.jsonl").is_file():
        recordings["live"] = live / "desktop"
    exes = {"reference": REF_BIN / "CaseDesk.exe"}
    for name, repo in REBUILDS.items():
        exe = rebuilt_exe(repo)
        if exe:
            exes[name] = exe
    summary = []
    for rname, rec in recordings.items():
        for ename, exe in exes.items():
            try:
                report = labctl_verify(exe, rec)
                steps = report.get("steps") or []
                row = {"recording": rname, "exe": ename, "status": report.get("status"), "steps": len(steps),
                       "passed": sum(1 for s in steps if s.get("status") == "pass"),
                       "failed": [s.get("seq") for s in steps if s.get("status") != "pass"][:40],
                       "problems": (report.get("problems") or [])[:10]}
                write_json(out / f"{rname}--{ename}.json", report)
            except Exception as exc:  # noqa: BLE001 - one exe failing to start does not stop the batch
                row = {"recording": rname, "exe": ename, "status": "error", "error": f"{type(exc).__name__}: {exc}"}
            summary.append(row)
            print(json.dumps(row))
            time.sleep(2)
    write_json(out / "desktop-summary.json", summary)
    return 0


def judge_copy(name: str, rec: Path) -> Path:
    """<evidence>/e/judge-desktop/<name>/desktop: the recording with the judge's files beside it."""
    import shutil

    import yaml

    out = EVIDENCE / "e" / "judge-desktop" / name / "desktop"
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(rec, out)
    (out / "bindings.yaml").write_text(yaml.safe_dump({"fields": VALUES}), encoding="utf-8")
    steps = [json.loads(line) for line in (rec / "steps.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    cases, seen = [], set()
    for step in steps:
        sel = (step.get("window") or {}).get("selector") or ""
        if step.get("action") == "window_open" and "AutomationId=" in sel and sel not in seen:
            seen.add(sel)
            cases.append({"id": f"opens-{sel.split('AutomationId=')[1].split(';')[0]}", "after": step["seq"],
                          "check": "window", "selector": sel, "note": "the form opens where the recording opened it"})
    (out / "acceptance.yaml").write_text(yaml.safe_dump({"version": 1, "cases": cases}), encoding="utf-8")
    return out.parent


def portal_verify() -> int:
    """The portal's desktop Verify (what the worker runs) for every exe and recording. OPENS WINDOWS."""
    import os

    os.environ["TWINLAB2_DESKTOP_VERIFY"] = "1"
    from twinlab2 import independent

    out = EVIDENCE / "e" / "replay"
    out.mkdir(parents=True, exist_ok=True)
    folder = EVIDENCE / "e" / "recording"
    live = next((p for p in folder.iterdir() if p.is_dir()), None) if folder.is_dir() else None
    recordings = {"spliced-casedesk-rec": CASEDESK_REC}
    if live is not None and (live / "desktop" / "steps.jsonl").is_file():
        recordings["live"] = live / "desktop"
    exes = {"reference": REF_BIN / "CaseDesk.exe"}
    for name, repo in REBUILDS.items():
        exe = rebuilt_exe(repo)
        if exe:
            exes[name] = exe
    summary = []
    for rname, rec in recordings.items():
        judge = judge_copy(rname, rec)
        runner = independent.TwinLabChecks(recordings=judge.parent, timeout=600)
        for ename, exe in exes.items():
            report = runner.replay(judge.name, None, surface="desktop", target=str(exe))
            steps = report.get("steps") or []
            row = {"recording": rname, "exe": ename, "status": report.get("status"), "steps": len(steps),
                   "passed": sum(1 for s in steps if s.get("status") == "pass"),
                   "failed": [s.get("seq") for s in steps if s.get("status") == "fail"][:40],
                   "cases": [(c.get("id"), c.get("status")) for c in report.get("cases") or []],
                   "problems": (report.get("problems") or [])[:10]}
            write_json(out / f"portal-{rname}--{ename}.json", report)
            summary.append(row)
            print(json.dumps(row))
            time.sleep(2)
    write_json(out / "portal-desktop-summary.json", summary)
    return 0


def main(argv) -> int:
    what = argv[1] if len(argv) > 1 else ""
    if what == "build-reference":
        print(build_reference())
    elif what == "drive":
        drive(int(argv[2]))
    elif what == "replay":
        return replay()
    elif what == "portal-verify":
        return portal_verify()
    else:
        raise SystemExit(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
