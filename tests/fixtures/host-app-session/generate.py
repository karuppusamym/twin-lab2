"""A workstation session with applications that run inside host processes: three Java programs
(two under javaw.exe started with `-jar`, one Web Start application), two Electron applications
under electron.exe, a .NET application under dotnet.exe, and host processes whose application
cannot be told (an inline `python -c`, a `node index.js` with no package name, `java @argfile`).

    python tests/fixtures/host-app-session/generate.py OUT_FOLDER

Every application file is written for real (jars with and without a manifest title, an Electron
app folder and an `.asar`, a JNLP file); each process's identity is what the recorder computes
from its command line (`workstation.hosts.resolve`), its journal row what the session writes
(`session.app_facts`), and the references what the timeline builds at Stop (`timeline.build`).
Nothing in them is written by hand, so no folder of this machine reaches them. Synthetic.

Separation traps: the two `-jar` programs share javaw.exe (their steps go by process id, not by
executable name); the Java program titled "Billing" and the Electron app named "Billing" have
the same name; the session's targets name the hosts (`exe: javaw.exe`, `exe: electron.exe`),
and a catalog application "Java Tools" is known by `exe:javaw.exe`, so a target's catalog id
must not be stamped on every Java program; the .NET application is in the catalog by its key.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import struct
import sys
import zipfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for path in (ROOT / "recorder", ROOT / "contract"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from twinlab2_recorder.workstation import hosts, levels, session, timeline  # noqa: E402

SESSION_ID = "20261120T160000Z-4057a11e"
JAVAW = r"C:\Program Files\Eclipse Adoptium\jdk-21.0.5\bin\javaw.exe"
ELECTRON = r"C:\tools\electron-v31\electron.exe"
DOTNET = r"C:\Program Files\dotnet\dotnet.exe"

# The catalog of the offered session (ids 1-2): the .NET program by its identity key, and a Java
# tools application an owner once registered by its executable name.
CATALOG = [
    {"name": "Payroll Client", "key": "desktop:Northwind Traders/Payroll Client", "surface": "desktop"},
    {"name": "Java Tools", "key": "exe:javaw.exe", "surface": "desktop"},
]
TARGETS = [
    {"level": "full", "app_id": 1, "name": "Payroll Client", "key": "desktop:Northwind Traders/Payroll Client"},
    {"level": "full", "app_id": 2, "name": "Java Tools", "exe": "javaw.exe"},
    # the person added these on the recorder
    {"level": "full", "exe": "electron.exe"},
    {"level": "full", "exe": "python.exe"},
    {"level": "full", "exe": "node.exe"},
    {"level": "full", "exe": "java.exe"},
]


def write_jar(path: Path, manifest: str) -> None:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("META-INF/MANIFEST.MF", manifest)


def write_asar(path: Path, files: dict[str, bytes]) -> None:
    body, index, offset = b"", {}, 0
    for name, data in files.items():
        index[name] = {"size": len(data), "offset": str(offset)}
        body, offset = body + data, offset + len(data)
    header = json.dumps({"files": index}).encode()
    padded = header + b"\0" * (-len(header) % 4)
    pickle = struct.pack("<II", len(padded) + 4, len(header)) + padded
    path.write_bytes(struct.pack("<II", 4, len(pickle)) + pickle + body)


def app_files(home: Path) -> dict:
    """The application files under `home` (a person's folder: it must never reach the
    references), and the processes: {pid: (image path, version resource, argv, cwd)}."""
    apps = home / "apps"
    (apps / "billing-ui").mkdir(parents=True)
    write_jar(apps / "billing-client.jar", "Manifest-Version: 1.0\nMain-Class: com.acme.billing.Main\n")
    write_jar(apps / "ledger-7.3.jar", "Manifest-Version: 1.0\nImplementation-Title: Billing\n"
                                       "Implementation-Version: 7.3\n")
    (apps / "billing-ui" / "package.json").write_text(json.dumps({"name": "billing-ui", "productName": "Billing",
                                                                  "version": "1.0.0"}), encoding="utf-8")
    write_asar(apps / "stock-viewer.asar", {"package.json": b'{"name": "stock-viewer", "version": "2.1.0"}'})
    cache = home / "AppData" / "LocalLow" / "Sun" / "Java" / "Deployment" / "cache" / "6.0" / "12"
    cache.mkdir(parents=True)
    (cache / "5c2e7a1b-41d03f9a").write_text(
        '<?xml version="1.0"?><jnlp spec="1.0+" codebase="https://claims.example.test/desk" href="claims.jnlp">'
        "<information><title>Claims Desk</title><vendor>Fabrikam</vendor></information></jnlp>", encoding="utf-8")
    (apps / "args.txt").write_text("-jar billing-client.jar", encoding="utf-8")
    (home / "scratch").mkdir()
    node, python = r"C:\Program Files\nodejs\node.exe", r"C:\Python313\python.exe"
    return {
        41: (JAVAW, {}, ["javaw.exe", "-Xmx1g", "-Duser.home=" + str(home), "-jar", "billing-client.jar",
                         "--profile", "ana"], str(apps)),
        42: (JAVAW, {}, ["javaw.exe", "-jar", str(apps / "ledger-7.3.jar")], None),
        43: (JAVAW, {}, ["javaw.exe", "-classpath", str(home / "deploy.jar"), "com.sun.javaws.Main",
                         str(cache / "5c2e7a1b-41d03f9a")], None),
        44: (ELECTRON, {"company": "GitHub, Inc.", "product": "Electron"}, ["electron.exe", str(apps / "billing-ui")], None),
        45: (ELECTRON, {"company": "GitHub, Inc.", "product": "Electron"},
             ["electron.exe", "--js-flags=--expose-gc", str(apps / "stock-viewer.asar")], None),
        46: (DOTNET, {"company": "Microsoft Corporation", "product": "Microsoft .NET"},
             ["dotnet.exe", str(apps / "Payroll.Client.dll"), "--employee", "4711"], None),
        # hosts whose application cannot be told
        51: (python, {}, ["python.exe", "-c", "import tkinter; tkinter.Tk().mainloop()"], None),
        52: (node, {}, ["node.exe", "index.js"], str(home / "scratch")),
        53: (r"C:\Program Files\Java\jre\bin\java.exe", {}, ["java.exe", "@" + str(apps / "args.txt")], None),
    }


DLL_INFO = {"Payroll.Client.dll": {"company": "Northwind Traders", "product": "Payroll Client",
                                   "product_version": "3.4.1"}}


def exe_info(path: str) -> dict:
    return DLL_INFO.get(hosts.file_name(path), {})


def identify(procs: dict, targets: levels.Targets) -> dict:
    """{pid: (identity, level)} as session._identify computes them."""
    out = {}
    for pid, (image, info, argv, cwd) in procs.items():
        ident = levels.desktop_identity(image, dict(info, file=hosts.file_name(image)))
        ident = hosts.resolve(ident, {"argv": argv, "cwd": cwd}, exe_info, image)
        out[pid] = (ident, targets.level(ident, image))
    return out


W = "desktop"


def desk(pid, window, title, action, control, aid):
    return {"pid": pid, "action": action, "surface": "desktop", "process": None,
            "window": {"selector": f"uia:AutomationId={window};ControlType=Window", "title": title},
            "target": {"selector": f"uia:AutomationId={aid};ControlType={control}"}}


# (tag, pid, step) in the order they happen: the tag is the application's word in its labels.
STEPS = [
    ("jarbilling", 41, desk(41, "BillingFrame", "Billing Client", "click", "Button", "jarbillingOpenInvoice")),
    ("ledger", 42, desk(42, "LedgerFrame", "Billing", "type", "Edit", "ledgerAmount")),
    ("uibilling", 44, desk(44, "BillingWindow", "Billing", "click", "Button", "uibillingSend")),
    ("claims", 43, desk(43, "ClaimsFrame", "Claims Desk", "click", "Button", "claimsApprove")),
    ("jarbilling", 41, desk(41, "BillingFrame", "Billing Client", "type", "Edit", "jarbillingCustomer")),
    ("payroll", 46, desk(46, "PayrollMain", "Payroll Client", "click", "Button", "payrollRun")),
    ("stock", 45, desk(45, "StockWindow", "Stock Viewer", "type", "Edit", "stockSymbol")),
    ("ledger", 42, desk(42, "LedgerFrame", "Billing", "click", "Button", "ledgerPost")),
    ("uibilling", 44, desk(44, "BillingWindow", "Billing", "type", "Edit", "uibillingAmount")),
    ("claims", 43, desk(43, "ClaimsFrame", "Claims Desk", "type", "Edit", "claimsNumber")),
    ("payroll", 46, desk(46, "PayrollMain", "Payroll Client", "type", "Edit", "payrollHours")),
    ("stock", 45, desk(45, "StockWindow", "Stock Viewer", "click", "Button", "stockRefresh")),
]
PRESENCE_PIDS = (51, 52, 53)


def ts(i):
    return f"2026-11-20T16:{i // 60:02d}:{i % 60:02d}.000Z"


def event_id(i):
    return f"019aa1c0-{i:04x}-7000-8000-{i:012x}"


def write_jsonl(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as out:
        for row in rows:
            out.write(json.dumps(row, ensure_ascii=False) + "\n")


def write(folder: Path, home: Path) -> dict:
    """The session folder; returns {pid: (identity, level)}."""
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    targets = levels.Targets.from_list(TARGETS)
    procs = app_files(home)
    found = identify(procs, targets)
    end = ts(len(STEPS) * 5 + 10)
    meta = {"format": timeline.FORMAT, "id": SESSION_ID, "name": "Host programs", "state": "stopped",
            "started": ts(0), "ended": end, "recorder": "TwinLabRecorder 0.2.0",
            "twinlab_commit": "430dfe538965b46ccbe824f8f2b4f415c5576483", "portal_session_id": 1,
            "targets": targets.as_list(), "apps": {}, "notes": []}
    (folder / "session.json").write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8", newline="\n")
    journal = []
    for pid, (ident, level) in found.items():
        if level in (levels.FULL, levels.STEPS):
            facts = session.app_facts(ident, level, targets.target_of(ident))
            journal.append({"ts": ts(1), "kind": "app", "pid": pid, **{k: v for k, v in facts.items() if v is not None}})
        else:
            journal.append({"ts": ts(2), "kind": "presence", "exe": ident["exe"], "reason": ident.get("reason")})
    write_jsonl(folder / "journal.jsonl", journal)
    steps, capture = [], [{"type": "open", "channel": W, "at": ts(0), "pid": 4242}]
    for i, (_tag, pid, step) in enumerate(STEPS, 1):
        at = ts(i * 5)
        row = {"seq": i, "ts": at, **dict(step, process=found[pid][0]["exe"]), "event_id": event_id(i),
               "channel": W, "captured_at": at}
        steps.append(row)
        capture += [{"type": "event", "event_id": event_id(i), "channel": W, "captured_at": at, "recording": W,
                     "action": step["action"], "seq": i, "ts": at},
                    {"type": "step", "event_id": event_id(i), "recording": W, "step": row}]
    capture.append({"type": "close", "at": end, "clean": True})
    write_jsonl(folder / "capture" / "desktop.jsonl", capture)
    write_jsonl(folder / "desktop" / "steps.jsonl", steps)
    return found


def tags() -> dict:
    return {event_id(i): tag for i, (tag, _pid, _step) in enumerate(STEPS, 1)}


def build(folder: Path) -> dict:
    return json.loads(json.dumps(timeline.build(folder)))


def main():
    out = Path(sys.argv[1]).resolve()
    found = write(out / "session", out / "home" / "ana")
    box = build(out / "session")
    for pid, (ident, level) in found.items():
        print(pid, level, ident["key"], ident.get("reason") or "")
    print(f"{len(box['apps'])} applications, {sum(len(f) for f in box['flows'].values())} steps")


if __name__ == "__main__":
    main()
