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
import random
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

if __package__ in (None, ""):  # run as a script: python recorder/twinlab2_recorder/main.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    if (Path(__file__).resolve().parents[2] / "contract").is_dir():  # a checkout: the shared package too
        sys.path.insert(1, str(Path(__file__).resolve().parents[2] / "contract"))

from twinlab2_recorder import settings as st  # noqa: E402
from twinlab2_recorder import ui_model as ui  # noqa: E402
from twinlab2_recorder.uithread import (Dispatcher, Job, Jobs, clear_ui_thread, mark_ui_thread,  # noqa: E402
                                        wait_process)
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
                   robot: bool = True) -> dict:  # fmt: skip
    from twinlab2_recorder.workstation.session import new_id

    return {"id": new_id(), "name": name, "targets": targets, "deny_keys": deny_keys,
            "portal_session_id": portal_session_id, "automation_id": automation_id,
            "minutes": minutes, "keep_browser_profile": keep_browser_profile, "cdp": cdp,
            "browser": browser, "pictures": pictures, "robot": robot}  # fmt: skip


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

    targets = Targets.from_list(conf["targets"], conf.get("deny_keys"))
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
    targets = Targets.from_list(conf["targets"], conf.get("deny_keys"))
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


def describe(box: dict, notes: list[str] | None = None) -> str:
    lines = [f"Session {box['session'].get('name')} ({box['session']['id']})"]
    for a in box["apps"]:
        lines.append(f"  {a['level']:6} {a.get('display') or a['key']}  [{a['key']}]  "
                     f"{len(box['flows'].get(a['key'], []))} steps")  # fmt: skip
    presence = sum(r["steps"] for r in box["timeline"] if r["app"] == "presence")
    lines.append(f"  other applications: used {presence} time(s), not named")
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


STOP_WAIT_S = 120  # how long closing the window waits for a recording to finish


def gui(cfg: st.Settings, ca_bundle: str | None) -> None:
    import tkinter as tk

    _hide_own_console()
    root = tk.Tk()
    build_window(cfg, ca_bundle, root)
    root.mainloop()


