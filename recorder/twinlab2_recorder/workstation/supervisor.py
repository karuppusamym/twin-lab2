# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: imports
# renamed to this repository's packages, unused paths cut. Source and changes: docs/UPSTREAM.md.
"""Run each channel of a workstation session in a process of its own, and watch them.

    sup = Supervisor(session)      # twinlab2_recorder.workstation.session.Session, its folder open
    sup.start()                    # spawns the collectors, waits (bounded) until they are ready
    ...
    sup.shutdown()                 # asks, waits, then terminates; writes channels.json
    sup.channel_status()           # {"web": {"status", "gaps", "notes", "details"}, ...}

A channel is one kind of capture: `web` (the session browser, Playwright over CDP), `desktop`
(UI Automation and the input hooks, plus the watcher of the window in front) and `mainframe`
(the TN3270 proxies). Each one runs in a child process (multiprocessing, spawn), so a call that
never returns in one (a UI Automation call into a hung application, a page that blocks the
DevTools connection) cannot stop the others.

What the supervisor does:

- **heartbeats**: every child sends one each second, with how long ago its recorder last made
  progress (its main loop asking whether to stop). No heartbeat, or no progress, for
  `heartbeat_timeout` seconds marks the channel **failed**, with the reason and the time; the
  process is terminated, and the channel is started once more when that is safe (`RESTARTABLE`);
- **explicit failures**: each failure, restart, early end and cut-off is a `channel` row in the
  journal, a note of the session and a gap of the channel (`from`, `to`, `reason`): never silent;
- **bounded shutdown**: the children are asked to stop, given `shutdown_s` seconds, then
  terminated; the channel is recorded as cut off, when, and what was lost;
- **status** for the completeness manifest (`channel_status()`): per channel `complete`,
  `interrupted`, `missing` or `not_requested`, its gaps and its notes. It is also written to
  `channels.json` in the session folder each time it changes, so a session recovered after a
  crash still says what each channel did.

The children send their journal rows and notes here; only the session (in this process) writes
the journal. A frozen build (PyInstaller) must call `multiprocessing.freeze_support()` first
thing in its entry point, or each child starts the whole program again.
"""

from __future__ import annotations

import importlib
import json
import multiprocessing as mp
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from twinlab2_recorder.workstation import levels as lv

CHANNELS = ("web", "desktop", "mainframe")
STATUSES = ("complete", "interrupted", "missing", "not_requested")
HEARTBEAT_S = 1.0
HEARTBEAT_TIMEOUT_S = 30.0
START_TIMEOUT_S = 45.0  # a child's first heartbeat (a spawn imports Python afresh)
READY_WAIT_S = 60.0
SHUTDOWN_S = {"web": 90.0, "desktop": 120.0, "mainframe": 15.0}
RESTARTABLE = {"web": True, "desktop": True, "mainframe": False}
NOT_RESTARTABLE = {
    "mainframe": "restarting the TN3270 proxy would drop the terminal's connection and "
    "renumber its screens",
}
CAPTURE = {"web": ("web", "network"), "desktop": ("desktop",), "mainframe": ("mainframe",)}
RUNNERS = {
    "web": "twinlab2_recorder.workstation.supervisor:run_web",
    "desktop": "twinlab2_recorder.workstation.supervisor:run_desktop",
    "mainframe": "twinlab2_recorder.workstation.supervisor:run_mainframe",
}
BROWSER_MODES = ("controlled", "attach")
# Within the contract's 300 characters for a channel note (completeness.fit cuts any longer one).
CDP_WARNING = (
    "web: attached to a running browser over CDP (qualified mode, lower fidelity): Chromium-based "
    "only; pages and closed shadow roots opened before attaching are not fully recorded, the "
    "browser's extensions and settings change what is seen, and it is not restarted if it closes"
)
# Remote and virtual desktop clients: what runs inside them is pixels to this machine.
REMOTE_CLIENTS = {"wfica32.exe", "cdviewer.exe", "selfservice.exe", "mstsc.exe", "msrdc.exe",
                  "vmconnect.exe", "vncviewer.exe", "tvnviewer.exe", "anydesk.exe",
                  "teamviewer.exe", "vmware-view.exe", "horizon-client.exe"}  # fmt: skip
# A user step on a control of one of these types with no automation id and no name is a step
# on something UI Automation cannot tell apart (custom-drawn, a canvas, an owner-drawn grid).
GENERIC_TYPES = {None, "", "Pane", "Custom", "Window", "Image", "Group", "Document"}
USER_ACTIONS = {"click", "double_click", "right_click", "type", "select", "toggle", "expand",
                "collapse", "menu"}  # fmt: skip
SESSION_KW = ("name", "automation_id", "portal_session_id", "browser", "max_seconds",
              "pictures", "recorder", "browser_profile", "cdp", "cdp_port", "robot")  # fmt: skip


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def requested(targets: lv.Targets) -> set[str]:
    """The channels a session's targets need."""
    out = set()
    if targets.origins():
        out.add("web")
    if any(not t.origin and not t.tn3270 for t in targets.targets):
        out.add("desktop")
    if targets.mainframes():
        out.add("mainframe")
    return out


