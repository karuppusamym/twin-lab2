# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: imports
# renamed to this repository's packages, unused paths cut. Source and changes: docs/UPSTREAM.md.
"""Run a workstation session: the web and desktop recorders in one process, one folder.

    Session(root, Targets.from_list([...]), name="order-to-invoice").run()

`run()` blocks until `stop()`, a `stop` file in the session folder, `max_seconds`, or (with
`script`) the script returning. While it runs:

- a **watcher** (its own thread, every 250 ms) reads which window is in front, identifies its
  process (twinlab2_recorder.workstation.levels) and gives it its level: a target's process is added to the
  desktop recorder (`add_process`), every other application gets a presence row in the journal
  (executable name and time, kept on this machine only); every 2 s it also looks for target
  processes that have a window but have not been in front yet;
- the **desktop recorder** runs in the calling thread, in session mode (it starts with no process
  and stops only when the session stops); it is not started when no target is a desktop one, so
  no input hook is installed then;
- the **web recorder** runs in its own thread when a target is a web origin: the session starts
  the Edge installed on the machine (Chrome when there is no Edge) with a profile of its own and
  attaches to it (twinlab2_recorder.web.recorder.attach with the target origins); pages of other origins are
  presence rows. The profile is deleted when the session ends.

Every event is written to the session's capture journal as it is captured
(twinlab2_recorder.workstation.journal: `capture/<channel>.jsonl`, fsync'd every 0.5 s; loss window 2 s),
with its permanent `event_id`; so are the web recorder's HAR entries (`capture/network.jsonl`).
`journal.jsonl` (the session's own rows) is fsync'd the same way.

The PC's exclude list (`targets.exclude`, twinlab2_recorder.workstation.exclude) is applied as
events are captured: an excluded program, a window whose title is excluded and an excluded web
address get no row at all, not even presence, and their windows are painted over in desktop
pictures (`exclusion_masks`). The session keeps one `excluded` row per entry that matched, with
the entry's name only (no time, title or address): the completeness manifest counts them.

At the end `session.json` says `stopped` and twinlab2_recorder.workstation.timeline writes `session.jsonl`
and `portal-outbox.json`, with the completeness manifest (twinlab2_recorder.workstation.completeness). A
folder left `recording` (the program was killed) is rebuilt from its journal by `recover()`
(twinlab2_recorder.workstation.recovery: the steps, and the web HAR from the network journal), marked
`recovered: true`, with a gap for the loss window and each channel that did not close.

Two hooks for a collector supervisor: `channel_status` (a callable returning
`{channel: {"status", "gaps", "notes"}}`, merged into the manifest) and `preflight` (the
`{"checks": [...], "ready"}` block of twinlab2_recorder.workstation.preflight), both kept in
`session.json`.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from twinlab2_recorder.workstation import hosts
from twinlab2_recorder.workstation import journal as capture
from twinlab2_recorder.workstation import levels as lv
from twinlab2_recorder.workstation import timeline

POLL_S = 0.25
SCAN_S = 2.0
EDGE = [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"]  # fmt: skip
CHROME = [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
          r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"]  # fmt: skip


def app_facts(ident: dict, level: str, target: lv.Target | None) -> dict:
    """The journal's `app` row of a process (without kind and pid). A target's catalog id and
    name are the application's only when the target names it: a target naming a host
    (`exe: javaw.exe`) records every application that host runs, each as its own."""
    facts = {k: ident.get(k) for k in ("key", "surface", "exe", "company", "product",
                                       "version", "display", "description")}  # fmt: skip
    # the application a host runs (hosts.resolve): its runtime and file name, on this PC
    facts.update({k: ident[k] for k in ("runtime", "app_file", "package") if ident.get(k)})
    facts["level"] = level
    if target is not None and (not ident.get("app_file") or target.key == ident.get("key")):
        if target.app_id is not None:
            facts["app_id"] = target.app_id
        if target.name:
            facts["display"] = target.name
    return facts


def _hosted(pid: int, ident: dict, path: str | None, win, depth: int = 0) -> tuple[dict, str | None]:
    """The identity of the application a host process runs (workstation.hosts.resolve), and the
    path its level is decided by; a WebView2 is its hosting process's application. A program of
    its own is read only for its package (a packaged one without company and product)."""
    if not ident.get("host") and ident.get("company") and ident.get("product"):
        return ident, path
    try:
        facts = win.host_facts(pid) if ident.get("host") else {"package": win.package_family(pid)}
    except Exception:  # noqa: BLE001 - an unreadable process stays what its image says
        facts = {}
    out = hosts.resolve(ident, facts, win.exe_info, path)
    if not out.pop("via_parent", False):
        return out, path
    parent = win.parent_pid(pid) if depth == 0 else None
    ppath = win.process_path(parent) if parent else None
    if ppath:
        pident, ppath = _hosted(parent, lv.desktop_identity(ppath, win.exe_info(ppath)), ppath, win, 1)
        if not pident.get("host"):
            return pident, ppath
    return out, path


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]


def find_browser(explicit: str | None = None) -> str | None:
    """The browser to start: a path, `edge` or `chrome`; Edge first when none is named."""
    local = os.environ.get("LOCALAPPDATA") or ""
    edge = EDGE + [str(Path(local) / r"Microsoft\Edge\Application\msedge.exe")]
    chrome = CHROME + [str(Path(local) / r"Google\Chrome\Application\chrome.exe")]
    name = (explicit or "").strip().lower()
    if name in ("edge", "msedge"):
        order = edge
    elif name in ("chrome", "google chrome"):
        order = chrome
    else:
        order = ([explicit] if explicit else []) + edge + chrome
    for path in order:
        if path and Path(path).is_file():
            return path
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _write_json(path: Path, value: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def twinlab_commit() -> str | None:
    """The Twin Lab commit these collectors come from: baked into a frozen build
    (`TWINLAB_COMMIT`), else read from the checkout."""
    if os.environ.get("TWINLAB_COMMIT"):
        return os.environ["TWINLAB_COMMIT"]
    try:
        out = subprocess.run(["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=5)  # fmt: skip
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


class Session:
    def __init__(self, root: Path, targets: lv.Targets, *, name: str = "",
                 session_id: str | None = None, automation_id: int | None = None,
                 portal_session_id: int | None = None, browser: str | None = None,
                 max_seconds: float = 4 * 3600, stop: Callable[[], bool] | None = None,
                 pictures: bool = True, recorder: str = "twinlab2_recorder.workstation",
                 browser_profile: Path | None = None, cdp: str | None = None,
                 cdp_port: int | None = None, robot: bool = False,
                 parent_pid: int | None = None,
                 channel_status: Callable[[], dict] | None = None,
                 preflight: dict | None = None) -> None:  # fmt: skip
        self.id = session_id or new_id()
        # Hooks for a collector supervisor (twinlab2_recorder.workstation.supervisor): what it knows of
        # each channel at the end, and the preflight checks run before the session.
        self.channel_status = channel_status
        self.preflight = preflight
        self.capture: dict[str, capture.Journal] = {}  # channel -> its capture journal
        self.failed: set[str] = set()  # channels whose collector stopped on an error
        # The session's browser: a profile of its own, deleted at the end (the default); a
        # profile that is kept (`browser_profile`: so an extension such as UiPath's, installed
        # once, is there in every session; its cookies stay too); or a browser that is already
        # running with a DevTools port (`cdp`), attached to and left running.
        self.browser_profile = Path(browser_profile) if browser_profile else None
        self.attach_cdp = cdp
        # The DevTools port of the browser the session starts: fixed for a kept profile (9333),
        # so a robot can attach to it the same way every time; any free port otherwise.
        self.cdp_port = cdp_port
        # A robot records: values set without the keyboard focus are steps too (UiPath Simulate).
        self.robot = robot
        # Read the UiPath robot's own log for the session's time (twinlab2_recorder.workstation.robot).
        self.robot_log = robot
        # The program that started this session (the recorder's window): when it is gone, the
        # session stops instead of recording on for hours.
        self.parent_pid = parent_pid
        self._parent_check = 0.0
        self._noted: set[str] = set()
        self.excluded_names: set[str] = set()  # exclude entries that matched (names only)
        self._cache: dict[int, tuple[dict, str]] = {}
        self._added: set[int] = set()
        self._apps_lock = threading.Lock()
        self.folder = Path(root) / self.id
        self.targets = targets
        self.name = name or self.id
        self.automation_id = automation_id
        self.portal_session_id = portal_session_id
        self.browser = browser
        self.max_seconds = max_seconds
        self.stop_fn = stop
        self.pictures = pictures
        self.recorder_name = recorder
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.notes: list[str] = []
        self.apps: dict[str, dict] = {}
        self.desktop = None  # the desktop Recorder, once it runs
        self._pending: list[tuple[int, str]] = []  # processes found before the recorder ran
        self.edge_pid: int | None = None
        self.browser_pids: set[int] = set()  # every process of the session's browser
        self.cdp: str | None = None  # the session browser's DevTools address, once it is up
        self.mainframe: list = []  # the TN3270 proxies (twinlab2_recorder.workstation.tn3270.Proxy)
        self._journal = None
        self.started = None
        # Each channel (web, desktop, mainframe) runs in a process of its own, watched by
        # twinlab2_recorder.workstation.supervisor; TWINLAB_COLLECTORS=threads runs them in this process.
        self.isolated = os.environ.get("TWINLAB_COLLECTORS", "processes") != "threads"
        self.supervisor = None
        self.web_part = 0  # web parts already recorded (a restarted web collector goes on)
        # Paused by the window (twinlab2_recorder.workstation.pause): nothing is captured meanwhile.
        self.pause_flag = None
        self._pauses = None

    def paused(self) -> bool:
        flag = self.pause_flag
        return bool(flag is not None and flag())

    # -- journal ---------------------------------------------------------------------------------

    def journal(self, row: dict) -> None:
        if row.get("kind") == "excluded":  # the entry's name and nothing else, not even a time
            self._journal_row({"kind": "excluded", "name": str(row.get("name") or "")})
            return
        self._journal_row(dict(row, ts=row.get("ts") or _now()))

    def _journal_row(self, row: dict) -> None:
        if row.get("kind") in ("presence", "outside"):  # an event of the timeline: its id now
            capture.stamp(row, "web" if row["kind"] == "outside" else "desktop", _now())
        with self._lock:
            if self._journal is None:
                return
            self._journal.write(row)

    # -- the exclude list ------------------------------------------------------------------------

    def excluded(self, rule) -> None:
        """An exclude entry matched: its name is kept once (exclude.summary)."""
        label = getattr(rule, "label", None) or str(rule or "")
        if label and label not in self.excluded_names:
            self.excluded_names.add(label)
            self.journal({"kind": "excluded", "name": label})

    def excluded_url(self, url: str) -> str | None:
        """For the web recorder: the exclude entry an address falls under (its name), or None."""
        pol = self.targets.exclude
        rule = pol.url(url) if pol else None
        if rule is None:
            return None
        self.excluded(rule)
        return rule.label

    def excluded_window(self, hwnd: int) -> str | None:
        """For the desktop recorder: the exclude entry a window falls under, by its program (the
        application it shows) or its title, or None. The title is read without sending the window
        a message and is not kept."""
        pol = self.targets.exclude
        if not pol or not hwnd:
            return None
        from twinlab2_recorder.desktop import win

        try:
            pid = win.app_pid(hwnd)
            if pid and pid != os.getpid():
                ident, level = self._identify(pid, self._cache)
            else:
                ident, level = {}, None
            rule = (pol.ident(ident) if level == lv.EXCLUDED else None) or pol.title(_title(win, hwnd))
        except Exception:  # noqa: BLE001 - a window gone while it was read is not excluded
            return None
        if rule is not None:
            self.excluded(rule)
            return rule.label
        return None

    def exclusion_masks(self, hwnd: int) -> list[list[int]] | None:
        """Where excluded windows cover a picture of `hwnd`: window-relative rectangles of the
        excluded windows above it in Z order and of any embedded in it ([]: none); None when the
        picture must not be kept (mostly covered: exclude.MASK_LIMIT)."""
        pol = self.targets.exclude
        if not pol or not hwnd:
            return []
        from twinlab2_recorder.desktop import win
        from twinlab2_recorder.workstation import exclude

        target = _rect(win, hwnd)
        if target is None:
            return []
        own = win.window_pid(hwnd)
        others = [_rect(win, h) for h in _above(win, hwnd) + _embedded(win, hwnd, own)
                  if self.excluded_window(h)]  # fmt: skip
        rects, share = exclude.masks(target, [r for r in others if r])
        return None if share > exclude.MASK_LIMIT else rects

    def requested_channels(self) -> list[str]:
        """The channels this session's targets need (twinlab2_recorder.workstation.completeness)."""
        want = set()
        if self.targets.origins():
            want |= {"web", "network"}
        if self.targets.mainframes():
            want.add("mainframe")
        if any(not t.origin and not t.tn3270 for t in self.targets.targets):
            want.add("desktop")
        return [c for c in capture.CHANNELS if c in want]

    def _channel_open(self, channel: str) -> None:
        if channel in self.capture:
            self.capture[channel].open()

    def note(self, text: str) -> None:
        self.notes.append(text)
        self.journal({"kind": "note", "text": text})

    def note_once(self, text: str) -> None:
        if text not in self._noted:
            self._noted.add(text)
            self.note(text)

    def _meta(self, state: str) -> dict:
        return {"format": timeline.FORMAT, "id": self.id, "name": self.name, "state": state,
                "started": self.started, "ended": _now() if state != "recording" else None,
                "recorder": self.recorder_name, "twinlab_commit": twinlab_commit(),
                "automation_id": self.automation_id,
                "portal_session_id": self.portal_session_id,
                "targets": self.targets.as_list(), "deny_keys": sorted(self.targets.deny_keys),
                "channels": self.requested_channels(), "apps": self.apps, "notes": self.notes,
                "local_policy": _policy_facts(self.targets.exclude),
                **({"preflight": self.preflight} if self.preflight else {}),
                **({"pauses": self._pauses.close()} if self._pauses is not None and state != "recording" else {})}  # fmt: skip

    # -- stopping ------------------------------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()

    def stopping(self) -> bool:
        if self._stop.is_set():
            return True
        if (self.folder / "stop").exists() or (self.stop_fn is not None and self.stop_fn()):
            self._stop.set()
        if self.parent_pid and time.monotonic() > self._parent_check:
            self._parent_check = time.monotonic() + 2.0
            if not _alive(self.parent_pid):
                self.note_once("the recorder's window closed: the session stopped")
                self._stop.set()
        return self._stop.is_set()

    # -- applications --------------------------------------------------------------------------

    def _identify(self, pid: int, cache: dict) -> tuple[dict, str]:
        if pid in cache and not (
            cache[pid][0].get("browser")
            and self._session_browser(pid)
            and not cache[pid][0].get("session_browser")
        ):
            return cache[pid]
        from twinlab2_recorder.desktop import win

        path = win.process_path(pid)
        ident = lv.desktop_identity(path, win.exe_info(path) if path else {})
        ident, path = _hosted(pid, ident, path, win)
        ours = self._session_browser(pid)
        level = lv.PRESENCE if ours else self.targets.level(ident, path)
        if ours:
            ident = dict(ident, browser=True, session_browser=True)
        elif level == lv.EXCLUDED:
            self.excluded(self.targets.excluded(ident))
            target = self.targets.target_of(ident)
            if target is not None:
                self.note_once(f"{target.name or ident.get('exe')} is a target of this session, but "
                               "this PC's exclude list names it: it is not recorded")  # fmt: skip
        elif self.targets.target_of(ident) is not None:
            exe = ident.get("exe")
            if level == lv.PRESENCE:
                why = ("it is marked not replicated" if ident.get("key") in self.targets.deny_keys
                       else "a browser is recorded through the session's own browser"
                       if ident.get("browser") else f"it hosts other programs ({ident['reason']})"
                       if ident.get("host") and ident.get("reason")
                       else "it hosts other programs (java, python...)"
                       if ident.get("host") else "it is part of Windows or on the deny list")  # fmt: skip
                self.note_once(f"{exe} matches a target but is recorded only as used: {why}")
            elif _elevated(pid) and not _elevated(os.getpid()):
                self.note_once(f"{exe} runs as administrator: Windows does not let this recorder "
                               "read it; run the recorder as administrator too")  # fmt: skip
        cache[pid] = (ident, level)
        return ident, level

    def admit(self, pid: int) -> str | None:
        """For the desktop recorder: the level of a process it meets before the watcher did."""
        if pid == os.getpid():
            return None
        ident, level = self._identify(pid, self._cache)
        if level not in (lv.FULL, lv.STEPS):
            return None
        with self._apps_lock:
            if pid not in self._added:
                self._added.add(pid)
                self._record_process(pid, ident, level, tell_recorder=False)
        return level

    def _session_browser(self, pid: int) -> bool:
        return pid == self.edge_pid or pid in self.browser_pids

    def _record_process(
        self, pid: int, ident: dict, level: str, tell_recorder: bool = True
    ) -> None:
        facts = app_facts(ident, level, self.targets.target_of(ident))
        self.apps.setdefault(ident["key"], facts)
        self.journal({"kind": "app", "pid": pid, **facts})
        if not tell_recorder:
            return
        if self.desktop is not None:
            self.desktop.add_process(pid, level)
        else:
            self._pending.append((pid, level))

    def _watch(self) -> None:
        from twinlab2_recorder.desktop import win

        own = os.getpid()
        cache, added = self._cache, self._added
        last_pid, next_scan = None, 0.0
        while not self.stopping():
            if self.paused():  # not even that another application was used
                last_pid = None
                time.sleep(0.2)
                continue
            try:
                now = time.monotonic()
                if now >= next_scan:
                    next_scan = now + SCAN_S
                    for pid in _window_pids(win):
                        if pid in added or pid == own:
                            continue
                        ident, level = self._identify(pid, cache)
                        if level in (lv.FULL, lv.STEPS):
                            with self._apps_lock:
                                if pid in added:
                                    continue
                                added.add(pid)
                            self._record_process(pid, ident, level)
                fg = win.foreground()
                pid = win.app_pid(fg) if fg else 0  # behind a modern app's frame: the app
                if pid and pid != own and pid != last_pid:
                    ident, level = self._identify(pid, cache)
                    if level in (lv.FULL, lv.STEPS):
                        with self._apps_lock:
                            new = pid not in added
                            added.add(pid)
                        if new:
                            self._record_process(pid, ident, level)
                    elif level == lv.EXCLUDED or self._title_excluded(win, fg):
                        pass  # excluded on this PC: not even that it was used
                    elif not self._session_browser(pid):
                        # Only that some other application was used; never its title.
                        row = {"kind": "presence", "exe": ident.get("exe")}
                        if ident.get("host") and ident.get("reason"):
                            row["reason"] = ident["reason"]  # why a host is not an application
                        self.journal(row)
                    last_pid = pid
            except Exception as exc:  # noqa: BLE001 - the watcher must outlive one bad read
                self.note_once(f"watcher: {type(exc).__name__}")
            time.sleep(POLL_S)

    def _title_excluded(self, win, hwnd: int) -> bool:
        pol = self.targets.exclude
        rule = pol.title(_title(win, hwnd)) if pol else None
        if rule is not None:
            self.excluded(rule)
        return rule is not None

    def _outside(self, origin: str, ts: str) -> None:
        self.journal({"kind": "outside", "ts": ts, "denied": lv.denied_origin(origin)})

    # -- web ---------------------------------------------------------------------------------------

    def _web(self) -> None:
        if self.attach_cdp:
            self._web_attach(self.attach_cdp)
            return
        from twinlab2_recorder.web import recorder

        exe = find_browser(self.browser)
        if exe is None:
            self.note("no Edge or Chrome found: the web targets were not recorded")
            return
        start = self.targets.web_start() or "about:blank"
        keep = self.browser_profile is not None
        profile = self.browser_profile if keep else self.folder / "web-profile"
        port = self.cdp_port or (9333 if keep else _free_port())
        if not _port_free(port):
            port = _free_port()
            self.note_once(f"the browser's DevTools port was taken: it is {port} in this session")
        if keep:
            profile.mkdir(parents=True, exist_ok=True)
            if _profile_pids(profile):
                self.note("the kept browser profile is open in another window: close it and "
                          "start the session again; web targets not recorded")  # fmt: skip
                return
        cdp = f"http://127.0.0.1:{port}"
        # Headless (TWINLAB_BROWSER_HEADLESS=1, `record --headless`: proofs and tests on a PC
        # someone works at): no window opens, and every page is fitted to 1280x720.
        headless = browser_headless()
        shown = ["--headless=new", f"--window-size={WINDOW[0]},{WINDOW[1]}"] if headless else []
        part = self.web_part
        gone_at: str | None = None  # when the browser closed, until it records again
        try:
            # The browser lives as long as the session: a robot closing its tab, or the whole
            # browser, does not end the web recording. A browser closed is opened again with the
            # same profile and port (a robot opening the same profile then lands in it), and
            # recorded into the next part (web, web-2, ...).
            while not self.stopping() and part < self.web_part + 20:
                part += 1
                out = self.folder / ("web" if part == 1 else f"web-{part}")
                proc = subprocess.Popen([exe, *shown, f"--remote-debugging-port={port}",
                                         f"--user-data-dir={profile}", "--no-first-run",
                                         "--no-default-browser-check", "--disable-sync",
                                         "--disable-features=msEdgeStartupBoost,Translate",
                                         "--edge-skip-compat-layer-relaunch", "--new-window",
                                         start if part == self.web_part + 1
                                         else "about:blank"])  # fmt: skip
                self.edge_pid = proc.pid
                try:
                    if not _wait_cdp(cdp, 30, self.stopping):
                        if not self.stopping():
                            self.note("the browser did not open its DevTools port: web targets "
                                      "not recorded")  # fmt: skip
                        break
                    # A browser can hand itself over to another process when it starts: its
                    # windows belong to whatever process runs with the session's profile.
                    self.browser_pids = set(_profile_pids(profile)) | {proc.pid}

                    def ready(part=part):
                        # Announced once the recorder listens: a robot or a test that drives the
                        # session's browser starts now. Stays on this PC.
                        nonlocal gone_at
                        self.cdp = cdp
                        self.journal({"kind": "browser", "cdp": cdp, "part": part})
                        self._web_ready(gone_at)
                        gone_at = None

                    recorder.attach(cdp, out, max_seconds=self.max_seconds, stop=self.stopping,
                                    origins=self.targets.origins(), outside=self._outside,
                                    keep_open=True, on_ready=ready,
                                    viewport=recorder.VIEWPORT if headless else None,
                                    **self._web_journals())  # fmt: skip
                except Exception as exc:  # noqa: BLE001 - the desktop part goes on
                    self.note_once(f"web recorder: {type(exc).__name__}: {str(exc)[:200]}")
                    self.failed |= {"web", "network"}
                if self.stopping():
                    _close_browser(cdp, proc, profile)
                    break
                gone_at = gone_at or _now()
                _end_process_tree(proc)
                self.note_once("the browser was closed during the session: it was opened again")
                time.sleep(1.0)
        finally:
            for pid in _profile_pids(profile):
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
            if not keep and not remove_profile(profile):
                self.note("the browser profile folder could not be deleted: delete web-profile")

    def _web_attach(self, cdp: str) -> None:
        """Record a browser that is already running with a DevTools port (one a robot or a
        person started with --remote-debugging-port): its windows are the session's browser."""
        from twinlab2_recorder.web import recorder

        if not _wait_cdp(cdp, 30, self.stopping):
            self.note(f"no browser answers at {cdp}: web targets not recorded")
            return
        self.browser_pids = set(_port_pids(cdp))

        def ready():
            self.cdp = cdp
            self.journal({"kind": "browser", "cdp": cdp, "attached": True})
            self._web_ready(None)

        try:
            recorder.attach(cdp, self.folder / "web", max_seconds=self.max_seconds,
                            stop=self.stopping, origins=self.targets.origins(),
                            outside=self._outside, keep_open=True, on_ready=ready,
                            **self._web_journals())  # fmt: skip
        except Exception as exc:  # noqa: BLE001 - the desktop part goes on
            self.note(f"web recorder stopped: {type(exc).__name__}: {str(exc)[:200]}")
            self.failed |= {"web", "network"}

    def _web_journals(self) -> dict:
        return {"journal": self.capture.get("web"), "network_journal": self.capture.get("network"),
                "exclude": self.excluded_url if self.targets.exclude else None, "paused": self.paused}  # fmt: skip

    def _web_ready(self, gone_at: str | None) -> None:
        """The web recorder listens: its channels are capturing (again, after `gone_at`)."""
        for ch in ("web", "network"):
            self._channel_open(ch)
            if gone_at and ch in self.capture:
                self.capture[ch].gap(gone_at, _now(), "the browser was closed during the "
                                     "session and opened again")  # fmt: skip

    # -- run ---------------------------------------------------------------------------------------

    def run(self, script: Callable[[Session], None] | None = None) -> Path:
        self.folder.mkdir(parents=True, exist_ok=False)
        self.started = _now()
        self._journal = capture.Journal(self.folder / "journal.jsonl", ticks=False)
        # Isolated collectors open their own capture journals (twinlab2_recorder.workstation.supervisor).
        self.capture = {} if self.isolated else {
            ch: capture.Journal(capture.path_of(self.folder, ch), ch)
            for ch in self.requested_channels()}  # fmt: skip
        _write_json(self.folder / "session.json", self._meta("recording"))
        from twinlab2_recorder.workstation import pause

        self.pause_flag = pause.PauseFlag(self.folder)
        self._pauses = pause.Pauses(self.journal)
        self.pause_flag.on_change(self._pauses.changed)
        if self.pause_flag.paused:
            self._pauses.changed(True)
        threads = []
        desktop_targets = [t for t in self.targets.targets if not t.origin and not t.tn3270]
        started_procs = []
        sup = None
        try:
            if sys.platform == "win32" and not self.isolated:
                # twinlab2_recorder.desktop.win first: it sets COM to multithreaded before comtypes loads,
                # in this thread (the desktop recorder runs here). UI Automation calls its event
                # handlers on its own threads; a single-threaded apartment without a message
                # pump would never receive them (no typing, selection or toggle steps).
                from twinlab2_recorder.desktop import win  # noqa: F401

                if _apartment() not in (None, 1) and self.targets.targets:
                    self.note("COM is single-threaded in the recording thread: UI Automation "
                              "events (typing, selections) may be missed")  # fmt: skip
            if self.isolated:
                from twinlab2_recorder.workstation.supervisor import Supervisor

                sup = self.supervisor = Supervisor(self)
                if self.channel_status is None:
                    self.channel_status = sup.channel_status
            else:
                self._start_mainframes()
            for t in desktop_targets:
                if t.start:
                    started_procs.append(subprocess.Popen([t.start]))
            if sys.platform == "win32" and not (sup is not None and sup.runs("desktop")):
                threads.append(threading.Thread(target=self._watch, name="tl-watch", daemon=True))
            if self.targets.origins() and sup is None:
                threads.append(threading.Thread(target=self._web, name="tl-web", daemon=True))
            for th in threads:
                th.start()
            if sup is not None:
                sup.start()  # the collectors are ready (or said why not) before a script runs
            failure: list[BaseException] = []
            if script is not None:
                threading.Thread(target=self._script, args=(script, failure), daemon=True,
                                 name="tl-script").start()  # fmt: skip
            try:
                if sup is None and desktop_targets and sys.platform == "win32":
                    self._run_desktop()
                else:
                    end = time.monotonic() + self.max_seconds
                    while not self.stopping() and time.monotonic() < end:
                        time.sleep(0.2)
            except KeyboardInterrupt:
                self.note("stopped with Ctrl+C")
            self.stop()
            if sup is not None:
                sup.shutdown()
            for th in threads:
                try:
                    th.join(300)
                except KeyboardInterrupt:
                    th.join(300)
            if failure:
                self.note(f"the script failed: {type(failure[0]).__name__}: {failure[0]}"[:300])
        finally:
            self.stop()
            if sup is not None:
                sup.shutdown()
            for proxy in self.mainframe:
                proxy.stop()
                proxy.join(10)
            for p in started_procs:
                _end_process_tree(p)
            for ch, j in self.capture.items():
                j.close(clean=ch not in self.failed)
            if self.pause_flag is not None:
                self.pause_flag.stop()
                (self.folder / pause.FILE).unlink(missing_ok=True)
            meta = self._meta("stopped")
            if self.channel_status is not None:
                try:
                    meta["channel_status"] = self.channel_status()
                except Exception as exc:  # noqa: BLE001 - the recording is kept whatever it says
                    self.note(f"the collectors' status could not be read: {type(exc).__name__}")
                    meta["notes"] = self.notes
            _write_json(self.folder / "session.json", meta)
            with self._lock:
                self._journal.close()
                self._journal = None
            timeline.build(self.folder)
            if self.robot_log:
                self._robot_log()
        return self.folder

    def _robot_log(self) -> None:
        from twinlab2_recorder.workstation import robot

        try:
            cov = robot.attach(self.folder)
        except Exception as exc:  # noqa: BLE001 - the recording is kept whatever the log holds
            self.note(f"the robot's log could not be read: {type(exc).__name__}")
            cov = None
        if cov is None:
            return
        gaps = ", ".join(
            f"{g['activity']} {g.get('display') or ''}".strip() for g in cov["gaps"][:10]
        )
        self.note(f"robot log: {cov['activities']} activities, {cov['covered']} of "
                  f"{cov['ui_activities']} UI activities left a recorded step"
                  + (f"; not seen: {gaps}" if gaps else ""))  # fmt: skip
        meta = json.loads((self.folder / "session.json").read_text(encoding="utf-8"))
        meta["notes"] = self.notes
        _write_json(self.folder / "session.json", meta)
        timeline.build(self.folder)

    def _start_mainframes(self) -> None:
        """One TN3270 proxy per mainframe target, on its `listen` port (23270, 23271... when not
        given; a free port when that one is taken)."""
        from twinlab2_recorder.workstation import tn3270

        for i, t in enumerate(self.targets.mainframes()):
            host, port = t.tn3270.rsplit(":", 1)
            wanted = t.listen if t.listen is not None else 23270 + i
            journal = self.capture.get("mainframe")
            try:
                proxy = tn3270.Proxy(host, int(port), self.folder / "mainframe", listen=wanted,
                                     tls=t.tls, app_key=t.key, stop=self.stopping,
                                     journal=journal, paused=self.paused)  # fmt: skip
            except OSError:
                proxy = tn3270.Proxy(host, int(port), self.folder / "mainframe", listen=0,
                                     tls=t.tls, app_key=t.key, stop=self.stopping,
                                     journal=journal, paused=self.paused)  # fmt: skip
                self.note(f"port {wanted} was taken: the terminal connects to {proxy.address}")
            proxy.start()
            self._channel_open("mainframe")
            self.mainframe.append(proxy)
            facts = {"key": t.key, "surface": "mainframe", "level": lv.FULL,
                     "display": t.name or host}  # fmt: skip
            if t.app_id is not None:
                facts["app_id"] = t.app_id
            self.apps[t.key] = facts
            self.journal({"kind": "app", **facts})
            self.journal({"kind": "mainframe", "app": t.key, "listen": proxy.address,
                          "host": t.tn3270, "tls": t.tls})  # fmt: skip

    def _run_desktop(self) -> None:
        from twinlab2_recorder.desktop.recorder import Recorder

        rec = Recorder(self.folder / "desktop", set(), session=True, stop=self.stopping,
                       max_seconds=self.max_seconds, pictures=self.pictures, admit=self.admit,
                       unfocused_values=self.robot, journal=self.capture.get("desktop"),
                       **self.desktop_exclusions())  # fmt: skip
        for pid, level in self._pending:
            rec.add_process(pid, level)
        self._pending = []
        self.desktop = rec
        self._channel_open("desktop")
        try:
            rec.run()
        except KeyboardInterrupt:
            raise
        except Exception:
            self.failed.add("desktop")
            raise

    def desktop_exclusions(self) -> dict:
        """The desktop recorder's hooks into the exclude list (none without one)."""
        pause = {"paused": self.paused}
        if not self.targets.exclude:
            return pause
        return {"exclude_window": self.excluded_window, "exclusion_masks": self.exclusion_masks, **pause}

    def _script(self, script, failure: list) -> None:
        try:
            import comtypes

            comtypes.CoInitializeEx(0)
        except Exception:  # noqa: BLE001 - not Windows, or COM already set up
            pass
        try:
            time.sleep(1.0)  # the recorders have their listeners in place
            script(self)
            time.sleep(1.5)  # the last step settles
        except BaseException as exc:  # noqa: BLE001 - reported in the session's notes
            failure.append(exc)
        finally:
            self.stop()