def build_window(cfg: st.Settings, ca_bundle: str | None, root) -> SimpleNamespace:  # noqa: C901 - one window
    """The recorder's window in `root`. Nothing here waits: portal calls, sending, uploads and
    finishing a recording run as background jobs (twinlab2_recorder.uithread), whose results come
    back through the dispatcher drained on this thread. Returns the window's parts (tests)."""
    import tkinter as tk
    from tkinter import messagebox, simpledialog, ttk

    mark_ui_thread()
    dispatcher = Dispatcher()
    root.title("TwinLab Recorder")
    root.geometry("760x620")
    root.minsize(600, 460)
    state = {"proc": None, "session": None, "deny": [], "apps": [], "polling": False, "closing": False,
             "recording": None, "watcher": None, "details": False, "box": None, "stop_asked": None}  # fmt: skip
    offered_model = ui.OfferedList()

    top = ttk.Frame(root, padding=10)
    top.pack(fill="x")
    status = tk.StringVar()
    activity = tk.StringVar()
    pair_btn = ttk.Button(top, text="Pair with portal…")
    pair_btn.pack(side="right", anchor="n")
    heads = ttk.Frame(top)
    heads.pack(side="left", fill="x", expand=True)
    status_label = ttk.Label(heads, textvariable=status, font=("Segoe UI", 10, "bold"), wraplength=520,
                             justify="left")  # fmt: skip
    status_label.pack(anchor="w", fill="x")
    activity_label = ttk.Label(heads, textvariable=activity, wraplength=520, justify="left")
    activity_label.pack(anchor="w", fill="x")

    def rewrap(e):
        for label in (status_label, activity_label):
            label.configure(wraplength=max(200, e.width - 180))

    top.bind("<Configure>", rewrap)

    # the recording indicator: always visible while a session records
    indicator = tk.StringVar(value="Not recording.")
    rec_frame = ttk.Frame(root, padding=(10, 0))
    rec_frame.pack(fill="x")
    rec_label = tk.Label(rec_frame, textvariable=indicator, anchor="w", justify="left", font=("Segoe UI", 10))
    rec_label.pack(side="left", fill="x", expand=True)
    stop_btn = ttk.Button(rec_frame, text="Stop recording", state="disabled")
    stop_btn.pack(side="right")
    idle_fg = rec_label.cget("fg")

    body = ttk.Notebook(root)
    body.pack(fill="both", expand=True, padx=10, pady=(6, 0))
    tabs = {name: ttk.Frame(body, padding=8) for name in ("offered", "own", "done", "uploads")}
    body.add(tabs["offered"], text="Offered sessions")
    body.add(tabs["own"], text="My own session")
    body.add(tabs["done"], text="Recordings on this PC")
    body.add(tabs["uploads"], text="Uploads")

    offered_list = tk.Listbox(tabs["offered"], height=8, exportselection=False)
    offered_list.pack(fill="both", expand=True)
    ttk.Label(tabs["offered"], text="Recording starts only when you press Start. The session's target "
              "applications are recorded in full; other applications only as having been used.",
              wraplength=640).pack(fill="x", pady=4)  # fmt: skip
    start_offered = ttk.Button(tabs["offered"], text="Start selected session")
    start_offered.pack(anchor="w")

    tab_own = tabs["own"]
    name_var = tk.StringVar(value="My session")
    ttk.Label(tab_own, text="Session name").pack(anchor="w")
    ttk.Entry(tab_own, textvariable=name_var).pack(fill="x")
    ttk.Label(tab_own, text="Target applications (from the portal's catalog)").pack(anchor="w", pady=(8, 0))
    own_frame = ttk.Frame(tab_own)
    own_frame.pack(fill="both", expand=True)
    own_vars: list[list] = []  # [BooleanVar, app, Checkbutton]
    ttk.Label(tab_own, text="Or one per line: an executable (CaseDesk.exe), a web address "
              "(https://...), a mainframe host (tn3270://host:port):").pack(anchor="w")  # fmt: skip
    extra = tk.Text(tab_own, height=3)
    extra.pack(fill="x")
    own_buttons = ttk.Frame(tab_own)
    own_buttons.pack(fill="x", pady=4)
    start_own = ttk.Button(own_buttons, text="Start my session")
    start_own.pack(side="left")
    propose_btn = ttk.Button(own_buttons, text="Targets from a UiPath project…")
    propose_btn.pack(side="left", padx=8)

    opts = ttk.LabelFrame(root, text="Browser", padding=6)
    opts.pack(fill="x", padx=10, pady=(6, 0))
    keep_var = tk.BooleanVar(value=bool(cfg.values.get("keep_browser_profile")))
    ttk.Checkbutton(opts, text="Keep the recorder's browser profile (needed for UiPath's browser "
                    "extension)", variable=keep_var).pack(anchor="w")  # fmt: skip
    cdp_row = ttk.Frame(opts)
    cdp_row.pack(fill="x")
    ttk.Label(cdp_row, text="Or attach to a running browser at (DevTools address):").pack(side="left")
    cdp_var = tk.StringVar(value=cfg.values.get("cdp") or "")
    ttk.Entry(cdp_row, textvariable=cdp_var, width=28).pack(side="left", padx=4)
    profile_btn = ttk.Button(cdp_row, text="Open the kept profile…")
    profile_btn.pack(side="right")
    more = ttk.Frame(opts)
    more.pack(fill="x")
    ttk.Label(more, text="Browser:").pack(side="left")
    browser_var = tk.StringVar(value=cfg.values.get("browser") or "edge")
    ttk.Combobox(more, textvariable=browser_var, values=["edge", "chrome"], width=8,
                 state="readonly").pack(side="left", padx=4)  # fmt: skip
    pictures_var = tk.BooleanVar(value=bool(cfg.values.get("pictures")))
    ttk.Checkbutton(more, text="Pictures of desktop windows (uses much memory on long runs)",
                    variable=pictures_var).pack(side="left", padx=8)  # fmt: skip

    tab_done = tabs["done"]
    done_list = tk.Listbox(tab_done, height=6, exportselection=False)
    done_list.pack(fill="both", expand=True)
    review = tk.Text(tab_done, height=12, wrap="word")
    review.pack(fill="both", expand=True, pady=4)
    row = ttk.Frame(tab_done)
    row.pack(fill="x")
    send_btn = ttk.Button(row, text="Send references to the portal")
    send_btn.pack(side="left")
    details_btn = ttk.Button(row, text="Details (JSON)")
    details_btn.pack(side="left", padx=6)
    upload_btn = ttk.Button(row, text="Upload the recording…")
    upload_btn.pack(side="left")
    ttk.Label(tab_done, text="Sending gives the portal only the references summarised above; the "
              "recording stays on this PC unless you upload it.", wraplength=640).pack(fill="x")  # fmt: skip

    tab_up = tabs["uploads"]
    uploads = ttk.Treeview(tab_up, columns=("session", "state", "progress", "detail"), show="headings", height=8)
    for col, text, width in (("session", "Session", 210), ("state", "State", 80), ("progress", "Progress", 70),
                             ("detail", "Detail", 320)):  # fmt: skip
        uploads.heading(col, text=text)
        uploads.column(col, width=width, stretch=col == "detail")
    uploads.pack(fill="both", expand=True)
    up_row = ttk.Frame(tab_up)
    up_row.pack(fill="x", pady=4)
    retry_btn = ttk.Button(up_row, text="Retry selected")
    retry_btn.pack(side="left")
    upload_now_btn = ttk.Button(up_row, text="Try all now")
    upload_now_btn.pack(side="left", padx=6)
    upload_note = tk.StringVar(value="Uploads continue in the background and resume after a restart.")
    ttk.Label(up_row, textvariable=upload_note, wraplength=440).pack(side="left", padx=8)

    # -- jobs and their states ---------------------------------------------------------------------

    def show_job(job: Job):
        if job.kind == "poll" and job.state != "retrying":
            return  # polls show through the paired status line
        activity.set(job.describe())

    jobs = Jobs(dispatcher, show_job)

    def pump():
        dispatcher.drain()
        if not state.get("destroyed"):
            root.after(50, pump)

    root.after(50, pump)

    def refresh_status():
        if cfg.values.get("portal") and cfg.values.get("token"):
            status.set(f"Paired as {cfg.values.get('machine')} with {cfg.values.get('portal')}")
        else:
            status.set("Not paired: pair with the portal to receive sessions and send references.")

    def refresh_done():
        keep = None
        sel = done_list.curselection()
        if sel and sel[0] < len(state.get("done") or []):
            keep = state["done"][sel[0]]["id"]
        done_list.delete(0, "end")
        state["done"] = sessions_list(cfg)
        for i, s in enumerate(state["done"]):
            done_list.insert("end", f"{s['name']}  ·  {s['state']}  ·  {'sent' if s['sent'] else 'not sent'}")
            if s["id"] == keep:
                done_list.selection_set(i)

    def selected_done() -> dict | None:
        sel = done_list.curselection()
        return state["done"][sel[0]] if sel and sel[0] < len(state.get("done") or []) else None

    def render_review():
        review.configure(state="normal")
        review.delete("1.0", "end")
        box, s = state.get("box"), selected_done()
        if s is None:
            pass
        elif box is None:
            review.insert("end", state.get("box_error") or "")
        else:
            review.insert("end", ui.review_summary(box, session_notes(cfg, s["id"]), cfg.values.get("portal"),
                                                   cfg.values.get("machine")))  # fmt: skip
            if state["details"]:
                review.insert("end", "\n\nDetails: the references as sent (JSON)\n" + json.dumps(box, indent=1))
        details_btn.configure(text="Hide details" if state["details"] else "Details (JSON)")
        review.configure(state="disabled")

    def show_done(_event=None):
        s = selected_done()
        state["box"], state["box_error"] = None, None
        if s is not None:
            try:
                state["box"] = outbox(cfg, s["id"])
            except SystemExit as exc:
                state["box_error"] = str(exc)
        render_review()

    def on_details():
        state["details"] = not state["details"]
        render_review()

    # -- polling the portal ----------------------------------------------------------------------------

    def poll_loop():
        if state.get("destroyed"):
            return
        root.after(int((15 + random.uniform(0, 5)) * 1000), poll_loop)
        poll_portal()

    def poll_portal():
        if state["polling"] or not cfg.values.get("token") or state.get("destroyed"):
            return
        state["polling"] = True

        def work(_report):
            p = _portal(cfg, ca_bundle)
            return p.sessions(), p.apps()

        # The error arrives as a bound argument (the reason string), never as a closure over the
        # handler's exception name, which Python deletes when the handler ends.
        jobs.run("poll", work, label="the portal", on_done=lambda got: apply(got[0], got[1], None),
                 on_failed=lambda reason: apply(None, None, reason))  # fmt: skip

    def apply(offered, apps, error):
        state["polling"] = False
        if error:
            if cfg.values.get("portal"):
                status.set(f"Paired as {cfg.values.get('machine')}: {error}")
            return
        refresh_status()
        state["deny"] = offered["deny_keys"]
        offered_model.sync(offered_list, offered["sessions"])
        new = apps["apps"]
        how = ui.app_choices([a for _, a, _ in own_vars], new)
        if how == "relabel":
            for item, a in zip(own_vars, new):
                item[1] = a
                item[2].configure(text=a["name"])
        elif how == "rebuild":
            ticked = {a["app_id"] for var, a, _ in own_vars if var.get()}
            for child in own_frame.winfo_children():
                child.destroy()
            own_vars.clear()
            for a in new:
                var = tk.BooleanVar(value=a["app_id"] in ticked)
                box = ttk.Checkbutton(own_frame, text=a["name"], variable=var)
                box.pack(anchor="w")
                own_vars.append([var, a, box])

    # -- recording -----------------------------------------------------------------------------------

    def options() -> dict:
        cdp = cdp_var.get().strip()
        if cdp and not cdp.startswith(("http://127.0.0.1:", "http://localhost:")):
            raise ValueError("attach only to a browser on this PC, for example http://127.0.0.1:9222")
        cfg.values.update(keep_browser_profile=keep_var.get(), cdp=cdp, browser=browser_var.get(),
                          pictures=pictures_var.get())  # fmt: skip
        cfg.save()
        return {"keep_browser_profile": keep_var.get(), "cdp": cdp or None,
                "browser": browser_var.get(), "pictures": pictures_var.get()}  # fmt: skip

    def set_start_buttons(enabled: bool):
        for b in (start_offered, start_own):
            b.configure(state="normal" if enabled and not state["closing"] else "disabled")

    def start(conf):
        """Preflight first (in a worker thread: a check may wait a few seconds), then record."""
        if state["proc"] is not None or state.get("checking") or state["closing"]:
            return
        try:
            conf.update(options())
        except ValueError as exc:
            messagebox.showerror("TwinLab Recorder", str(exc))
            return
        state["checking"] = True
        set_start_buttons(False)
        indicator.set("Checking that each channel can start…")

        def work():
            try:
                block, error = preflight_checks(cfg, conf), None
            except Exception as exc:  # noqa: BLE001 - shown to the person
                block, error = None, f"{type(exc).__name__}: {exc}"
            dispatcher.post(checked, conf, block, error)

        threading.Thread(target=work, name="tl-preflight", daemon=True).start()

    def checked(conf, block, error):
        state["checking"] = False
        if state["closing"]:
            return
        if error:
            indicator.set("Not recording: the checks could not run.")
            set_start_buttons(True)
            messagebox.showerror("TwinLab Recorder", f"The checks before recording could not run: {error}")
            return
        conf["preflight"] = block
        if not block["ready"]:
            failed = "\n".join("• " + f for f in failed_checks(block))
            indicator.set("Not ready:\n" + failed)
            if not messagebox.askyesno("TwinLab Recorder — not ready",
                                       f"These checks failed:\n\n{failed}\n\nFix them and press Start "
                                       "again. Or record anyway? The recording will say it started with "
                                       "these checks failed.", icon="warning", default="no"):  # fmt: skip
                set_start_buttons(True)
                return
            conf["override_preflight"] = True
        launch(conf)

    def launch(conf):
        cfg_path = cfg.folder / "next-session.json"
        cfg_path.write_text(json.dumps(conf), encoding="utf-8")
        cmd = [sys.executable] + ([] if getattr(sys, "frozen", False) else [str(Path(__file__).resolve())])
        cmd += ["run-session", "--config", str(cfg_path), "--parent", str(os.getpid())]
        cmd += ["--portable-data"] if cfg.portable else []
        (cfg.folder / "logs").mkdir(exist_ok=True)
        state["log"] = cfg.folder / "logs" / f"{conf['id']}.log"
        log = state["log"].open("w", encoding="utf-8")
        state["proc"] = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))  # fmt: skip
        log.close()
        state["session"] = conf["id"]
        rec = ui.RecordingStatus(conf["name"], conf["targets"], time.monotonic())
        state["recording"] = rec
        state["extra_lines"] = (["Ready: every check passed."] if conf["preflight"]["ready"] else
                                ["Recording anyway, with failed checks."]) + mainframe_addresses(conf)  # fmt: skip
        state["watcher"] = ui.JournalWatcher(cfg.recordings / conf["id"] / "journal.jsonl", dispatcher.post,
                                             lambda rows: [rec.feed(r) for r in rows]).start()  # fmt: skip
        stop_btn.configure(state="normal")
        set_start_buttons(False)
        tick()
        root.after(1000, watch)

    def tick():
        rec = state["recording"]
        if rec is None:
            rec_label.configure(fg=idle_fg)
            return
        lines = rec.lines(time.monotonic()) + state.get("extra_lines", [])
        if state.get("stop_asked") is not None:
            lines.append(f"Finishing the recording… ({int(time.monotonic() - state['stop_asked'])} s"
                         + (f" of {STOP_WAIT_S} s before the window closes)" if state["closing"] else ")"))  # fmt: skip
        indicator.set("\n".join(lines))
        rec_label.configure(fg={"ok": "#b00020", "starting": "#b00020", "trouble": "#c25e00"}[rec.health()])
        root.after(1000, tick)

    def watch():
        proc = state["proc"]
        if proc is None:
            return
        if proc.poll() is None:
            root.after(1000, watch)
            return
        recorded(proc.returncode)

    def recorded(code):
        """The recording process ended (by itself, Stop, or the window closing)."""
        if state["proc"] is None:
            return
        state["proc"] = None
        if state["watcher"]:
            state["watcher"].stop()
        state["recording"], state["stop_asked"] = None, None
        rec_label.configure(fg=idle_fg)
        stop_btn.configure(state="disabled")
        if state["closing"]:
            return
        if code != 0:
            indicator.set(f"The recording ended with an error (see {state.get('log')}). What was "
                          "recorded until then is kept.")  # fmt: skip
            messagebox.showerror("TwinLab Recorder", f"The recording ended with an error. The log is "
                                 f"{state.get('log')}")  # fmt: skip
        else:
            indicator.set("Recording saved on this PC. Review it before sending.")
        set_start_buttons(True)
        refresh_done()
        body.select(tab_done)
        for i, s in enumerate(state.get("done") or []):
            if s["id"] == state["session"]:
                done_list.selection_clear(0, "end")
                done_list.selection_set(i)
                show_done()

    def on_stop():
        if state["session"] and state["proc"] is not None:
            (cfg.recordings / state["session"] / "stop").touch()
            if state["stop_asked"] is None:
                state["stop_asked"] = time.monotonic()
            if state["recording"]:
                state["recording"].stopping = True
            stop_btn.configure(state="disabled")

    def on_start_offered():
        s = offered_model.selected(offered_list)
        if s is None:
            messagebox.showinfo("TwinLab Recorder", "Choose a session first.")
            return
        start(session_config(s["targets"], state["deny"], s["name"], s["id"], s["automation_id"]))

    def on_start_own():
        targets = [dict(a["target"]) for var, a, _ in own_vars if var.get()]
        for line in extra.get("1.0", "end").splitlines():
            line = line.strip()
            if line.lower().startswith(("http://", "https://")):
                targets.append({"origin": line, "level": "full"})
            elif line.lower().endswith(".exe"):
                targets.append({"exe": line, "level": "full"})
            elif line.lower().startswith(("tn3270://", "tn3270s://")):
                targets.append({"tn3270": line})
        if not targets:
            messagebox.showinfo("TwinLab Recorder", "Choose at least one target application.")
            return
        start(session_config(targets, state["deny"], name_var.get().strip() or "My session"))

    # -- sending references -----------------------------------------------------------------------------

    def on_send():
        s = selected_done()
        if s is None or state.get("sending"):
            return
        if not messagebox.askyesno("TwinLab Recorder", f"Send the references summarised here to "
                                   f"{cfg.values.get('portal') or 'the portal'}? The recording itself stays "
                                   "on this PC."):  # fmt: skip
            return
        state["sending"] = True
        send_btn.configure(state="disabled")
        session_id = s["id"]

        def sent(result):
            state["sending"] = False
            send_btn.configure(state="normal")
            messagebox.showinfo("TwinLab Recorder", f"Sent. The portal mapped it as {result.get('recording_id')}.")
            refresh_done()

        def not_sent(reason):
            state["sending"] = False
            send_btn.configure(state="normal")
            messagebox.showerror("TwinLab Recorder", f"The references were not sent: {reason}")

        jobs.run("send", lambda _r: send(cfg, session_id, ca_bundle), state="sending",
                 label=f"references of {s['name']}", retries=3, on_done=sent, on_failed=not_sent)  # fmt: skip

    # -- pairing ------------------------------------------------------------------------------------

    def on_pair():
        if state.get("pairing"):
            return
        url = simpledialog.askstring("Pair", "Portal address", initialvalue=cfg.values.get("portal") or "https://")
        if not url:
            return
        try:
            url = check_portal(url)
        except PortalError as exc:
            messagebox.showerror("TwinLab Recorder", str(exc))
            return
        code = simpledialog.askstring("Pair", "Pairing code from the portal's Machines page")
        if not code:
            return
        name = simpledialog.askstring("Pair", "A name for this machine", initialvalue=os.environ.get("COMPUTERNAME", ""))
        if not name:
            return
        state["pairing"] = True
        pair_btn.configure(state="disabled")

        def paired(doc):
            state["pairing"] = False
            pair_btn.configure(state="normal")
            cfg.paired(url, doc["machine_id"], doc["name"], doc["token"])
            refresh_status()
            poll_portal()
            upload_worker.kick()

        def failed(reason):
            state["pairing"] = False
            pair_btn.configure(state="normal")
            messagebox.showerror("TwinLab Recorder", f"Not paired: {reason}")

        jobs.run("pair", lambda _r: Portal(url, ca_bundle=ca_bundle).pair(code, name), state="connecting",
                 label=f"pairing with {url}", retries=2, on_done=paired, on_failed=failed)  # fmt: skip

    # -- uploads --------------------------------------------------------------------------------------

    def show_uploads(found, active, error):
        now = time.time()
        rows = [ui.upload_row(j, now, active) for j in found]
        seen = set()
        for r in rows:
            values = (r["session"], r["state"], f"{r['percent']}%", r["detail"])
            seen.add(r["id"])
            if uploads.exists(r["id"]):
                uploads.item(r["id"], values=values)
            else:
                uploads.insert("", "end", iid=r["id"], values=values)
        for iid in uploads.get_children():
            if iid not in seen:
                uploads.delete(iid)
        busy = sum(r["state"] in ("sending", "queued", "retrying") for r in rows)
        upload_note.set((f"Not sending: {error}. " if error else "") +
                        (f"{busy} upload(s) waiting or in progress; they continue in the background and "
                         "resume after a restart." if busy else "Nothing waiting to upload."))  # fmt: skip

    upload_worker = ui.UploadWorker(lambda on_change: UploadQueue(cfg.folder, on_change=on_change),
                                    lambda: _portal(cfg, ca_bundle), dispatcher.post, show_uploads)  # fmt: skip

    def on_upload():
        s = selected_done()
        if s is None:
            return
        if not messagebox.askyesno("TwinLab Recorder", f"Upload the whole recording of {s['name']} (its "
                                   "folder, with what it captured) to "
                                   f"{cfg.values.get('portal') or 'the portal'}? It is queued and sent in "
                                   "the background."):  # fmt: skip
            return
        upload_worker.add(cfg.recordings / s["id"], s["id"])
        body.select(tab_up)

    def on_retry():
        for iid in uploads.selection():
            upload_worker.retry(iid)

    # -- other buttons ----------------------------------------------------------------------------------

    def on_profile():
        if state["proc"] is not None:
            messagebox.showinfo("TwinLab Recorder", "Stop the recording first.")
            return
        from twinlab2_recorder.workstation.session import open_profile

        try:
            open_profile(browser_profile(cfg), url="https://microsoftedge.microsoft.com/addons/search/uipath")
        except RuntimeError as exc:
            messagebox.showerror("TwinLab Recorder", str(exc))
            return
        keep_var.set(True)
        messagebox.showinfo("TwinLab Recorder", "The recorder's browser opened with its kept profile. "
                            "Install UiPath's extension there (or sign in to the applications), "
                            "then close that browser before you start recording.")  # fmt: skip

    def on_propose():
        from tkinter import filedialog

        folder = filedialog.askdirectory(title="The UiPath project folder (with Main.xaml)")
        if not folder:
            return
        from twinlab2_recorder.workstation.propose import propose

        try:
            got = propose(Path(folder))
        except FileNotFoundError as exc:
            messagebox.showerror("TwinLab Recorder", str(exc))
            return
        lines = []
        for t in got["targets"]:
            if t.get("exe"):
                lines.append(t["exe"])
            elif t.get("origin"):
                lines.append(t["origin"])
            elif t.get("tn3270"):
                lines.append("tn3270://" + t["tn3270"])
        extra.delete("1.0", "end")
        extra.insert("end", "\n".join(lines))
        if got["browser"]:
            browser_var.set(got["browser"])
        messagebox.showinfo("TwinLab Recorder", f"{len(lines)} target(s) from {got['files']} workflow file(s)."
                            + ("\n\n" + "\n".join(got["notes"]) if got["notes"] else "")
                            + "\n\nCheck them before you start.")  # fmt: skip

    # -- closing ------------------------------------------------------------------------------------

    def destroy():
        state["destroyed"] = True
        upload_worker.stop()
        jobs.cancelled.set()
        if state["watcher"]:
            state["watcher"].stop()
        clear_ui_thread()
        root.destroy()

    def on_close():
        if state["closing"]:
            activity.set("Still finishing the recording: the window closes when it is done "
                         f"(at most {STOP_WAIT_S} s).")  # fmt: skip
            return
        proc = state["proc"]
        if proc is None:
            destroy()
            return
        if not messagebox.askyesno("TwinLab Recorder", "A recording is running. Stop it and close? The "
                                   "window stays open until the recording is finished."):  # fmt: skip
            return
        state["closing"] = True
        on_stop()
        set_start_buttons(False)
        for b in (pair_btn, send_btn, upload_btn):
            b.configure(state="disabled")

        def finished(code):
            recorded(code if code is not None else -1)
            message = ui.close_message(code, STOP_WAIT_S)
            if message:
                messagebox.showwarning("TwinLab Recorder", message)
            destroy()

        jobs.run("stop", lambda _r: wait_process(proc, STOP_WAIT_S), state="stopping",
                 label="finishing the recording before the window closes", on_done=finished,
                 on_failed=lambda reason: finished(None))  # fmt: skip

    propose_btn.configure(command=on_propose)
    profile_btn.configure(command=on_profile)
    pair_btn.configure(command=on_pair)
    stop_btn.configure(command=on_stop)
    start_offered.configure(command=on_start_offered)
    start_own.configure(command=on_start_own)
    send_btn.configure(command=on_send)
    details_btn.configure(command=on_details)
    upload_btn.configure(command=on_upload)
    retry_btn.configure(command=on_retry)
    upload_now_btn.configure(command=lambda: upload_worker.kick(force=True))
    done_list.bind("<<ListboxSelect>>", show_done)
    refresh_status()
    refresh_done()
    upload_worker.start()
    root.after(200, poll_loop)
    root.protocol("WM_DELETE_WINDOW", on_close)
    return SimpleNamespace(**{k: v for k, v in locals().items() if not k.startswith("_")})


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
                                  not args.no_robot)  # fmt: skip
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
            print(describe(box, session_notes(cfg, folder.name)))
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
            print(describe(box, session_notes(cfg, args.session_id)))
            print(json.dumps(box, indent=1))
        elif args.command == "send":
            box = outbox(cfg, args.session_id)
            print(describe(box))
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