def browser_mode(session) -> str:
    """`controlled` (the default and recommended: the recorder starts the browser, with a profile
    of its own) or `attach` (a browser already running with a DevTools port)."""
    return "attach" if getattr(session, "attach_cdp", None) else "controlled"


# -- the parent's side -------------------------------------------------------------------------


class Channel:
    def __init__(self, name: str, runner: str | None) -> None:
        self.name = name
        self.runner = runner
        self.requested = runner is not None
        self.proc = None
        self.stop = None
        self.inbox = None
        self.state = "idle"  # idle starting running failed ended cut_off missing
        self.spawned = 0.0
        self.last_hb: float | None = None
        self.progress_age = 0.0
        self.exit: dict | None = None
        self.exit_seen: float | None = None
        self.ready_at: str | None = None
        self.ever_ready = False
        self.started_at: str | None = None
        self.ended_at: str | None = None
        self.stop_requested_at: str | None = None
        self.restarts = 0
        self.gaps: list[dict] = []
        self.notes: list[str] = []
        self.failures: list[dict] = []
        self.inaccessible: list[dict] = []
        self.last_note: str | None = None
        self.missing_reason: str | None = None
        self.browser_pids: set[int] = set()
        self.parts = 0
        self.mode: str | None = None

    def open_gap(self) -> dict | None:
        return next((g for g in reversed(self.gaps) if g.get("to") is None), None)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.is_alive()


class _MainframeAddress:
    """What the parent knows of a TN3270 proxy running in the mainframe collector."""

    def __init__(self, address: str) -> None:
        self.address = address
        self.listen = int(address.rsplit(":", 1)[1])

    def stop(self) -> None:  # the proxy itself stops with its collector
        pass

    def join(self, _timeout: float | None = None) -> None:
        pass