def _policy_facts(policy) -> dict:
    return {"entries": len(policy.rules) if policy else 0, "managed": bool(policy and policy.managed)}


def remove_profile(profile: Path, seconds: float = 60) -> bool:
    """Delete the session browser's profile (its cookies and cache). Windows keeps a deleted
    file pending while a scanner or the font cache still has it open, extensions install
    read-only files, and a profile's paths can be longer than MAX_PATH (the browser handles them,
    Python's plain paths do not): so the deletion uses the long-path form, clears read-only bits
    and is tried again for a while."""
    import stat

    def writable(func, path, _exc):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except OSError:
            pass

    target = str(Path(profile).resolve())
    if sys.platform == "win32" and not target.startswith("\\\\?\\"):
        target = "\\\\?\\" + target
    end = time.monotonic() + seconds
    while profile.exists():
        shutil.rmtree(target, onexc=writable)
        if not profile.exists() or time.monotonic() > end:
            break
        time.sleep(1.0)
    return not profile.exists()


def _title(win, hwnd: int) -> str:
    """A window's title without sending it a message (a hung window cannot block the watcher)."""
    import ctypes

    buf = ctypes.create_unicode_buffer(512)
    try:
        win.user32.InternalGetWindowText(hwnd, buf, 512)
    except (AttributeError, OSError, ctypes.ArgumentError):
        return ""
    return buf.value


