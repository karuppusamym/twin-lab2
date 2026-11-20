"""TwinLabRecorder: record a session across applications on this PC; tell the portal about it.

    TwinLabRecorder                      the window (pair, sessions, start, stop, send)
    TwinLabRecorder pair --portal URL --code CODE --name "Workstation 7"
    TwinLabRecorder sessions             sessions the portal offers this machine
    TwinLabRecorder record --session 3   record an offered session (Ctrl+C or Stop ends it)
    TwinLabRecorder record --exe CaseDesk.exe --origin https://billing.example.test
    TwinLabRecorder record --tn3270 mainframe.example.test:23 --keep-browser-profile ...
    TwinLabRecorder browser              open the kept browser profile (install UiPath's extension)
    TwinLabRecorder propose <project>    targets proposed from a UiPath project's XAML
    TwinLabRecorder show <session-id>    the references that would be sent
    TwinLabRecorder send <session-id>    send them (asks first; --yes to skip the question)
    TwinLabRecorder recover              finalise sessions an earlier run left unfinished

Options for every command: `--portable-data` (keep everything in `data\\` beside the program),
`--ca-bundle FILE` (a private certificate authority for the portal).

The recordings stay in the data folder. Only the references go to the portal, and only when the
person agrees. The collectors are Twin Lab's (`labctl.workstation`), run in this process.
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

if __package__ in (None, ""):  # run as a script: python agent/main.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import settings as st  # noqa: E402
from agent.client import Portal, PortalError, check_portal  # noqa: E402
import recorder_contract as contract  # noqa: E402


def _labctl() -> None:
    """Make Twin Lab's collectors importable: bundled in a frozen build; else the pinned copy
    in vendor/ (twinlab.lock.json, `python -m agent.twinlab fetch`), checked against its digest.
    TWINLAB_PATH names another checkout for development only: it is not the pinned release."""
    if getattr(sys, "frozen", False) or "labctl" in sys.modules:
        return  # bundled at build time (from the pin, see _build.json), or already chosen
    from agent import twinlab  # never a labctl that pip put in site-packages: the pin comes first

    override = os.environ.get("TWINLAB_PATH")
    if override:
        if not (Path(override) / "lab" / "labctl" / "labctl").is_dir():
            raise SystemExit(f"TWINLAB_PATH={override} has no lab/labctl/labctl")
        print(f"note: Twin Lab's collectors from TWINLAB_PATH={override}, not the pinned commit",
              file=sys.stderr)  # fmt: skip
        sys.path.insert(0, str(Path(override) / "lab" / "labctl"))
        return
    try:
        twinlab.verify()
    except twinlab.PinError as exc:
        raise SystemExit(f"Twin Lab's collectors: {exc}") from exc
    sys.path.insert(0, str(twinlab.labctl_parent()))
    # The vendored folder is not a Git checkout: say which commit it is.
    os.environ.setdefault("TWINLAB_COMMIT", twinlab.lock()["commit"])


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
    from labctl.workstation.session import new_id

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


def run_session(cfg: st.Settings, conf: dict, script=None, parent_pid: int | None = None) -> Path:
    """Record one session in this process (the calling thread runs the desktop recorder)."""
    _labctl()
    from labctl.workstation.levels import Targets
    from labctl.workstation.session import Session, recover_all

    commit = _build_info().get("twinlab_commit")
    if commit:
        os.environ.setdefault("TWINLAB_COMMIT", commit)
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
                parent_pid=parent_pid)  # fmt: skip
    return s.run(script=script)


def outbox(cfg: st.Settings, session_id: str) -> dict:
    path = cfg.recordings / session_id / "portal-outbox.json"
    if not path.is_file():
        raise SystemExit(f"no references for session {session_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def contract_references(box: dict) -> dict:
    """The outbox as recorder contract references: the contract version and this recorder
    stamped on it, then checked against the schema here, before anything leaves the PC.

    Twin Lab's outbox (labctl.workstation.timeline) does not carry a contract version yet; the
    pinned commit's outbox is what contract 1.0.0 describes. When it does carry one, it must be
    the major this recorder speaks."""
    declared = box.get("contract_version")
    if declared is not None and (contract.parse(declared) or (None,))[0] != contract.parse(contract.CONTRACT_VERSION)[0]:
        raise PortalError(f"these references are for recorder contract {declared}; this recorder speaks "
                          f"{contract.CONTRACT_VERSION}: update TwinLabRecorder")  # fmt: skip
    doc = dict(box, contract_version=declared or contract.CONTRACT_VERSION)
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


def gui(cfg: st.Settings, ca_bundle: str | None) -> None:  # noqa: C901 - one window
    import tkinter as tk
    from tkinter import messagebox, simpledialog, ttk

    _hide_own_console()

    root = tk.Tk()
    root.title("TwinLab Recorder")
    root.geometry("720x560")
    root.minsize(560, 420)
    state = {"proc": None, "session": None, "offered": [], "apps": [], "deny": []}

    top = ttk.Frame(root, padding=10)
    top.pack(fill="x")
    status = tk.StringVar()
    pair_btn = ttk.Button(top, text="Pair with portal…")
    pair_btn.pack(side="right", anchor="n")
    status_label = ttk.Label(top, textvariable=status, font=("Segoe UI", 10, "bold"), wraplength=520,
                             justify="left")  # fmt: skip
    status_label.pack(side="left", fill="x", expand=True)
    top.bind("<Configure>", lambda e: status_label.configure(wraplength=max(200, e.width - 180)))

    body = ttk.Notebook(root)
    body.pack(fill="both", expand=True, padx=10)
    tab_offered, tab_own, tab_done = ttk.Frame(body, padding=8), ttk.Frame(body, padding=8), ttk.Frame(body, padding=8)
    body.add(tab_offered, text="Offered sessions")
    body.add(tab_own, text="My own session")
    body.add(tab_done, text="Recordings on this PC")

    offered_list = tk.Listbox(tab_offered, height=8)
    offered_list.pack(fill="both", expand=True)
    ttk.Label(tab_offered, text="Recording starts only when you press Start. The session's target "
              "applications are recorded in full; other applications only as having been used.",
              wraplength=640).pack(fill="x", pady=4)  # fmt: skip
    start_offered = ttk.Button(tab_offered, text="Start selected session")
    start_offered.pack(anchor="w")

    name_var = tk.StringVar(value="My session")
    ttk.Label(tab_own, text="Session name").pack(anchor="w")
    ttk.Entry(tab_own, textvariable=name_var).pack(fill="x")
    ttk.Label(tab_own, text="Target applications (from the portal's catalog)").pack(anchor="w", pady=(8, 0))
    own_frame = ttk.Frame(tab_own)
    own_frame.pack(fill="both", expand=True)
    own_vars: list[tuple[tk.BooleanVar, dict]] = []
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

    done_list = tk.Listbox(tab_done, height=8)
    done_list.pack(fill="both", expand=True)
    review = tk.Text(tab_done, height=10, wrap="word")
    review.pack(fill="both", expand=True, pady=4)
    row = ttk.Frame(tab_done)
    row.pack(fill="x")
    send_btn = ttk.Button(row, text="Send references to the portal")
    send_btn.pack(side="left")
    ttk.Label(row, text="Only what is shown above is sent; the recording stays on this PC.").pack(side="left", padx=8)

    bottom = ttk.Frame(root, padding=10)
    bottom.pack(fill="x")
    rec_status = tk.StringVar(value="Not recording.")
    ttk.Label(bottom, textvariable=rec_status).pack(side="left")
    stop_btn = ttk.Button(bottom, text="Stop recording", state="disabled")
    stop_btn.pack(side="right")

    def refresh_status():
        if cfg.values.get("portal") and cfg.values.get("token"):
            status.set(f"Paired as {cfg.values.get('machine')} with {cfg.values.get('portal')}")
        else:
            status.set("Not paired: pair with the portal to receive sessions and send references.")

    def refresh_done():
        done_list.delete(0, "end")
        state["done"] = sessions_list(cfg)
        for s in state["done"]:
            done_list.insert("end", f"{s['name']}  ·  {s['state']}  ·  {'sent' if s['sent'] else 'not sent'}")

    def show_done(_event=None):
        sel = done_list.curselection()
        review.delete("1.0", "end")
        if not sel:
            return
        s = state["done"][sel[0]]
        try:
            box = outbox(cfg, s["id"])
        except SystemExit as exc:
            review.insert("end", str(exc))
            return
        review.insert("end", describe(box, session_notes(cfg, s["id"])) + "\n\n" + json.dumps(box, indent=1))

    def poll_portal():
        def work():
            try:
                p = _portal(cfg, ca_bundle)
                offered, apps = p.sessions(), p.apps()
                root.after(0, lambda: apply(offered, apps, None))
            except PortalError as exc:
                root.after(0, lambda: apply(None, None, str(exc)))

        threading.Thread(target=work, daemon=True).start()
        root.after(int((15 + random.uniform(0, 5)) * 1000), poll_portal)

    def apply(offered, apps, error):
        if error:
            if cfg.values.get("portal"):
                status.set(f"Paired as {cfg.values.get('machine')}: {error}")
            return
        refresh_status()
        state["offered"], state["deny"] = offered["sessions"], offered["deny_keys"]
        offered_list.delete(0, "end")
        for s in state["offered"]:
            names = ", ".join(sorted({t.get("name") or t.get("exe") or t.get("origin") or "" for t in s["targets"]}))
            offered_list.insert("end", f"{s['name']}  ·  {names}")
        if [a["app_id"] for a in apps["apps"]] != [a["app_id"] for _, a in own_vars]:
            for child in own_frame.winfo_children():
                child.destroy()
            own_vars.clear()
            for a in apps["apps"]:
                var = tk.BooleanVar()
                ttk.Checkbutton(own_frame, text=a["name"], variable=var).pack(anchor="w")
                own_vars.append((var, a))

    def options() -> dict:
        cdp = cdp_var.get().strip()
        if cdp and not cdp.startswith(("http://127.0.0.1:", "http://localhost:")):
            raise ValueError("attach only to a browser on this PC, for example http://127.0.0.1:9222")
        cfg.values.update(keep_browser_profile=keep_var.get(), cdp=cdp, browser=browser_var.get(),
                          pictures=pictures_var.get())  # fmt: skip
        cfg.save()
        return {"keep_browser_profile": keep_var.get(), "cdp": cdp or None,
                "browser": browser_var.get(), "pictures": pictures_var.get()}  # fmt: skip

    def start(conf):
        if state["proc"] is not None:
            return
        try:
            conf.update(options())
        except ValueError as exc:
            messagebox.showerror("TwinLab Recorder", str(exc))
            return
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
        state["ready"] = False
        lines = mainframe_addresses(conf)
        rec_status.set(f"Recording {conf['name']}…" + ("\n" + "\n".join(lines) if lines else ""))
        stop_btn.configure(state="normal")
        for b in (start_offered, start_own):
            b.configure(state="disabled")
        root.after(1000, watch)

    def watch():
        proc = state["proc"]
        if proc is None:
            return
        if proc.poll() is None:
            journal = cfg.recordings / state["session"] / "journal.jsonl"
            if not state.get("ready") and journal.is_file() and '"kind": "browser"' in journal.read_text(
                    encoding="utf-8", errors="replace"):  # fmt: skip
                state["ready"] = True
                rec_status.set(rec_status.get() + "\nThe browser is ready: start the process now.")
            root.after(1000, watch)
            return
        state["proc"] = None
        if proc.returncode != 0:
            rec_status.set(f"The recording ended with an error (see {state.get('log')}). What was "
                           "recorded until then is kept.")  # fmt: skip
            messagebox.showerror("TwinLab Recorder", f"The recording ended with an error. The log is "
                                 f"{state.get('log')}")  # fmt: skip
        else:
            rec_status.set("Recording saved on this PC. Review it before sending.")
        stop_btn.configure(state="disabled")
        for b in (start_offered, start_own):
            b.configure(state="normal")
        refresh_done()
        body.select(tab_done)
        for i, s in enumerate(state.get("done") or []):
            if s["id"] == state["session"]:
                done_list.selection_set(i)
                show_done()

    def on_stop():
        if state["session"]:
            (cfg.recordings / state["session"] / "stop").touch()
            rec_status.set("Stopping: the recording is being finished…")

    def on_start_offered():
        sel = offered_list.curselection()
        if not sel:
            messagebox.showinfo("TwinLab Recorder", "Choose a session first.")
            return
        _labctl()
        s = state["offered"][sel[0]]
        start(session_config(s["targets"], state["deny"], s["name"], s["id"], s["automation_id"]))

    def on_start_own():
        _labctl()
        targets = [dict(a["target"]) for var, a in own_vars if var.get()]
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

    def on_send():
        sel = done_list.curselection()
        if not sel:
            return
        s = state["done"][sel[0]]
        if not messagebox.askyesno("TwinLab Recorder", "Send the references shown to the portal? "
                                   "The recording itself stays on this PC."):  # fmt: skip
            return
        try:
            result = send(cfg, s["id"], ca_bundle)
        except (PortalError, SystemExit) as exc:
            messagebox.showerror("TwinLab Recorder", str(exc))
            return
        messagebox.showinfo("TwinLab Recorder", f"Sent. The portal mapped it as {result.get('recording_id')}.")
        refresh_done()

    def on_pair():
        url = simpledialog.askstring("Pair", "Portal address", initialvalue=cfg.values.get("portal") or "https://")
        if not url:
            return
        code = simpledialog.askstring("Pair", "Pairing code from the portal's Machines page")
        if not code:
            return
        name = simpledialog.askstring("Pair", "A name for this machine", initialvalue=os.environ.get("COMPUTERNAME", ""))
        if not name:
            return
        try:
            doc = Portal(url, ca_bundle=ca_bundle).pair(code, name)
        except PortalError as exc:
            messagebox.showerror("TwinLab Recorder", str(exc))
            return
        cfg.paired(check_portal(url), doc["machine_id"], doc["name"], doc["token"])
        refresh_status()
        poll_portal()

    def on_profile():
        if state["proc"] is not None:
            messagebox.showinfo("TwinLab Recorder", "Stop the recording first.")
            return
        _labctl()
        from labctl.workstation.session import open_profile

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
        _labctl()
        from labctl.workstation.propose import propose

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

    propose_btn.configure(command=on_propose)
    profile_btn.configure(command=on_profile)
    pair_btn.configure(command=on_pair)
    stop_btn.configure(command=on_stop)
    start_offered.configure(command=on_start_offered)
    start_own.configure(command=on_start_own)
    send_btn.configure(command=on_send)
    done_list.bind("<<ListboxSelect>>", show_done)
    refresh_status()
    refresh_done()
    if cfg.values.get("token"):
        root.after(200, poll_portal)

    def on_close():
        if state["proc"] is not None and not messagebox.askyesno(
            "TwinLab Recorder", "A recording is running. Stop it and close?"):  # fmt: skip
            return
        on_stop()
        if state["proc"] is not None:
            try:
                state["proc"].wait(120)
            except subprocess.TimeoutExpired:
                pass
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


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
    rs = sub.add_parser("run-session")
    rs.add_argument("--config", required=True)
    rs.add_argument("--parent", type=int)
    sh = sub.add_parser("show")
    sh.add_argument("session_id")
    sd = sub.add_parser("send")
    sd.add_argument("session_id")
    sd.add_argument("--yes", action="store_true")
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
            _labctl()
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
            run_session(cfg, conf, parent_pid=args.parent)
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
        elif args.command == "browser":
            _labctl()
            from labctl.workstation.session import open_profile

            open_profile(browser_profile(cfg), url="https://microsoftedge.microsoft.com/addons/search/uipath")
            print(f"opened the kept profile {browser_profile(cfg)}: install UiPath's extension, close "
                  "the browser, then record with --keep-browser-profile")  # fmt: skip
        elif args.command == "propose":
            _labctl()
            from labctl.workstation.propose import propose

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
            _labctl()
            from labctl.workstation.session import recover_all

            done = recover_all(cfg.recordings)
            print("recovered: " + (", ".join(done) if done else "nothing"))
    except PortalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