class Supervisor:
    def __init__(self, session, *, channels: dict[str, str] | None = None,
                 heartbeat_timeout: float = HEARTBEAT_TIMEOUT_S,
                 shutdown_s: float | dict[str, float] | None = None,
                 ready_wait: float = READY_WAIT_S,
                 start_timeout: float = START_TIMEOUT_S) -> None:  # fmt: skip
        self.session = session
        if channels is None:
            want = requested(session.targets)
            channels = {c: RUNNERS[c] for c in CHANNELS if c in want}
        self.channels = {c: Channel(c, channels.get(c)) for c in CHANNELS}
        self.heartbeat_timeout = heartbeat_timeout
        self.start_timeout = max(start_timeout, heartbeat_timeout)
        if isinstance(shutdown_s, int | float):
            self.shutdown_s = dict.fromkeys(CHANNELS, float(shutdown_s))
        else:
            self.shutdown_s = {**SHUTDOWN_S, **(shutdown_s or {})}
        self.ready_wait = ready_wait
        self.ctx = mp.get_context("spawn")
        self.out = self.ctx.Queue()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._done = threading.Event()
        self.stopping = False
        self.shut = False
        self.started_at: str | None = None
        self.ended_at: str | None = None
        web = self.channels["web"]
        if web.requested:
            web.mode = browser_mode(session)

    def runs(self, name: str) -> bool:
        return self.channels[name].requested

    # -- start ---------------------------------------------------------------------------------

    def start(self) -> None:
        self.started_at = _now()
        web = self.channels["web"]
        if web.requested and web.mode == "attach":
            # Shown at the start (the session's notes, the CLI) and kept in the manifest notes.
            web.notes.append(CDP_WARNING)
            self.session.note(CDP_WARNING)
            self._event(web, "warning", reason=CDP_WARNING)
        desktop = self.channels["desktop"]
        if desktop.requested and sys.platform != "win32" and desktop.runner == RUNNERS["desktop"]:
            desktop.state = "missing"
            desktop.missing_reason = "desktop recording needs Windows (UI Automation)"
            desktop.notes.append(desktop.missing_reason)
            self.session.note(f"desktop channel: {desktop.missing_reason}")
        for ch in self.channels.values():
            if ch.requested and ch.state == "idle":
                self._spawn(ch)
        self._thread = threading.Thread(target=self._loop, name="tl-supervisor", daemon=True)
        self._thread.start()
        self._write()
        end = time.monotonic() + self.ready_wait
        while time.monotonic() < end and not self.session.stopping():
            with self._lock:
                waiting = [c for c in self.channels.values()
                           if c.state in ("starting",) and not c.ever_ready]  # fmt: skip
            if not waiting:
                break
            time.sleep(0.1)
        with self._lock:
            for ch in self.channels.values():
                if ch.state == "starting" and not ch.ever_ready:
                    self.session.note(f"{ch.name} channel: not ready after {self.ready_wait:.0f} s; "
                                      "the session goes on and it is still being waited for")  # fmt: skip

    def _spec(self, ch: Channel, resume: bool) -> dict:
        s = self.session
        spec = {k: getattr(s, k, None) for k in ("automation_id", "portal_session_id", "browser",
                                                   "max_seconds", "pictures", "robot",
                                                   "cdp_port")}  # fmt: skip
        spec.update(name=s.name, recorder=s.recorder_name, browser_profile=s.browser_profile,
                    cdp=s.attach_cdp, root=str(s.folder.parent), session_id=s.id,
                    targets=s.targets, started=s.started, resume=resume,
                    web_part=_web_parts(s.folder) if ch.name == "web" else 0,
                    heartbeat_s=HEARTBEAT_S)  # fmt: skip
        return spec

    def _spawn(self, ch: Channel, resume: bool = False) -> None:
        ch.stop = self.ctx.Event()
        ch.inbox = self.ctx.Queue()
        ch.exit, ch.exit_seen, ch.last_hb, ch.progress_age = None, None, None, 0.0
        ch.proc = self.ctx.Process(target=_child, name=f"tl-{ch.name}",
                                   args=(ch.name, ch.runner, self._spec(ch, resume), self.out,
                                         ch.inbox, ch.stop),
                                   daemon=True)  # fmt: skip
        ch.spawned = time.monotonic()
        ch.state = "starting"
        ch.started_at = ch.started_at or _now()
        ch.proc.start()
        _bind_to_job(ch.proc.pid)
        self._event(ch, "started", pid=ch.proc.pid, restart=resume)

    # -- the loop: messages and heartbeats -----------------------------------------------------

    def _loop(self) -> None:
        next_check = 0.0
        while not self._done.is_set():
            try:
                msg = self.out.get(timeout=0.1)
            except queue.Empty:
                msg = None
            except (EOFError, OSError):
                break
            if msg is not None:
                try:
                    with self._lock:
                        self._dispatch(msg)
                except Exception as exc:  # noqa: BLE001 - one bad message never stops the watch
                    self.session.note_once(f"supervisor: a message was not understood: "
                                           f"{type(exc).__name__}")  # fmt: skip
            if time.monotonic() >= next_check:
                next_check = time.monotonic() + 0.2
                with self._lock:
                    self._check()

    def _drain(self, seconds: float = 0.0) -> None:
        end = time.monotonic() + seconds
        while True:
            try:
                msg = self.out.get(timeout=max(0.0, min(0.1, end - time.monotonic())) or 0.01)
            except queue.Empty:
                if time.monotonic() >= end:
                    return
                continue
            except (EOFError, OSError):
                return
            with self._lock:
                self._dispatch(msg)

    def _dispatch(self, msg: dict) -> None:
        ch = self.channels.get(msg.get("ch"))
        if ch is None:
            return
        t = msg.get("t")
        s = self.session
        if t == "hb":
            ch.last_hb = time.monotonic()
            ch.progress_age = float(msg.get("progress_age") or 0.0)
        elif t == "row":
            row = msg["row"]
            kind = row.get("kind")
            if kind == "app" and row.get("key"):
                s.apps.setdefault(row["key"], {k: v for k, v in row.items()
                                               if k not in ("kind", "ts", "pid")})  # fmt: skip
            elif kind == "mainframe" and row.get("listen"):
                s.mainframe.append(_MainframeAddress(str(row["listen"])))
            elif kind == "browser":
                s.cdp = row.get("cdp")
                ch.parts = max(ch.parts, int(row.get("part") or 1))
            s.journal(row)
        elif t == "note":
            ch.last_note = msg.get("text")
            s.note(msg.get("text") or "")
        elif t == "browser":
            ch.browser_pids = {int(p) for p in msg.get("pids") or []}
            s.browser_pids = set(ch.browser_pids)
            desk = self.channels["desktop"]
            if desk.alive() and desk.inbox is not None:
                desk.inbox.put({"t": "browser_pids", "pids": sorted(ch.browser_pids)})
        elif t == "ready":
            if ch.state in ("starting", "running"):
                ch.state = "running"
                now = msg.get("ts") or _now()
                ch.ready_at = ch.ready_at or now
                ch.ever_ready = True
                gap = ch.open_gap()
                if gap is not None and gap.get("until_ready"):
                    gap["to"] = now
                    gap.pop("until_ready", None)
                self._event(ch, "ready")
                if ch.name == "web" and self.channels["desktop"].alive() and ch.browser_pids:
                    self.channels["desktop"].inbox.put(
                        {"t": "browser_pids", "pids": sorted(ch.browser_pids)}
                    )
        elif t == "gap":
            g = {"from": msg.get("from"), "to": msg.get("to"), "reason": msg.get("reason")}
            ch.gaps.append(g)
            self._event(ch, "gap", reason=g["reason"], **{"from": g["from"], "to": g["to"]})
            if g["to"] is None:
                g["live"] = True  # closed by a later message, or at the end
        elif t == "gap_end":
            for g in ch.gaps:
                if g.get("live") and g.get("reason") == msg.get("reason") and g["to"] is None:
                    g["to"] = msg.get("to") or _now()
                    g.pop("live", None)
        elif t == "inaccessible":
            entry = {k: v for k, v in msg.items() if k not in ("t", "ch")}
            ch.inaccessible.append(entry)
            if msg.get("note") and msg["note"] not in ch.notes:
                ch.notes.append(msg["note"])
                s.note(f"{ch.name}: {msg['note']}")
        elif t == "exit":
            ch.exit = msg
        if t not in ("hb", "row", "note"):
            self._write()

    def _check(self) -> None:
        now = time.monotonic()
        for ch in self.channels.values():
            if ch.state not in ("starting", "running") or ch.proc is None:
                continue
            if not ch.proc.is_alive():
                # Messages still on their way are read first: an exit message says how it ended.
                if ch.exit is None:
                    if ch.exit_seen is None:
                        ch.exit_seen = now
                        continue
                    if now - ch.exit_seen < 1.0:
                        continue
                self._ended(ch)
                self._write()
                continue
            if self.stopping:
                continue  # stopping: the shutdown deadline decides, not the heartbeats
            if ch.last_hb is None:
                if now - ch.spawned > self.start_timeout:
                    self._fail(ch, f"no heartbeat within {self.start_timeout:.0f} s of starting "
                                   "(the collector process did not start)")  # fmt: skip
                continue
            if now - ch.last_hb > self.heartbeat_timeout:
                self._fail(ch, f"no heartbeat for {now - ch.last_hb:.0f} s (the collector "
                               "process is frozen)")  # fmt: skip
            elif ch.progress_age > self.heartbeat_timeout:
                self._fail(ch, f"its recorder made no progress for {ch.progress_age:.0f} s (a "
                               "call into the application or the browser did not return)")  # fmt: skip

    def _ended(self, ch: Channel) -> None:
        code = ch.proc.exitcode if ch.proc is not None else None
        ch.ended_at = _now()
        exit_msg = ch.exit or {}
        error = exit_msg.get("error")
        if self.stopping or exit_msg.get("stopped"):
            ch.state = "ended"
            if error:
                ch.notes.append(f"ended with an error: {error}")
                self.session.note(f"{ch.name} channel: ended with an error: {error}")
            self._event(ch, "ended", exitcode=code)
            return
        if not exit_msg:
            self._fail(ch, f"the collector process ended unexpectedly (exit code {code})")
        elif error:
            self._fail(ch, f"the collector stopped with an error: {error}")
        else:
            # It returned by itself: nothing more to record (no browser, no DevTools port...).
            # Starting it again would end the same way.
            reason = ch.last_note or "the collector ended before the session"
            ch.state = "ended"
            if not ch.ever_ready:
                ch.state = "missing"
                ch.missing_reason = reason
            else:
                ch.gaps.append({"from": ch.ended_at, "to": None, "reason": reason})
            self._event(ch, "ended_early", reason=reason, exitcode=code)
            self.session.note(f"{ch.name} channel ended before the session: {reason}")

    def _fail(self, ch: Channel, reason: str) -> None:
        self._fail_now(ch, reason)
        self._write()

    def _fail_now(self, ch: Channel, reason: str) -> None:
        at = _now()
        ch.failures.append({"at": at, "reason": reason})
        self._event(ch, "failed", reason=reason)
        self._terminate(ch)
        ch.ended_at = at
        lost = " (data it had not written yet is lost)"
        restart_ok = RESTARTABLE.get(ch.name, False) and ch.restarts < 1 and not self.stopping
        if restart_ok and not self.session.stopping():
            ch.restarts += 1
            ch.gaps.append({"from": at, "to": None, "reason": f"collector failed: {reason}{lost}; "
                            "restarted", "until_ready": True})  # fmt: skip
            self.session.note(f"{ch.name} channel failed at {at}: {reason}; it was restarted")
            self._event(ch, "restarted")
            self._spawn(ch, resume=True)
            return
        why = ("it was already restarted once" if ch.restarts >= 1 else
               NOT_RESTARTABLE.get(ch.name, "the session is stopping"))  # fmt: skip
        ch.gaps.append({"from": at, "to": None,
                        "reason": f"collector failed: {reason}{lost}; not restarted: {why}"})  # fmt: skip
        ch.state = "failed"
        if not ch.ever_ready:
            ch.missing_reason = reason
        self.session.note(f"{ch.name} channel failed at {at}: {reason}; not restarted: {why}")

    def _terminate(self, ch: Channel) -> None:
        p = ch.proc
        if p is None:
            return
        if p.is_alive():
            p.terminate()
            p.join(5)
        if p.is_alive():
            p.kill()
            p.join(5)
        if ch.name == "web":
            self._clean_web(ch)

    def _clean_web(self, ch: Channel) -> None:
        """The web collector was terminated: the browser it started (controlled mode) is ended
        and its profile deleted, as the collector would have done."""
        if ch.mode != "controlled":
            return
        from twinlab2_recorder.workstation import session as ses

        pids = set(ch.browser_pids)
        profile = self.session.browser_profile or (self.session.folder / "web-profile")
        pids |= set(ses._profile_pids(profile))
        for pid in pids:
            _kill_tree(pid)
        if self.session.browser_profile is None and profile.exists():
            ses.remove_profile(profile, seconds=10)
        ch.browser_pids = set()

    def _event(self, ch: Channel, event: str, **facts) -> None:
        self.session.journal({"kind": "channel", "channel": ch.name, "event": event,
                              **{k: v for k, v in facts.items() if v is not None}})  # fmt: skip

    # -- shutdown ------------------------------------------------------------------------------

    def shutdown(self) -> dict:
        """Ask every collector to stop, wait at most `shutdown_s` (per channel), then terminate
        what is left and record it as cut off. Returns channel_status()."""
        if self.shut:
            return self.channel_status()
        self.shut = True
        with self._lock:
            self.stopping = True
            asked = time.monotonic()
            at = _now()
            for ch in self.channels.values():
                if ch.alive():
                    ch.stop.set()
                    ch.stop_requested_at = at
        while True:
            with self._lock:
                left = [c for c in self.channels.values() if c.alive()]
                late = [c for c in left if time.monotonic() - asked > self.shutdown_s[c.name]]
                for ch in late:
                    cut = _now()
                    limit = self.shutdown_s[ch.name]
                    self._terminate(ch)
                    ch.state = "cut_off"
                    ch.ended_at = cut
                    reason = (f"did not stop within {limit:.0f} s of being asked: terminated "
                              "(what it had not written yet is lost)")  # fmt: skip
                    ch.gaps.append({"from": ch.stop_requested_at, "to": cut, "reason": reason})
                    self._event(ch, "cut_off", reason=reason)
                    self.session.note(f"{ch.name} channel was cut off at {cut}: {reason}")
            if not left or len(late) == len(left):
                break
            time.sleep(0.1)
        self._drain(0.5)
        self._done.set()
        if self._thread is not None:
            self._thread.join(5)
        self._drain(0.2)
        with self._lock:
            self.ended_at = _now()
            for ch in self.channels.values():
                if ch.state in ("starting", "running") and ch.proc is not None:
                    ch.state = "ended"
                    ch.ended_at = ch.ended_at or self.ended_at
                for g in ch.gaps:
                    if g.get("to") is None:
                        g["to"] = self.ended_at
                    g.pop("until_ready", None)
                    g.pop("live", None)
            self._write()
        try:
            self.out.close()
        except (OSError, ValueError):
            pass
        return self.channel_status()

    # -- status --------------------------------------------------------------------------------

    def channel_status(self) -> dict:
        """Per channel: `{"status": complete|interrupted|missing|not_requested, "gaps": [{"from",
        "to", "reason"}], "notes": [...], "details": {...}}`. A gap still open has `to` null."""
        out = {}
        with self._lock:
            for name in CHANNELS:
                ch = self.channels[name]
                if not ch.requested:
                    out[name] = {"status": "not_requested", "gaps": [], "notes": [], "details": {}}
                    continue
                gaps = [{"from": g.get("from"), "to": g.get("to"), "reason": g.get("reason")}
                        for g in ch.gaps]  # fmt: skip
                if not ch.ever_ready:
                    status = "missing"
                    reason = ch.missing_reason or (ch.failures[-1]["reason"] if ch.failures
                                                   else "the collector was not ready")  # fmt: skip
                    if not gaps:
                        gaps = [{"from": self.started_at, "to": self.ended_at, "reason": reason}]
                elif gaps:
                    status = "interrupted"
                else:
                    status = "complete"
                details = {"state": ch.state, "ready_at": ch.ready_at, "ended_at": ch.ended_at,
                           "restarts": ch.restarts, "failures": list(ch.failures)}  # fmt: skip
                if name == "web":
                    details["mode"] = ch.mode
                if ch.inaccessible:
                    details["inaccessible"] = list(ch.inaccessible)
                out[name] = {"status": status, "gaps": gaps, "notes": list(ch.notes),
                             "details": details}  # fmt: skip
        return out

    def _write(self) -> None:
        try:
            folder = self.session.folder
            if folder.is_dir():
                tmp = folder / "channels.tmp"
                tmp.write_text(json.dumps({"channels": self.channel_status(),
                                           "started": self.started_at, "ended": self.ended_at},
                                          indent=1, ensure_ascii=False), encoding="utf-8")  # fmt: skip
                tmp.replace(folder / "channels.json")
        except OSError:
            pass