def _rect(win, hwnd: int) -> tuple[int, int, int, int] | None:
    import ctypes
    from ctypes import wintypes

    r = wintypes.RECT()
    if not hwnd or not win.user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    return (r.left, r.top, r.right - r.left, r.bottom - r.top)


def _above(win, hwnd: int) -> list[int]:
    """The visible top-level windows above `hwnd` in Z order (EnumWindows goes top down)."""
    out: list[int] = []
    found: list[bool] = []

    def cb(h, _):
        if int(h or 0) == int(hwnd):
            found.append(True)
            return False
        if win.user32.IsWindowVisible(h) and not win.user32.IsIconic(h):
            out.append(int(h))
        return True

    win.user32.EnumWindows(win.WNDENUMPROC(cb), 0)
    return out if found else []


def _embedded(win, hwnd: int, own: int) -> list[int]:
    """Visible child windows of `hwnd` that belong to another process (an embedded program)."""
    out: list[int] = []

    def cb(h, _):
        if win.user32.IsWindowVisible(h) and win.window_pid(h) not in (own, 0):
            out.append(int(h))
        return True

    win.user32.EnumChildWindows(hwnd, win.WNDENUMPROC(cb), 0)
    return out


def _window_pids(win) -> set[int]:
    pids: set[int] = set()

    def cb(hwnd, _):
        if win.user32.IsWindowVisible(hwnd):
            pids.add(win.app_pid(hwnd))
        return True

    win.user32.EnumWindows(win.WNDENUMPROC(cb), 0)
    return pids


