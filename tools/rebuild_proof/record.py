"""Step 1 of the proof: record each case with the real recorder, headless, into a portal the
harness starts, and send the references (and the recording upload) to it.

    python tools/rebuild_proof/record.py a|b|c|d

What runs is the recorder's own command line, from this checkout's `recorder/`:
`python -m twinlab2_recorder pair ...`, then `record --session N --cdp http://127.0.0.1:9341`
(the documented "attach to a running browser" option), `send`, `upload`. Its collectors,
supervisor, journals, timeline and outbox code all run unchanged. What differs from a recording
on a person's PC:

- the browser is Playwright's Chromium started by the harness with ``--headless=new`` and a
  DevTools port, and the recorder attaches to it (``--cdp``) instead of starting its own Edge
  window. The recorder's attach path (`web.recorder.attach`) is the one a robot's browser uses;
- the person is a script (drive.py) that clicks and types through Playwright over the same
  DevTools connection, with per-key typing so the page sees ordinary input events;
- the 3270 terminal is Twin Lab's scripted socket terminal pointed at the recorder's proxy;
- the recorder's data folder is `<evidence>/<case>/recorder-data` (LOCALAPPDATA for the
  recorder process), so nothing lands in the user's own recorder folder;
- **desktop (cases C and D)**: no desktop collector runs now (it hooks the keyboard and mouse
  of the PC the user is working on). The CaseDesk steps are Twin Lab's earlier real UI
  Automation recording of CaseDesk (`casedesk-rec`, 36 steps, recorded 2026-11-18 by the same
  collector code), spliced into the session folder with fresh event ids and shifted times,
  after which the recorder's own `timeline.build` writes the outbox again. desktop_batch.ps1
  records CaseDesk live on "go desktop".
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import drive  # noqa: E402
from common import (CASEDESK_REC, EVIDENCE, PORTS, ROOT, chromium, no_window, port_free, read_json,  # noqa: E402
                    run, spawn, wait_port, write_json)
from portal_api import URL, Portal  # noqa: E402

CDP_PORT = 9341
PY = sys.executable

CASES = {
    "a": {"name": "Orders: a new order is taken", "refapps": ["web-a"],
          "apps": [("Orders", "Customer orders: search, new orders with their items", "http://127.0.0.1:18751")]},
    "b": {"name": "Account inquiry on the host", "refapps": ["tn3270"],
          "apps": [("Account Inquiry", "Mainframe account inquiry: sign on, look up accounts", "tn3270://127.0.0.1:13270")]},
    "c": {"name": "Order to invoice, account check and case update", "refapps": ["web-c", "tn3270"],
          "apps": [("Orders", "Customer orders: search, new orders with their items", "http://127.0.0.1:18761"),
                   ("Billing", "Invoices for orders", "http://127.0.0.1:18761/billing"),
                   ("Account Inquiry", "Mainframe account inquiry: sign on, look up accounts", "tn3270://127.0.0.1:13270")],
          "desktop": ("CaseDesk", "Case handling: find a case, update its summary and status",
                      "desktop:Twin Lab reference/CaseDesk")},
    # w: Orders and Billing on one host, recorded in the recorder's OWN browser (launched Edge,
    # `record --headless`: no window), not attached; web only
    "w": {"name": "Order to invoice (launched browser)", "refapps": ["web-c"], "launched": True,
          "apps": [("Orders", "Customer orders: search, new orders with their items", "http://127.0.0.1:18761"),
                   ("Billing", "Invoices for orders", "http://127.0.0.1:18761/billing")]},
    # e: the desktop batch only ("go desktop"): CaseDesk recorded LIVE by the desktop collector
    "e": {"name": "CaseDesk: a case is resolved (live desktop recording)", "refapps": [],
          "apps": [("CaseDesk", "Case handling: find a case, update its summary and status",
                    "desktop:Twin Lab reference/CaseDesk")]},
    "d": {"name": "CaseDesk: a case is resolved", "refapps": [], "apps": [],
          "desktop": ("CaseDesk", "Case handling: find a case, update its summary and status",
                      "desktop:Twin Lab reference/CaseDesk")},
}


def recorder(case: str, *args, check=True, log=None):
    data = EVIDENCE / case / "recorder-data"
    return run([PY, "-m", "twinlab2_recorder", *args], log or EVIDENCE / case / "logs" / "recorder.log",
               extra_env={"LOCALAPPDATA": str(data)}, check=check, timeout=1800)


def recordings_dir(case: str) -> Path:
    return EVIDENCE / case / "recorder-data" / "TwinLabRecorder" / "recordings"


def start_refapps(case: str) -> list[subprocess.Popen]:
    procs = []
    for what in CASES[case]["refapps"]:
        port = PORTS["tn3270"] if what == "tn3270" else (PORTS["a"]["orders"] if what == "web-a" else PORTS["c"]["shared"])
        if not port_free(port):
            raise SystemExit(f"port {port} is taken: a reference app is already running?")
        procs.append(spawn([PY, str(Path(__file__).with_name("refapps.py")), what],
                           EVIDENCE / case / "logs" / f"refapps-{what}.log"))
        wait_port(port, timeout=30)
    return procs


def start_chromium(case: str) -> subprocess.Popen:
    profile = EVIDENCE / case / "chromium-profile"
    shutil.rmtree(profile, ignore_errors=True)
    proc = subprocess.Popen([chromium(), "--headless=new", f"--remote-debugging-port={CDP_PORT}",
                             f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
                             "--window-size=1280,720", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=no_window())
    wait_port(CDP_PORT, timeout=30)
    return proc


def launched_cdp(folder: Path) -> str:
    """The DevTools address of the browser the recorder started (its journal's `browser` row)."""
    for line in (folder / "journal.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(line) if line.strip() else {}
        if row.get("kind") == "browser" and row.get("cdp"):
            return row["cdp"]
    raise SystemExit("the recorder did not say where its browser listens")


def stop(procs) -> None:
    for p in procs:
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(15)
            except subprocess.TimeoutExpired:
                p.kill()


def splice_desktop(folder: Path, app: dict, after_ts: str | None) -> int:
    """Put Twin Lab's recorded CaseDesk steps into a session folder as its desktop channel.
    Every step gets a new event id, channel `desktop` and a capture time; times are shifted to
    start 2 s after `after_ts` (or keep the session's start). -> number of steps."""
    from datetime import datetime, timedelta, timezone
    from twinlab2_recorder.workstation import journal

    def parse(ts):
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))

    def fmt(dt):
        return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    src = [json.loads(line) for line in (CASEDESK_REC / "steps.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    start = parse(after_ts) + timedelta(seconds=2) if after_ts else datetime.now(timezone.utc)
    shift = start - parse(src[0]["ts"])
    out = folder / "desktop"
    (out / "trees").mkdir(parents=True, exist_ok=True)
    for tree in (CASEDESK_REC / "trees").glob("*.json"):
        shutil.copy2(tree, out / "trees" / tree.name)
    shutil.copy2(CASEDESK_REC / "desktop.json", out / "desktop.json")
    with open(out / "steps.jsonl", "w", encoding="utf-8") as f:
        for s in src:
            s["ts"] = fmt(parse(s["ts"]) + shift)
            journal.stamp(s, "desktop", s["ts"])
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    pid = src[0].get("pid")
    row = {"ts": fmt(start), "kind": "app", "pid": pid, "key": app["key"], "surface": "desktop",
           "exe": "CaseDesk.exe", "company": "Twin Lab reference", "product": "CaseDesk", "version": "1.0.0.0",
           "toolkit": "winforms", "level": "full", "display": app["display"]}
    if app.get("app_id"):
        row["app_id"] = app["app_id"]
    with open(folder / "journal.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    meta = read_json(folder / "session.json")
    meta.setdefault("apps", {})[app["key"]] = {k: v for k, v in row.items() if k not in ("ts", "kind", "pid")}
    last = parse(src[-1]["ts"])
    if not meta.get("ended") or parse(meta["ended"]) < last:
        meta["ended"] = fmt(last + timedelta(seconds=1))
    meta.setdefault("notes", []).append(
        "rebuild proof: desktop steps spliced from Twin Lab's casedesk-rec recording (not recorded live)")
    write_json(folder / "session.json", meta)
    return len(src)


def contract_fixups(folder: Path) -> list[str]:
    """GAP (recorder): with `--cdp`, the session's own note on attach mode is longer than the 300
    characters contract 1.3.0 allows in `completeness.channels.<ch>.notes`, so `send` refuses
    every attached recording. Cut such notes to 300 characters in the outbox (and say so)."""
    path = folder / "portal-outbox.json"
    box = read_json(path)
    fixed = []
    for ch, block in ((box.get("completeness") or {}).get("channels") or {}).items():
        notes = block.get("notes") or []
        for i, note in enumerate(notes):
            if isinstance(note, str) and len(note) > 300:
                notes[i] = note[:297] + "..."
                fixed.append(f"completeness.channels.{ch}.notes[{i}] cut from {len(note)} to 300 characters")
    if fixed:
        write_json(path, box)
    return fixed


def last_step_ts(folder: Path) -> str | None:
    ts = []
    for path in [folder / "web" / "steps.jsonl", folder / "mainframe" / "steps.jsonl"]:
        if path.is_file():
            ts += [json.loads(line)["ts"] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return max(ts) if ts else None


def desktop_only_session(case: str, app: dict) -> Path:
    """Case D: a session folder holding only the CaseDesk channel (spliced), written the way the
    recorder's Session writes one, then `timeline.build`."""
    from twinlab2_recorder.workstation import timeline
    from twinlab2_recorder.workstation.session import new_id
    from datetime import datetime, timezone

    sid = new_id()
    folder = recordings_dir(case) / sid
    folder.mkdir(parents=True)
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    write_json(folder / "session.json", {
        "format": timeline.FORMAT, "id": sid, "name": CASES[case]["name"], "state": "stopped",
        "started": now, "ended": None, "recorder": "TwinLabRecorder 0.2.0", "twinlab_commit": None,
        "automation_id": None, "portal_session_id": None,
        "targets": [{"exe": "casedesk.exe", "level": "full", **({"app_id": app["app_id"]} if app.get("app_id") else {})}],
        "deny_keys": [], "channels": ["desktop"], "apps": {}, "notes": []})
    (folder / "journal.jsonl").write_text("", encoding="utf-8")
    splice_desktop(folder, app, None)
    timeline.build(folder)
    return folder


def main(argv) -> int:
    case = argv[1] if len(argv) > 1 else ""
    if case not in CASES:
        raise SystemExit(__doc__)
    spec = CASES[case]
    base = EVIDENCE / case
    if base.exists():  # an earlier attempt is kept beside it, never deleted
        base.rename(base.with_name(f"{case}-attempt-{time.strftime('%Y%m%dT%H%M%S')}"))
    (base / "logs").mkdir(parents=True)
    procs = start_refapps(case)
    portal = Portal(case).start()
    chrome = None
    try:
        ids = {}
        for name, purpose, identity in spec["apps"]:
            ids[name] = portal.app(name, purpose, identity)
        desk = None
        if spec.get("desktop"):
            name, purpose, identity = spec["desktop"]
            ids[name] = portal.app(name, purpose, identity)
            desk = {"key": identity, "display": name, "app_id": ids[name]}
        code = portal.pairing_code()
        recorder(case, "pair", "--portal", URL, "--code", code, "--name", "Proof workstation")
        result: dict = {}
        if case == "d":
            folder = desktop_only_session(case, desk)
            sid = folder.name
        else:
            requested = portal.request_recording(spec["name"], [ids[n] for n, _, _ in spec["apps"]])
            offered = recorder(case, "sessions").stdout
            number = requested["id"]
            web = any(i.startswith("http") for _, _, i in spec["apps"])
            if case == "e" and "--go-desktop" not in argv:
                raise SystemExit("case e opens windows and hooks input: only desktop_batch.ps1 runs it")
            args = ["record", "--session", str(number), "--minutes", "20"]
            if web and spec.get("launched"):
                args += ["--headless"]  # the recorder starts Edge itself, headless
            elif web:
                chrome = start_chromium(case)
                args += ["--cdp", f"http://127.0.0.1:{CDP_PORT}"]
            log = base / "logs" / "recorder-record.log"
            e = {"LOCALAPPDATA": str(base / "recorder-data")}
            rec = spawn([PY, "-m", "twinlab2_recorder", *args], log, extra_env=e)
            sid = None
            end = time.monotonic() + 120
            while sid is None and time.monotonic() < end:
                time.sleep(0.5)
                for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.startswith("recording session "):
                        sid = line.split()[2].rstrip(":")
                found = [p.name for p in recordings_dir(case).glob("*T*Z-*") if p.is_dir()]
                if sid is None and found:
                    sid = found[0]
                if rec.poll() is not None and sid is None:
                    raise SystemExit("the recorder stopped before recording: " + log.read_text(encoding="utf-8")[-2000:])
            if sid is None:
                rec.kill()
                raise SystemExit("the recorder did not start a session")
            folder = recordings_dir(case) / sid
            # wait for the collectors: the browser attached, the 3270 proxy listening
            end = time.monotonic() + 60
            while time.monotonic() < end:
                j = (folder / "journal.jsonl").read_text(encoding="utf-8") if (folder / "journal.jsonl").is_file() else ""
                ready_web = not web or '"browser"' in j
                ready_mf = case in ("a", "e", "w") or not port_free(PORTS["proxy"])
                if ready_web and ready_mf:
                    break
                time.sleep(0.5)
            time.sleep(3)
            result["offered"] = offered
            try:
                if case == "e":
                    import desktop_live

                    exe = desktop_live.REF_BIN / "CaseDesk.exe"
                    app_proc = subprocess.Popen([str(exe)])
                    time.sleep(3)
                    desktop_live.drive(app_proc.pid)
                    try:
                        app_proc.wait(20)
                    except subprocess.TimeoutExpired:
                        app_proc.kill()
                    result["desktop"] = "reference CaseDesk driven by Twin Lab's flow.py"
                elif case == "a":
                    result.update(drive.case_a(f"http://127.0.0.1:{CDP_PORT}", "http://127.0.0.1:18751"))
                elif case == "w":
                    result["cdp"] = launched_cdp(folder)
                    result.update(drive.case_w(result["cdp"], "http://127.0.0.1:18761"))
                elif case == "b":
                    result.update(drive.case_b(PORTS["proxy"]))
                else:
                    result.update(drive.case_c(f"http://127.0.0.1:{CDP_PORT}", "http://127.0.0.1:18761", PORTS["proxy"]))
            finally:
                time.sleep(2)
                (folder / "stop").write_text("", encoding="utf-8")
                try:
                    rec.wait(600)
                except subprocess.TimeoutExpired:
                    subprocess.run(["taskkill", "/T", "/F", "/PID", str(rec.pid)], capture_output=True)
                    raise SystemExit("the recorder did not stop within 10 minutes")
            if case == "c":
                from twinlab2_recorder.workstation import timeline

                result["desktop_steps_spliced"] = splice_desktop(folder, desk, last_step_ts(folder))
                timeline.build(folder)
        result["contract_fixups"] = contract_fixups(folder)
        print(recorder(case, "send", sid, "--yes").stdout)
        result["sent"] = read_json(folder / "sent.json")
        up = recorder(case, "upload", sid, check=False)
        result["upload"] = up.stdout.strip()[-2000:]
        result["session_id"] = sid
        write_json(base / "record" / "driver.json", result)
        shutil.copytree(folder, base / "recording" / sid)
        rec_row = portal.wait_recording()
        write_json(base / "record" / "portal-recording.json", portal.get(f"/api/v1/recordings/{rec_row['id']}"))
        write_json(base / "record" / "apps.json", ids)
        print(f"case {case}: session {sid} -> portal recording {rec_row['id']}; apps {ids}")
    finally:
        portal.stop()
        stop([chrome] + procs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