def read_status(folder: Path) -> dict | None:
    """The channel status a session wrote (`channels.json`), or None."""
    path = Path(folder) / "channels.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("channels")
    except (OSError, ValueError):
        return None


def _web_parts(folder: Path) -> int:
    """How many web parts the session folder already has (web, web-2, ...)."""
    n = 1 if (folder / "web").exists() else 0
    for p in folder.glob("web-*"):
        if p.name[4:].isdigit():
            n = max(n, int(p.name[4:]))
    return n


_JOB = None


def _bind_to_job(pid: int) -> None:
    """Windows: put a collector in a job that ends with this process (kill on close), so a
    session killed outright never leaves its collectors recording."""
    global _JOB
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    k = ctypes.windll.kernel32
    k.CreateJobObjectW.restype = wintypes.HANDLE
    k.OpenProcess.restype = wintypes.HANDLE
    k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                          wintypes.DWORD]  # fmt: skip
    k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    if _JOB is None:

        class Basic(ctypes.Structure):
            _fields_ = [("a", ctypes.c_int64), ("b", ctypes.c_int64), ("LimitFlags", wintypes.DWORD),
                        ("c", ctypes.c_size_t), ("d", ctypes.c_size_t), ("e", wintypes.DWORD),
                        ("f", ctypes.c_size_t), ("g", wintypes.DWORD), ("h", wintypes.DWORD)]  # fmt: skip

        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", ctypes.c_uint64 * 6), ("p", ctypes.c_size_t),
                        ("j", ctypes.c_size_t), ("pp", ctypes.c_size_t), ("pj", ctypes.c_size_t)]  # fmt: skip

        job = k.CreateJobObjectW(None, None)
        if not job:
            return
        info = Extended()
        info.basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            k.CloseHandle(job)
            return
        _JOB = job  # kept open for the life of this process
    h = k.OpenProcess(0x0101, False, pid)  # PROCESS_SET_QUOTA | PROCESS_TERMINATE
    if h:
        k.AssignProcessToJobObject(_JOB, h)
        k.CloseHandle(h)