HEADLESS_ENV = "TWINLAB_BROWSER_HEADLESS"
WINDOW = (1280, 720)  # the headless window's starting size; its pages are then fitted


def browser_headless() -> bool:
    """Does the session start its browser headless (`TWINLAB_BROWSER_HEADLESS=1`, set by
    `record --headless`)? The variable reaches the web collector's own process."""
    return os.environ.get(HEADLESS_ENV, "").strip().lower() in ("1", "true", "yes")


def _wait_cdp(cdp: str, timeout: float, stopping: Callable[[], bool]) -> bool:
    import urllib.request

    end = time.monotonic() + timeout
    while time.monotonic() < end and not stopping():
        try:
            with urllib.request.urlopen(cdp + "/json/version", timeout=2) as r:
                if r.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(0.3)
    return False


def _close_browser(cdp: str, proc: subprocess.Popen, profile: Path) -> None:
    """Close the session's browser so that it lets go of its profile: its tabs through the
    DevTools port first (the browser exits when the last one closes), then its process tree,
    then any process still started with this profile (a browser hands work to helpers that are
    not its children)."""
    import urllib.request

    try:
        with urllib.request.urlopen(cdp + "/json/list", timeout=3) as r:
            targets = json.loads(r.read().decode("utf-8"))
        for t in targets:
            if t.get("type") == "page" and t.get("id"):
                try:
                    req = urllib.request.Request(f"{cdp}/json/close/{t['id']}", method="PUT")
                    urllib.request.urlopen(req, timeout=3).close()
                except OSError:
                    pass
        proc.wait(10)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    _end_process_tree(proc)
    for _ in range(3):
        pids = _profile_pids(profile)
        if not pids:
            return
        for pid in pids:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
        time.sleep(1.0)


