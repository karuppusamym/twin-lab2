"""TwinLabRecorder: record a session across applications on this PC; tell the portal about it.

    TwinLabRecorder                      the window (pair, sessions, start, stop, send)
    TwinLabRecorder pair --portal URL --code CODE --name "Workstation 7"
    TwinLabRecorder sessions             sessions the portal offers this machine
    TwinLabRecorder record --session 3   record an offered session (Ctrl+C or Stop ends it)
    TwinLabRecorder record --exe CaseDesk.exe --origin https://billing.example.test
    TwinLabRecorder record --tn3270 mainframe.example.test:23 --keep-browser-profile ...
                                         (checks first, preflight; a failed check refuses to start
                                         unless --override-preflight; the checks go with the references)
    TwinLabRecorder browser              open the kept browser profile (install UiPath's extension)
    TwinLabRecorder propose <project>    targets proposed from a UiPath project's XAML
    TwinLabRecorder show <session-id>    the references that would be sent
    TwinLabRecorder send <session-id>    send them (asks first; --yes to skip the question)
    TwinLabRecorder upload <session-id>  queue the recorded session for upload and send the queue
    TwinLabRecorder uploads              send what is queued (resuming), and show the queue
    TwinLabRecorder recover              finalise sessions an earlier run left unfinished

Options for every command: `--portable-data` (keep everything in `data\\` beside the program),
`--ca-bundle FILE` (a private certificate authority for the portal).

The recordings stay in the data folder. The references go to the portal only when the person
agrees; a recording itself only when the person uploads it (`upload`), through the offline queue
in `upload-queue\\` (twinlab2_recorder.upload_queue), which keeps its copy until the portal
confirms it. The collectors (`twinlab2_recorder.workstation`, `.desktop`, `.web`) are Twin
Lab's, copied at 90bdd91 (docs/UPSTREAM.md), and run in this process.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):  # run as a script: python recorder/twinlab2_recorder/main.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    if (Path(__file__).resolve().parents[2] / "contract").is_dir():  # a checkout: the shared package too
        sys.path.insert(1, str(Path(__file__).resolve().parents[2] / "contract"))

from twinlab2_recorder import settings as st  # noqa: E402
from twinlab2_recorder.client import Portal, PortalError, check_portal  # noqa: E402
from twinlab2_recorder.upload_queue import UploadQueue, describe as describe_upload  # noqa: E402
from twinlab2_contract import recorder_contract as contract  # noqa: E402
from twinlab2_contract.upstream import UPSTREAM_COMMIT  # noqa: E402


def _build_info() -> dict:
    path = st.program_dir() / "_build.json"
    if not path.is_file():
        path = Path(__file__).resolve().parent / "_build.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _portal(cfg: st.Settings, ca_bundle: str | None) -> Portal:
    if not cfg.values.get("portal") or not cfg.token():
        raise PortalError("this machine is not paired: run `TwinLabRecorder pair` first")
    return Portal(cfg.values["portal"], cfg.token(), ca_bundle)


# --- sessions ---------------------------------------------------------------------------------------


def session_config(targets: list[dict], deny_keys: list[str], name: str, portal_session_id=None,
                   automation_id=None, minutes: float = 240, keep_browser_profile: bool = False,
                   cdp: str | None = None, browser: str | None = None, pictures: bool = False,
                   robot: bool = True, exclude: dict | None = None) -> dict:  # fmt: skip
    """A session's configuration. `exclude`: this PC's exclude list
    (twinlab2_recorder.workstation.exclude.Policy.as_session()); run_session adds the PC's own list
    and the managed policy file to whatever it says, so a session never applies fewer."""
    from twinlab2_recorder.workstation.session import new_id

    return {"id": new_id(), "name": name, "targets": targets, "deny_keys": deny_keys,
            "portal_session_id": portal_session_id, "automation_id": automation_id,
            "minutes": minutes, "keep_browser_profile": keep_browser_profile, "cdp": cdp,
            "browser": browser, "pictures": pictures, "robot": robot,
            "exclude": exclude}  # fmt: skip


def session_exclude(cfg: st.Settings, conf: dict) -> dict:
    """The exclude list a session applies: what its configuration says, plus this PC's own list
    and the managed policy file (read again now: the office's entries always apply)."""
    from twinlab2_recorder.workstation import exclude

    return exclude.merge(exclude.Policy.from_session(conf.get("exclude")), cfg.exclude_policy()).as_session()


def _target_excluded(cfg: st.Settings, t: dict) -> bool:
    """Does this PC's exclude list name a target (its executable or web address)?"""
    pol = cfg.exclude_policy()
    return bool(pol.process(t.get("exe")) or (t.get("origin") and pol.url(str(t["origin"]) + (t.get("path") or "/"))))


def browser_profile(cfg: st.Settings) -> Path:
    """The kept profile of the recorder's browser: an extension installed in it once (UiPath's)
    is there in every session that keeps it."""
    return cfg.folder / "browser-profile"


def mainframe_addresses(conf: dict) -> list[str]:
    """Where each mainframe target's terminal connects (127.0.0.1:23270, 23271...)."""
    out, i = [], 0
    for t in conf.get("targets") or []:
        if t.get("tn3270"):
            port = t["listen"] if isinstance(t.get("listen"), int) else 23270 + i
            out.append(f"{t.get('name') or t['tn3270']}: point the terminal at 127.0.0.1:{port}")
            i += 1
    return out


class NotReady(Exception):
    """Preflight found checks that failed, and nobody chose to record anyway."""

    def __init__(self, block: dict):
        self.block = block
        super().__init__("not ready: " + "; ".join(failed_checks(block)))


def preflight_checks(cfg: st.Settings, conf: dict) -> dict:
    """Twin Lab's preflight (twinlab2_recorder.workstation.preflight) for this session, before anything is
    recorded: the collectors write the contract this recorder speaks, free disk where the
    recordings go, the browser (or the attached one) and each web target answer, UI Automation
    answers for desktop targets, each mainframe host answers and its terminal port is free.
    -> {"checks": [{"name", "ok", "detail"}], "ready": bool}"""
    from twinlab2_recorder.workstation.levels import Targets
    from twinlab2_recorder.workstation.preflight import preflight

    targets = Targets.from_list(conf["targets"], conf.get("deny_keys"), session_exclude(cfg, conf))
    got = preflight(targets, out=cfg.recordings, browser=conf.get("browser") or None,
                    cdp=conf.get("cdp") or None, portal_contract=contract.CONTRACT_VERSION)  # fmt: skip
    # The block goes to the portal: no local paths (they name the Windows user), within the
    # contract's lengths (name 80, detail 300).
    local = sorted({str(cfg.recordings.resolve()), str(cfg.folder.resolve()), str(Path.home())}, key=len, reverse=True)
    checks = []
    for c in got["checks"]:
        detail = str(c.get("detail") or "")
        for path in local:
            detail = detail.replace(path, "the recorder's data folder" if path != str(Path.home()) else "~")
        checks.append({"name": str(c.get("name") or "")[:80], "ok": bool(c.get("ok")), "detail": detail[:300]})
    return {"checks": checks, "ready": bool(got["ready"])}


def failed_checks(block: dict) -> list[str]:
    return [f"{c.get('name')}: {c.get('detail') or 'failed'}" for c in block.get("checks") or [] if not c.get("ok")]


def describe_checks(block: dict) -> str:
    lines = [f"  {'ok  ' if c.get('ok') else 'FAIL'}  {c.get('name')}: {c.get('detail') or ''}"
             for c in block.get("checks") or []]  # fmt: skip
    lines.append("Ready" if block.get("ready") else "NOT READY")
    return "\n".join(lines)


def run_session(cfg: st.Settings, conf: dict, script=None, parent_pid: int | None = None) -> Path:
    """Record one session in this process (the calling thread runs the desktop recorder).

    The session's preflight block (`conf["preflight"]`, run here when the caller has not run
    it) goes into session.json and the outbox. A session whose checks failed is not recorded
    (NotReady) unless `conf["override_preflight"]` says the person chose to record anyway; its
    references then carry the failed checks and `"ready": false`."""
    from twinlab2_recorder.workstation.levels import Targets
    from twinlab2_recorder.workstation.session import Session, recover_all

    # the Twin Lab commit the collectors were copied from (session.json, the references)
    os.environ.setdefault("TWINLAB_COMMIT", _build_info().get("twinlab_commit") or UPSTREAM_COMMIT)
    pre = conf.get("preflight")
    if not isinstance(pre, dict) or not isinstance(pre.get("checks"), list):
        pre = preflight_checks(cfg, conf)
    if not pre.get("ready") and conf.get("override_preflight") is not True:
        raise NotReady(pre)
    recover_all(cfg.recordings)
    targets = Targets.from_list(conf["targets"], conf.get("deny_keys"), session_exclude(cfg, conf))
    s = Session(cfg.recordings, targets, name=conf.get("name") or "", session_id=conf["id"],
                portal_session_id=conf.get("portal_session_id"),
                automation_id=conf.get("automation_id"),
                max_seconds=float(conf.get("minutes") or 240) * 60,
                recorder=f"{st.APP} {st.VERSION}",
                browser_profile=browser_profile(cfg) if conf.get("keep_browser_profile") else None,
                cdp=conf.get("cdp") or None, browser=conf.get("browser") or None,
                pictures=bool(conf.get("pictures")), robot=conf.get("robot", True) is not False,
                parent_pid=parent_pid, preflight=pre)  # fmt: skip
    return s.run(script=script)


def outbox(cfg: st.Settings, session_id: str) -> dict:
    path = cfg.recordings / session_id / "portal-outbox.json"
    if not path.is_file():
        raise SystemExit(f"no references for session {session_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def contract_references(box: dict) -> dict:
    """The outbox as recorder contract references: the contract version and this recorder
    stamped on it, then checked against the schema here, before anything leaves the PC.

    The outbox (twinlab2_recorder.workstation.timeline) declares its contract version (1.3.0 now;
    Twin Lab's declares 1.1.0 since 4385949). An
    outbox without a version (an older Twin Lab) is stamped with the version its shape is: 1.1.0
    when it reports the recording's `completeness`, else 1.0.0. A 1.1 outbox in which no step has
    an event id is a session recorded before event ids and is sent as 1.0.0 (see below). An
    outbox that declares a version must be of the major this recorder speaks."""
    declared = box.get("contract_version")
    if declared is not None and (contract.parse(declared) or (None,))[0] != contract.parse(contract.CONTRACT_VERSION)[0]:
        raise PortalError(f"these references are for recorder contract {declared}; this recorder speaks "
                          f"{contract.CONTRACT_VERSION}: update TwinLabRecorder")  # fmt: skip
    doc = dict(box, contract_version=declared or ("1.1.0" if "completeness" in box else "1.0.0"))
    steps = [s for flow in (doc.get("flows") or {}).values() for s in flow]
    if steps and contract.has_event_ids(doc["contract_version"]) and not any(s.get("event_id") for s in steps):
        # Twin Lab since 4385949 writes 1.1.0 on every outbox, also when it rebuilds a session
        # recorded before event ids: no step then has one, and its completeness was computed
        # without a capture journal. That is a contract 1.0 recording; send it as one (the portal
        # derives stable event ids at ingest, and shows completeness as unknown). A recording in
        # which only some steps carry ids still fails the 1.1 check below.
        doc["contract_version"] = "1.0.0"
        doc.pop("completeness", None)
        doc.pop("preflight", None)
        doc.pop("screens", None)  # 1.3 only: its states name events by id
    recorder = {"name": st.APP, "version": st.VERSION}
    commit = (box.get("session") or {}).get("twinlab_commit") or _build_info().get("twinlab_commit")
    if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{7,40}", commit):
        recorder["twinlab_commit"] = commit
    doc["recorder"] = recorder
    found = contract.errors(doc, "references")
    if found:
        raise PortalError(f"these references are outside recorder contract {contract.CONTRACT_VERSION} "
                          f"({'; '.join(found[:3])}): the recorder's Twin Lab collectors do not match "
                          "the contract; rebuild it from the pinned commit")  # fmt: skip
    return doc


def send(cfg: st.Settings, session_id: str, ca_bundle: str | None = None) -> dict:
    box = contract_references(outbox(cfg, session_id))
    result = _portal(cfg, ca_bundle).send(box)
    (cfg.recordings / session_id / "sent.json").write_text(json.dumps(
        {"sent_at": datetime.now(timezone.utc).isoformat(), "portal": cfg.values.get("portal"),
         "recording_id": result.get("recording_id"), "url": result.get("url")}, indent=1),
        encoding="utf-8")  # fmt: skip
    return result


def _split_by_path(steps: list[dict], origin: str, targets: list[dict]) -> list[tuple[dict, int]]:
    """The steps of one origin per target on it, by the portal's rule: a step goes to the target
    whose path prefix is the longest one its screen starts with (whole segments), else to the
    target without a path; with several targets without a path their steps are not told apart
    here (None). -> [(target, steps or None)] in the targets' order."""
    from twinlab2_recorder.workstation.flow import prefix_segments

    counts: list = [0] * len(targets)
    bares = [i for i, t in enumerate(targets) if not prefix_segments(t.get("path"))]
    bare = bares[0] if len(bares) == 1 else None  # several without a path: not told apart here
    for s in steps:
        screen = str(s.get("screen") or "")
        path = screen[len(origin):] if screen.startswith(origin) else ""
        segs = tuple(x for x in path.split("/") if x)
        best, length = bare, -1
        for i, t in enumerate(targets):
            pre = prefix_segments(t.get("path"))
            if pre and len(pre) > length and segs[: len(pre)] == pre:
                best, length = i, len(pre)
        if best is not None:
            counts[best] += 1
    if len(bares) > 1:
        for i in bares:
            counts[i] = None
    return list(zip(targets, counts))


def describe(box: dict, notes: list[str] | None = None, targets: list[dict] | None = None) -> str:
    """The session as the person reads it before sending. `targets`: the session's targets with
    their names (session.json); several on one origin get one line each (gap 12 of the rebuild
    proof: a shared host was listed once, under the last target's name)."""
    lines = [f"Session {box['session'].get('name')} ({box['session']['id']})"]
    targets = targets if targets is not None else (box.get("session") or {}).get("targets") or []
    targets = [_web_target(t) for t in targets if isinstance(t, dict)]
    for a in box["apps"]:
        flow = box["flows"].get(a["key"], [])
        origin = a.get("origin") or ""
        mine = [t for t in targets if origin and str(t.get("origin") or "").rstrip("/").lower() == origin.lower()]
        if a.get("surface") != "web" or len(mine) < 2:
            lines.append(f"  {a['level']:6} {a.get('display') or a['key']}  [{a['key']}]  {len(flow)} steps")
            continue
        lines.append(f"  {a['level']:6} {len(mine)} applications on one host  [{a['key']}]  {len(flow)} steps")
        for t, n in _split_by_path(flow, origin, mine):
            name = t.get("name") or (f"app {t['app_id']}" if t.get("app_id") is not None else "?")
            where = t.get("path") or "(the rest of the host)"
            if n is None:
                lines.append(f"           {name}  (no path: the portal tells its steps apart by its own address)")
            else:
                lines.append(f"           {name}  {where}  {n} steps")
    presence = sum(r["steps"] for r in box["timeline"] if r["app"] == "presence")
    lines.append(f"  other applications: used {presence} time(s), not named")
    local = (box.get("completeness") or {}).get("local_exclusions") or {}
    if local.get("count"):
        lines.append(f"  excluded on this PC: {local['count']} application(s), nothing recorded"
                     + (f" ({', '.join(local.get('names') or [])})" if local.get("names") else ""))  # fmt: skip
    rb = box.get("robot")
    if rb:
        lines.append(f"  robot log: {rb['activities']} activities; {rb['covered']} of {rb['ui_activities']} "
                     f"UI activities left a recorded step ({rb['gaps']} not seen: robot\\coverage.json)")  # fmt: skip
    pre = box.get("preflight")
    if isinstance(pre, dict) and not pre.get("ready"):
        lines.append("  RECORDING STARTED WITH FAILED CHECKS (the person chose to record anyway):")
        lines += [f"    {f}" for f in failed_checks(pre)]
    if not box["apps"]:
        lines.append("  NO TARGET APPLICATION WAS RECORDED: see the notes below")
    for note in notes or []:
        lines.append(f"  note: {note}")
    return "\n".join(lines)


def _web_target(t: dict) -> dict:
    """A target with its origin and path apart (an origin given with its path, a web key)."""
    row = dict(t)
    if row.get("origin") or str(row.get("key") or "").startswith("web:"):
        from twinlab2_recorder.workstation.levels import Target

        try:
            target = Target.from_dict(row)
            row["origin"], row["path"] = target.origin, target.path or row.get("path")
        except (ValueError, TypeError, AttributeError):
            pass
    return row


def session_targets(cfg: st.Settings, session_id: str) -> list[dict] | None:
    """The session's targets with their names, as session.json keeps them (None if unreadable)."""
    try:
        meta = json.loads((cfg.recordings / session_id / "session.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return [_web_target(t) for t in meta.get("targets") or [] if isinstance(t, dict)]


def session_notes(cfg: st.Settings, session_id: str) -> list[str]:
    try:
        meta = json.loads((cfg.recordings / session_id / "session.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    notes = list(meta.get("notes") or [])
    for part in sorted((cfg.recordings / session_id).glob("*/*.json")):
        if part.name in ("desktop.json", "mainframe.json"):
            try:
                notes += [f"{part.parent.name}: {n}" for n in json.loads(part.read_text(encoding="utf-8")).get("notes") or []]
            except (OSError, ValueError):
                pass
    return notes


def sessions_list(cfg: st.Settings) -> list[dict]:
    rows = []
    for folder in sorted(cfg.recordings.iterdir(), reverse=True) if cfg.recordings.is_dir() else []:
        meta_path = folder / "session.json"
        if not meta_path.is_file():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        rows.append({"id": folder.name, "name": meta.get("name"), "state": meta.get("state"),
                     "sent": (folder / "sent.json").is_file()})  # fmt: skip
    return rows


# --- the window ----------------------------------------------------------------------------------


def _hide_own_console() -> None:
    """The program is a console program (so its commands print); started by a double click,
    its console window is its own and is hidden behind the recorder's window."""
    if sys.platform != "win32":
        return
    import ctypes

    kernel32 = ctypes.windll.kernel32
    procs = (ctypes.c_uint * 4)()
    if kernel32.GetConsoleProcessList(procs, 4) == 1:
        # Detached, the console (or the Windows Terminal tab around it) closes, and closing it
        # can no longer end the recorder.
        window = kernel32.GetConsoleWindow()
        kernel32.FreeConsole()
        if window:
            ctypes.windll.user32.ShowWindow(window, 0)


def gui(cfg: st.Settings, ca_bundle: str | None) -> None:
    """The window: twinlab2_recorder.app (what it does) in WebView2 (twinlab2_recorder.webwindow)."""
    from twinlab2_recorder import webwindow

    _hide_own_console()
    webwindow.run(cfg, ca_bundle)


# --- command line ---------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="TwinLabRecorder", description=__doc__.split("\n\n")[0])
    parser.add_argument("--portable-data", action="store_true")
    parser.add_argument("--ca-bundle")
    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("pair")
    p.add_argument("--portal", required=True)
    p.add_argument("--code", required=True)
    p.add_argument("--name", required=True)
    sub.add_parser("sessions")
    r = sub.add_parser("record")
    r.add_argument("--session", type=int, help="an offered session's number")
    r.add_argument("--exe", action="append", default=[])
    r.add_argument("--origin", action="append", default=[])
    r.add_argument("--steps-exe", action="append", default=[])
    r.add_argument("--tn3270", action="append", default=[], help="a mainframe host:port")
    r.add_argument("--tn3270-listen", type=int, help="the port the terminal connects to (23270)")
    r.add_argument("--keep-browser-profile", action="store_true",
                   help="use the kept browser profile (UiPath's extension installed once)")
    r.add_argument("--cdp", help="attach to a running browser at this DevTools address")
    r.add_argument("--browser", choices=["edge", "chrome"], help="the browser the session starts")
    r.add_argument("--pictures", action="store_true", help="pictures of desktop windows (memory)")
    r.add_argument("--no-robot", action="store_true",
                   help="ignore values set without the keyboard focus (a person, not a robot)")
    r.add_argument("--name", default="")
    r.add_argument("--minutes", type=float, default=240)
    r.add_argument("--send", action="store_true", help="send the references at the end without asking")
    r.add_argument("--override-preflight", action="store_true",
                   help="record even when checks fail (the references say which failed)")
    rs = sub.add_parser("run-session")
    rs.add_argument("--config", required=True)
    rs.add_argument("--parent", type=int)
    sh = sub.add_parser("show")
    sh.add_argument("session_id")
    sd = sub.add_parser("send")
    sd.add_argument("session_id")
    sd.add_argument("--yes", action="store_true")
    up = sub.add_parser("upload", help="queue a recorded session for upload and send what is queued")
    up.add_argument("session_id")
    uq = sub.add_parser("uploads", help="send what is queued and show the queue")
    uq.add_argument("--now", action="store_true", help="also try jobs that are waiting to retry")
    sub.add_parser("recover")
    sub.add_parser("browser", help="open the kept browser profile to install an extension")
    pr = sub.add_parser("propose", help="targets proposed from a UiPath project's XAML")
    pr.add_argument("project")
    sub.add_parser("gui")
    sub.add_parser("version")
    args = parser.parse_args(argv)
    cfg = st.Settings(st.data_dir(args.portable_data))
    cfg.portable = args.portable_data

    try:
        if args.command in (None, "gui"):
            gui(cfg, args.ca_bundle)
        elif args.command == "version":
            print(st.APP, st.VERSION, "contract", contract.CONTRACT_VERSION, json.dumps(_build_info()))
        elif args.command == "pair":
            portal = check_portal(args.portal)
            doc = Portal(portal, ca_bundle=args.ca_bundle).pair(args.code, args.name)
            cfg.paired(portal, doc["machine_id"], doc["name"], doc["token"])
            print(f"paired as {doc['name']} (machine {doc['machine_id']}) with {portal}")
        elif args.command == "sessions":
            doc = _portal(cfg, args.ca_bundle).sessions()
            for s in doc["sessions"]:
                names = ", ".join(t.get("name") or t.get("exe") or t.get("origin") for t in s["targets"])
                print(f"{s['id']:>4}  {s['name']}  -  {names}")
            if not doc["sessions"]:
                print("no session is offered to this machine")
        elif args.command == "record":
            deny: list[str] = []
            targets = [{"exe": e} for e in args.exe] + [{"origin": o} for o in args.origin]
            targets += [{"exe": e, "level": "steps"} for e in args.steps_exe]
            for i, m in enumerate(args.tn3270):
                t = {"tn3270": m}
                if args.tn3270_listen is not None:
                    t["listen"] = args.tn3270_listen + i
                targets.append(t)
            portal_id = automation_id = None
            name = args.name
            if args.session is not None:
                doc = _portal(cfg, args.ca_bundle).sessions()
                offered = next((s for s in doc["sessions"] if s["id"] == args.session), None)
                if offered is None:
                    raise SystemExit(f"session {args.session} is not offered to this machine")
                targets += offered["targets"]
                deny, portal_id, automation_id = doc["deny_keys"], offered["id"], offered["automation_id"]
                name = name or offered["name"]
            if not targets:
                raise SystemExit("name a session (--session) or targets (--exe, --origin)")
            conf = session_config(targets, deny, name or "session", portal_id, automation_id, args.minutes,
                                  args.keep_browser_profile, args.cdp, args.browser, args.pictures,
                                  not args.no_robot, cfg.exclude_policy().as_session())  # fmt: skip
            excluded = [t for t in targets if _target_excluded(cfg, t)]
            for t in excluded:
                print(f"note: {t.get('name') or t.get('exe') or t.get('origin')} is on this PC's exclude "
                      "list: it will not be recorded", file=sys.stderr)  # fmt: skip
            print("checking that each channel can start...")
            conf["preflight"] = preflight_checks(cfg, conf)
            print(describe_checks(conf["preflight"]))
            if not conf["preflight"]["ready"]:
                if not args.override_preflight:
                    print("not ready: fix the checks above, or record anyway with --override-preflight "
                          "(the recording then says it started with failed checks)", file=sys.stderr)  # fmt: skip
                    return 2
                conf["override_preflight"] = True
                print("warning: recording anyway (--override-preflight); the references say which "
                      "checks failed", file=sys.stderr)  # fmt: skip
            print(f"recording session {conf['id']}: Ctrl+C, or a file 'stop' in "
                  f"{cfg.recordings / conf['id']}, ends it")  # fmt: skip
            for line in mainframe_addresses(conf):
                print(line)
            try:
                folder = run_session(cfg, conf)
            except KeyboardInterrupt:
                folder = cfg.recordings / conf["id"]
            box = outbox(cfg, folder.name)
            print(describe(box, session_notes(cfg, folder.name), session_targets(cfg, folder.name)))
            if args.send:
                print(json.dumps(send(cfg, folder.name, args.ca_bundle)))
            else:
                print(f"kept on this PC. Review with `show {folder.name}`, send with `send {folder.name}`")
        elif args.command == "run-session":
            conf = json.loads(Path(args.config).read_text(encoding="utf-8"))
            try:
                run_session(cfg, conf, parent_pid=args.parent)
            except NotReady as exc:
                print(describe_checks(exc.block), file=sys.stderr)
                print(f"error: {exc}", file=sys.stderr)
                return 2
        elif args.command == "show":
            box = outbox(cfg, args.session_id)
            print(describe(box, session_notes(cfg, args.session_id), session_targets(cfg, args.session_id)))
            print(json.dumps(box, indent=1))
        elif args.command == "send":
            box = outbox(cfg, args.session_id)
            print(describe(box, targets=session_targets(cfg, args.session_id)))
            if not args.yes and input("Send these references to the portal? [y/N] ").strip().lower() != "y":
                print("not sent")
                return 1
            print(json.dumps(send(cfg, args.session_id, args.ca_bundle)))
        elif args.command in ("upload", "uploads"):
            queue = UploadQueue(cfg.folder)
            only = None
            if args.command == "upload":
                folder = cfg.recordings / args.session_id
                if not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}", args.session_id) or not folder.is_dir():
                    raise SystemExit(f"no recorded session {args.session_id}")
                only = queue.add_session(folder, args.session_id)["id"]
            jobs = queue.run(_portal(cfg, args.ca_bundle), only=only, force=args.command == "upload" or args.now)
            for job in jobs:
                print(describe_upload(job))
            if not jobs:
                print("nothing is queued for upload")
        elif args.command == "browser":
            from twinlab2_recorder.workstation.session import open_profile

            open_profile(browser_profile(cfg), url="https://microsoftedge.microsoft.com/addons/search/uipath")
            print(f"opened the kept profile {browser_profile(cfg)}: install UiPath's extension, close "
                  "the browser, then record with --keep-browser-profile")  # fmt: skip
        elif args.command == "propose":
            from twinlab2_recorder.workstation.propose import propose

            got = propose(Path(args.project))
            for t in got["targets"]:
                print("  " + (t.get("exe") or t.get("origin") or "tn3270://" + t.get("tn3270", "")))
            if got["browser"]:
                print(f"browser: {got['browser']}")
            for note in got["notes"]:
                print(f"note: {note}")
            args_line = " ".join(("--exe " + t["exe"]) if t.get("exe") else ("--origin " + t["origin"])
                                 if t.get("origin") else ("--tn3270 " + t["tn3270"]) for t in got["targets"])  # fmt: skip
            print(f"record with: record {args_line}" + (f" --browser {got['browser']}" if got["browser"] else ""))
        elif args.command == "recover":
            from twinlab2_recorder.workstation.session import recover_all

            done = recover_all(cfg.recordings)
            print("recovered: " + (", ".join(done) if done else "nothing"))
    except PortalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    # The pinned Twin Lab runs each collector in its own "spawn" process; in the frozen
    # TwinLabRecorder those children re-enter this executable and must stop here.
    import multiprocessing

    multiprocessing.freeze_support()
    sys.exit(main())