def _kill_tree(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


# -- the child's side --------------------------------------------------------------------------


class ChildContext:
    """What a collector process has: where to send rows, notes and gaps, and whether to stop."""

    def __init__(self, channel: str, out, inbox, stop, heartbeat_s: float = HEARTBEAT_S) -> None:
        self.channel = channel
        self.out = out
        self.inbox = inbox
        self._stop = stop
        self.local_stop = threading.Event()
        self.heartbeat_s = heartbeat_s
        self.last_progress = time.monotonic()
        self.handlers: dict[str, list] = {}

    def send(self, msg: dict) -> None:
        self.out.put(dict(msg, ch=self.channel))

    def progress(self) -> None:
        self.last_progress = time.monotonic()

    def stopping(self) -> bool:
        return self._stop.is_set() or self.local_stop.is_set()

    def ready(self) -> None:
        self.send({"t": "ready", "ts": _now()})

    def note(self, text: str) -> None:
        self.send({"t": "note", "text": text})

    def gap(self, start: str, end: str | None, reason: str) -> None:
        self.send({"t": "gap", "from": start, "to": end, "reason": reason})

    def on(self, kind: str, fn) -> None:
        self.handlers.setdefault(kind, []).append(fn)

    def start(self) -> None:
        threading.Thread(target=self._beat, name="tl-heartbeat", daemon=True).start()
        threading.Thread(target=self._parent, name="tl-parent", daemon=True).start()
        threading.Thread(target=self._read_inbox, name="tl-inbox", daemon=True).start()

    def _beat(self) -> None:
        # Its own thread: it shows the process lives; progress_age shows the recorder moves.
        while True:
            try:
                self.send({"t": "hb", "progress_age": time.monotonic() - self.last_progress})
            except (OSError, ValueError):
                return
            time.sleep(self.heartbeat_s)

    def _parent(self) -> None:
        parent = mp.parent_process()
        if parent is None:
            return
        parent.join()  # returns when the session's process is gone
        # The session was killed: so is this collector, at once (its capture journals stay
        # unclosed, which recovery reads as interrupted, with the loss window). Recording on
        # would hold the session's files while the next start recovers them.
        self.local_stop.set()
        os._exit(70)

    def _read_inbox(self) -> None:
        while True:
            try:
                msg = self.inbox.get()
            except (EOFError, OSError, ValueError):
                return
            for fn in self.handlers.get(msg.get("t"), []):
                try:
                    fn(msg)
                except Exception:  # noqa: BLE001 - a message the collector cannot use
                    pass


def _resolve(runner: str):
    module, _, attr = runner.partition(":")
    return getattr(importlib.import_module(module), attr)


def _child(channel: str, runner: str, spec: dict, out, inbox, stop) -> None:
    # Ctrl+C reaches every process of the console: the session decides when its collectors stop.
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except (OSError, ValueError):
        pass
    ctx = ChildContext(channel, out, inbox, stop, spec.get("heartbeat_s") or HEARTBEAT_S)
    ctx.start()
    error = None
    try:
        fn = _resolve(runner)
        ctx.progress()  # importing the collector is not a stall
        fn(ctx, spec)
    except BaseException as exc:  # noqa: BLE001 - reported to the supervisor
        error = f"{type(exc).__name__}: {str(exc)[:300]}"
    try:
        ctx.send({"t": "exit", "error": error, "stopped": ctx.stopping()})
        out.close()
        out.join_thread()
    except (OSError, ValueError):
        pass


def channel_session(ctx: ChildContext, spec: dict):
    """The session as a collector process sees it: its rows and notes go to the supervisor, and
    it stops when the supervisor says so."""
    from twinlab2_recorder.workstation.session import Session

    class ChannelSession(Session):
        def __init__(self) -> None:
            kw = {k: spec.get(k) for k in SESSION_KW if spec.get(k) is not None}
            kw["max_seconds"] = spec.get("max_seconds") or 4 * 3600
            super().__init__(Path(spec["root"]), spec["targets"], session_id=spec["session_id"],
                             **kw)  # fmt: skip
            self.ctx = ctx
            self.started = spec.get("started")
            self.web_part = int(spec.get("web_part") or 0)
            self.resume = bool(spec.get("resume"))
            self.isolated = False
            self._main = threading.get_ident()
            self._seen_remote: set[str] = set()
            # This collector's capture journals (twinlab2_recorder.workstation.journal), on the session's
            # files: a restarted collector appends to them; a killed one leaves them unclosed.
            from twinlab2_recorder.workstation import journal as capture

            self.capture = {c: capture.Journal(capture.path_of(self.folder, c), c)
                            for c in CAPTURE.get(ctx.channel, ())}  # fmt: skip

        def close_capture(self) -> None:
            for c, j in self.capture.items():
                j.close(clean=c not in self.failed)

        def journal(self, row: dict) -> None:
            row = dict(row, ts=row.get("ts") or _now())
            ctx.send({"t": "row", "row": row})
            if row.get("kind") == "browser":
                pids = set(self.browser_pids) | ({self.edge_pid} if self.edge_pid else set())
                ctx.send({"t": "browser", "pids": sorted(pids)})
                ctx.ready()

        def note(self, text: str) -> None:
            self.notes.append(text)
            ctx.note(text)

        def stopping(self) -> bool:
            if threading.get_ident() == self._main:
                ctx.progress()
            if ctx.stopping():
                self._stop.set()
            return self._stop.is_set()

        def _identify(self, pid: int, cache: dict):
            fresh = pid not in cache
            ident, level = super()._identify(pid, cache)
            if fresh and ctx.channel == "desktop":
                self._accessibility(pid, ident, level)
            return ident, level

        def _accessibility(self, pid: int, ident: dict, level: str) -> None:
            from twinlab2_recorder.workstation import session as ses

            exe = str(ident.get("exe") or "").lower()
            if exe in REMOTE_CLIENTS and "remote" not in self._seen_remote:
                self._seen_remote.add("remote")
                ctx.send({"t": "inaccessible", "kind": "remote_surface", "ts": _now(),
                          "note": "a remote or virtual desktop client (Citrix, RDP, VNC...) was "
                                  "used: what runs inside it is only pixels here; its controls "
                                  "and steps are not recorded"})  # fmt: skip
            if self.targets.target_of(ident) is None or level == lv.PRESENCE:
                return
            if ses._elevated(pid) and not ses._elevated(os.getpid()):
                ctx.send({"t": "inaccessible", "kind": "elevated", "ts": _now(),
                          "app": ident.get("key"),
                          "note": f"{ident.get('exe')} runs as administrator: this recorder "
                                  "(not elevated) cannot read its windows; its steps are missing"})  # fmt: skip

        def _run_desktop(self) -> None:
            from twinlab2_recorder.desktop.recorder import Recorder

            rec = Recorder(self.folder / "desktop", set(), session=True, stop=self.stopping,
                           max_seconds=self.max_seconds, pictures=self.pictures,
                           admit=self.admit, unfocused_values=self.robot,
                           journal=self.capture.get("desktop"), resume=self.resume)  # fmt: skip
            self.first_seq = rec.seq
            for pid, lvl in self._pending:
                rec.add_process(pid, lvl)
            self._pending = []
            self.desktop = rec
            self._channel_open("desktop")
            ctx.ready()
            try:
                rec.run()
            except Exception:
                self.failed.add("desktop")
                raise

    return ChannelSession()


def _playwright_chromium() -> str | None:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            exe = pw.chromium.executable_path
        return exe if exe and Path(exe).is_file() else None
    except Exception:  # noqa: BLE001 - Playwright or its browser not installed
        return None


def run_web(ctx: ChildContext, spec: dict) -> None:
    from twinlab2_recorder.workstation.session import find_browser

    s = channel_session(ctx, spec)
    if not s.attach_cdp and find_browser(s.browser) is None:
        exe = _playwright_chromium()
        if exe:
            s.browser = exe
            s.note("no Edge or Chrome installed: the session browser is Playwright's Chromium")
    try:
        s._web()
    finally:
        s.close_capture()


def run_mainframe(ctx: ChildContext, spec: dict) -> None:
    s = channel_session(ctx, spec)
    s._start_mainframes()
    ctx.ready()
    try:
        while not s.stopping():
            time.sleep(0.2)
    finally:
        for proxy in s.mainframe:
            proxy.stop()
        for proxy in s.mainframe:
            proxy.join(10)
        s.close_capture()


def desktop_locked() -> bool | None:
    """Is the input desktop something other than the user's (locked, the sign-in screen, a UAC
    prompt on the secure desktop)? None when it cannot be told (not Windows)."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    u = ctypes.windll.user32
    u.OpenInputDesktop.restype = wintypes.HANDLE
    u.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    u.CloseDesktop.argtypes = [wintypes.HANDLE]
    u.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]  # fmt: skip
    h = u.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
    if not h:
        return True  # this user may not open it: the secure desktop
    try:
        buf = ctypes.create_unicode_buffer(256)
        need = wintypes.DWORD()
        if not u.GetUserObjectInformationW(h, 2, buf, ctypes.sizeof(buf), ctypes.byref(need)):
            return None
        return buf.value.lower() != "default"
    finally:
        u.CloseDesktop(h)


LOCKED_REASON = ("the desktop was locked or showed the secure desktop (sign-in, UAC prompt): "
                 "nothing on it can be read")  # fmt: skip


def _watch_lock(ctx: ChildContext, stop: threading.Event, check=desktop_locked) -> None:
    since = None
    while not stop.is_set():
        try:
            locked = check()
        except Exception:  # noqa: BLE001 - one failed read is not a lock
            locked = None
        if locked and since is None:
            since = _now()
            ctx.gap(since, None, LOCKED_REASON)
        elif locked is False and since is not None:
            ctx.send({"t": "gap_end", "reason": LOCKED_REASON, "to": _now()})
            since = None
        stop.wait(1.0)
    if since is not None:
        ctx.send({"t": "gap_end", "reason": LOCKED_REASON, "to": _now()})


def inaccessible_steps(rows: list[dict], after_seq: int = 0) -> list[dict]:
    """Desktop user steps on controls UI Automation cannot tell apart: no element at all, or one
    of a generic type with no automation id and no name (custom-drawn, a canvas)."""
    out = []
    for r in rows:
        if int(r.get("seq") or 0) <= after_seq or r.get("action") not in USER_ACTIONS:
            continue
        target = r.get("target")
        if not target:
            why = "no element under the input"
        else:
            props = target.get("props") or {}
            kind = target.get("type") or props.get("ControlType")
            if props.get("AutomationId") or props.get("Name") or target.get("name"):
                continue
            if kind not in GENERIC_TYPES or props.get("Role"):
                continue
            why = f"a {kind or 'control'} with no name or automation id"
        out.append({"seq": r.get("seq"), "ts": r.get("ts"), "action": r.get("action"),
                    "why": why})  # fmt: skip
    return out


def run_desktop(ctx: ChildContext, spec: dict) -> None:
    if sys.platform != "win32":
        raise RuntimeError("desktop recording needs Windows (UI Automation)")
    # twinlab2_recorder.desktop.win first, in this thread: it sets COM to multithreaded before comtypes
    # loads (UI Automation calls its event handlers on its own threads).
    from twinlab2_recorder.desktop import win  # noqa: F401
    from twinlab2_recorder.workstation import session as ses
    from twinlab2_recorder.workstation import timeline

    s = channel_session(ctx, spec)
    if ses._apartment() not in (None, 1):
        s.note("COM is single-threaded in the recording thread: UI Automation events (typing, "
               "selections) may be missed")  # fmt: skip

    def browser_pids(msg: dict) -> None:
        s.browser_pids = {int(p) for p in msg.get("pids") or []}

    ctx.on("browser_pids", browser_pids)
    done = threading.Event()
    threading.Thread(target=s._watch, name="tl-watch", daemon=True).start()
    threading.Thread(target=_watch_lock, args=(ctx, done), name="tl-lock", daemon=True).start()
    try:
        s._run_desktop()
    finally:
        done.set()
        s.stop()
        s.close_capture()
    rows = timeline.read_jsonl(s.folder / "desktop" / "steps.jsonl")
    found = inaccessible_steps(rows, getattr(s, "first_seq", 0))
    if found:
        ctx.send({"t": "inaccessible", "kind": "controls", "count": len(found),
                  "steps": found[:50],
                  "note": f"{len(found)} desktop step(s) on controls UI Automation cannot tell "
                          "apart (no name or automation id: custom-drawn or inaccessible): "
                          "their targets are not identified"})  # fmt: skip