def _alive(pid: int) -> bool:
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes

    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000, False, pid)
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        k.GetExitCodeProcess(h, ctypes.byref(code))
        return code.value == 259
    finally:
        k.CloseHandle(h)


def _elevated(pid: int) -> bool | None:
    """Does the process run as administrator (an elevated token)? None when it cannot be told."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    k, a = ctypes.windll.kernel32, ctypes.windll.advapi32
    h = k.OpenProcess(0x1000, False, pid)
    if not h:
        return None
    token = wintypes.HANDLE()
    try:
        if not a.OpenProcessToken(h, 0x0008, ctypes.byref(token)):
            return True  # a process whose token this one may not read is above it
        try:
            value, size = wintypes.DWORD(), wintypes.DWORD()
            if not a.GetTokenInformation(token, 20, ctypes.byref(value), 4, ctypes.byref(size)):
                return None
            return bool(value.value)
        finally:
            k.CloseHandle(token)
    finally:
        k.CloseHandle(h)


def _apartment() -> int | None:
    """COM apartment of this thread: 0 single-threaded, 1 multithreaded, 3 main single-threaded."""
    if sys.platform != "win32":
        return None
    import ctypes

    kind, qualifier = ctypes.c_int(), ctypes.c_int()
    if ctypes.windll.ole32.CoGetApartmentType(ctypes.byref(kind), ctypes.byref(qualifier)) != 0:
        return None
    return kind.value


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _port_pids(cdp: str) -> list[int]:
    """The browser listening on a DevTools address, and its other processes (Windows)."""
    from urllib.parse import urlsplit

    if sys.platform != "win32":
        return []
    port = urlsplit(cdp).port
    script = (f"$p = (Get-NetTCPConnection -LocalPort {int(port or 0)} -State Listen "
              "-ErrorAction SilentlyContinue | Select-Object -First 1).OwningProcess; "
              "if ($p) { $p; Get-CimInstance Win32_Process -Filter \"ParentProcessId=$p\" | "
              "ForEach-Object { $_.ProcessId } }")  # fmt: skip
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                             capture_output=True, text=True, timeout=60)  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(x) for x in out.stdout.split() if x.strip().isdigit()]


def open_profile(profile: Path, browser: str | None = None, url: str = "about:blank") -> int:
    """Open the kept browser profile without recording (to install an extension, sign in once).
    Returns the browser's process id."""
    exe = find_browser(browser)
    if exe is None:
        raise RuntimeError("no Edge or Chrome on this machine")
    Path(profile).mkdir(parents=True, exist_ok=True)
    return subprocess.Popen([exe, f"--user-data-dir={profile}", "--no-first-run",
                             "--no-default-browser-check", "--edge-skip-compat-layer-relaunch",
                             url]).pid  # fmt: skip


def _profile_pids(profile: Path) -> list[int]:
    """Processes whose command line names this browser profile folder (Windows)."""
    if sys.platform != "win32":
        return []
    needle = str(profile).replace("'", "''")
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' or Name='chrome.exe'\" "
        f"| Where-Object {{ $_.CommandLine -and $_.CommandLine.Contains('{needle}') }} "
        "| ForEach-Object { $_.ProcessId }"
    )
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                             capture_output=True, text=True, timeout=60)  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(x) for x in out.stdout.split() if x.strip().isdigit()]


def _end_process_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()


def recover(folder: Path) -> bool:
    """Finalise a session folder left `recording`. True when it was finalised now."""
    folder = Path(folder)
    meta_path = folder / "session.json"
    if not meta_path.is_file():
        return False
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if meta.get("state") != "recording":
        return False
    from twinlab2_recorder.workstation import recovery

    rows = timeline.read_jsonl(folder / "journal.jsonl")
    done = recovery.rebuild(folder)
    meta["state"] = "recovered"
    meta["recovered"] = True
    times = [r.get("ts") for r in rows if r.get("ts")]
    for ch in capture.CHANNELS:
        times += [r.get("at") or r.get("captured_at") for r in capture.read(
            capture.path_of(folder, ch))]  # fmt: skip
    times = [t for t in times if t]
    meta["ended"] = max(times, key=timeline._t) if times else meta.get("started")
    notes = list(meta.get("notes") or [])
    if (folder / capture.DIR).is_dir():
        events = sum(d.get("events", 0) for d in done.values())
        unfinished = sum(d.get("incomplete", 0) for d in done.values())
        notes.append(f"recovered after the recorder stopped unexpectedly: {events} event(s) "
                     "rebuilt from the capture journal"
                     + (f", {unfinished} of them without their detail" if unfinished else "")
                     + f"; events in the last {capture.LOSS_WINDOW_S:g} s may be lost")  # fmt: skip
        net = done.get("network") or {}
        if net.get("rebuilt"):
            notes.append(f"the web HAR was rebuilt from the network journal ({net['entries']} "
                         "request(s)); requests still in flight were lost")  # fmt: skip
    else:
        notes.append("recovered after the recorder stopped unexpectedly: what was written until "
                     "then is kept; a web recording has no HAR")  # fmt: skip
    meta["notes"] = notes
    pictures = folder / "desktop" / "pictures"
    if pictures.is_dir() and not (folder / "desktop" / "pictures.json").is_file():
        shutil.rmtree(pictures, ignore_errors=True)  # never checked for data: not kept
        notes.append("the desktop pictures were deleted: they had not been checked for data")
    _write_json(meta_path, meta)
    remove_profile(folder / "web-profile", seconds=10)
    timeline.build(folder)
    return True


def recover_all(root: Path) -> list[str]:
    """Finalise every unfinished session under `root`, and delete browser profiles an earlier
    session could not delete."""
    root = Path(root)
    if not root.is_dir():
        return []
    done = [p.name for p in sorted(root.iterdir()) if p.is_dir() and recover(p)]
    for left in root.glob("*/web-profile"):
        remove_profile(left, seconds=5)
    return done
