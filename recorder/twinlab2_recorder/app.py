"""The recorder's window, without a GUI toolkit: what it shows (`RecorderApp.snapshot()`) and
what it does (the `on_*`, `select_*` and `exclude_*` actions).

The page in `twinlab2_recorder/ui/` (shown by twinlab2_recorder.webwindow in WebView2) draws the
snapshot and calls the actions through a JS bridge. Everything here runs on one thread, the
**window thread** (`Loop`): the bridge hands each call to it (`Loop.call`) and nothing here
waits. Portal calls, preflight, sending, uploads and finishing a recording run as background jobs
(twinlab2_recorder.uithread) whose results come back through the loop's dispatcher.

Questions ("Send this to the portal?") are a pending `dialog` in the snapshot, answered with
`answer(id, yes, chosen)`; messages are `notices` (toasts), dismissed with `dismiss(id)`. Tests
drive all of it headlessly (tests/recorder/test_window_headless.py), and the page itself in
headless Chromium (tests/recorder/test_window_page.py).
"""

from __future__ import annotations

from collections.abc import Callable
import functools
import heapq
import itertools
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import threading
import time

from twinlab2_recorder import settings as st
from twinlab2_recorder import ui_model as ui
from twinlab2_recorder.client import Portal, PortalError, check_portal
from twinlab2_recorder.uithread import (
    Dispatcher,
    Job,
    Jobs,
    clear_ui_thread,
    mark_ui_thread,
    on_ui_thread,
    wait_process,
)
from twinlab2_recorder.upload_queue import UploadQueue

STOP_WAIT_S = 120  # how long closing the window waits for a recording to finish
POLL_S = (15, 20)  # the portal is asked for sessions every 15 to 20 seconds
PAGES = ("record", "recordings", "uploads", "privacy", "settings", "pair")
EXCLUDE_KINDS = ["Program", "Window title", "Web site"]
EXCLUDE_KIND_OF = {"Program": "process", "Window title": "title", "Web site": "web"}
EXCLUDE_HINTS = {
    "Program": "A program's file name, such as WINWORD.EXE or EXCEL.EXE (* matches anything: adobe*.exe).",
    "Window title": "Words in a window's title, such as Personal; or a whole title with *, such as * - Excel.",
    "Web site": "A host, such as mail.google.com (its subdomains too), or an address prefix, such as "
    "https://intranet.example.com/hr/.",
}
CHANNEL_WORDS = {"starting": ("Starting", "active"), "ok": ("Recording", "ok"), "restarting": ("Restarting", "warn"),
                 "failed": ("Failed", "bad"), "ended": ("Ended", "off"), "ended early": ("Ended early", "bad"),
                 "cut off": ("Cut off", "bad")}  # fmt: skip
STATE_CHIPS = {"stopped": ("Finished", "ok"), "recovered": ("Recovered after a crash", "warn"),
               "recording": ("Unfinished", "warn")}  # fmt: skip


class Loop:
    """The window thread: callables posted from any thread (`dispatcher.post`) and timers
    (`after`), run one at a time. `run_forever()` on a thread of its own; tests may call `step()`."""

    def __init__(self) -> None:
        self.dispatcher = Dispatcher()
        self._timers: list = []
        self._seq = itertools.count()
        self._lock = threading.Lock()
        self.stopped = threading.Event()
        self.thread: threading.Thread | None = None

    def after(self, ms: float, fn: Callable, *args) -> None:
        with self._lock:
            heapq.heappush(
                self._timers, (time.monotonic() + ms / 1000, next(self._seq), fn, args)
            )

    def step(self) -> None:
        self.dispatcher.drain()
        now = time.monotonic()
        while True:
            with self._lock:
                if not self._timers or self._timers[0][0] > now:
                    return
                _, _, fn, args = heapq.heappop(self._timers)
            fn(*args)

    def run_forever(self) -> None:
        mark_ui_thread()
        try:
            while not self.stopped.is_set():
                try:
                    self.step()
                except Exception as exc:  # noqa: BLE001 - one failed callback must not end the window
                    print(f"window: {type(exc).__name__}: {exc}", file=sys.stderr)
                self.stopped.wait(0.03)
        finally:
            clear_ui_thread()

    def start(self) -> Loop:
        self.thread = threading.Thread(
            target=self.run_forever, name="tl-window", daemon=True
        )
        self.thread.start()
        return self

    def stop(self) -> None:
        self.stopped.set()

    def call(self, fn: Callable, *args, timeout: float = 30):
        """Run `fn(*args)` on the window thread and return its result (or raise its error)."""
        if on_ui_thread():
            return fn(*args)
        done, box = threading.Event(), {}

        def run():
            try:
                box["result"] = fn(*args)
            except BaseException as exc:  # noqa: BLE001 - re-raised in the caller
                box["error"] = exc
            done.set()

        self.dispatcher.post(run)
        if not done.wait(timeout):
            raise TimeoutError("the window did not answer")
        if "error" in box:
            raise box["error"]
        return box.get("result")


def surface_of(t: dict) -> str:
    if t.get("tn3270"):
        return "mainframe"
    if t.get("origin") or str(t.get("key") or "").startswith("web:"):
        return "web"
    if t.get("exe") or str(t.get("key") or "").startswith("desktop:"):
        return "desktop"
    return str(t.get("surface") or "desktop")


class RecorderApp:
    """The window's state and actions. `host` gives the window's own powers: `close()` (the
    window goes) and, optionally, `choose_folder(title)` (used by the bridge, not here)."""

    def __init__(self, cfg: st.Settings, ca_bundle: str | None, loop: Loop, host=None,
                 make_portal: Callable | None = None) -> None:  # fmt: skip
        from twinlab2_recorder import main as m

        self.m = m
        self.cfg, self.ca_bundle, self.loop, self.host = cfg, ca_bundle, loop, host
        self.make_portal = make_portal or functools.partial(m._portal, cfg, ca_bundle)
        self.state: dict = {"proc": None, "session": None, "deny": [], "polling": False, "closing": False,
                            "recording": None, "watcher": None, "details": False, "box": None,
                            "stop_asked": None, "done": [], "exclude_rows": []}  # fmt: skip
        self.page = "record"
        self.record_tab = "offered"
        self.offered: list[dict] = []
        self.offered_selected: int | None = None
        self.apps: list[dict] = []
        self.catalog_known = False
        self.recording_selected: str | None = None
        self.status = ""
        self.portal_error: str | None = None
        self.activity = ""
        self.activity_state: str | None = None
        self.note: dict | None = (
            None  # the Record page's note after a recording / failed checks
        )
        self.dialog: dict | None = None
        self.notices: list[dict] = []
        self.upload_rows: list[dict] = []
        self.upload_note = (
            "Uploads continue in the background and resume after a restart."
        )
        self.upload_error: str | None = None
        self.own_fill = {"seq": 0, "extra": ""}
        self.ex_kind = EXCLUDE_KINDS[0]
        self.ex_note = ""
        self.settings = {"browser": cfg.values.get("browser") or "edge",
                         "keep_browser_profile": bool(cfg.values.get("keep_browser_profile")),
                         "cdp": cfg.values.get("cdp") or "", "pictures": bool(cfg.values.get("pictures"))}  # fmt: skip
        self._ids = itertools.count(1)
        self.version = 0
        self.jobs = Jobs(loop.dispatcher, self._show_job)
        self.upload_worker = ui.UploadWorker(lambda on_change: UploadQueue(cfg.folder, on_change=on_change),
                                             lambda: self.make_portal(), loop.dispatcher.post,
                                             self.show_uploads)  # fmt: skip

    def start(self) -> RecorderApp:
        """Fill the window and start its background work (polling, uploads)."""
        self.refresh_exclude()
        self.refresh_status()
        self.refresh_done()
        self.upload_worker.start()
        self.loop.after(200, self._poll_loop)
        return self

    # -- what the page shows ---------------------------------------------------------------------

    def _changed(self) -> None:
        self.version += 1

    def paired(self) -> bool:
        return bool(self.cfg.values.get("portal") and self.cfg.values.get("token"))

    def idle(self) -> bool:
        return (
            self.state["proc"] is None
            and not self.state.get("checking")
            and not self.state["closing"]
        )

    def snapshot(self) -> dict:
        s, now = self.state, time.monotonic()
        rec = s["recording"]
        mode = ("stopping" if s.get("stop_asked") is not None else "recording") if rec else \
            "checking" if s.get("checking") else "idle"  # fmt: skip
        selected = self.selected_done()
        has_box = selected is not None and s.get("box") is not None
        busy_send = bool(s.get("sending")) or s["closing"]
        return {
            "version": self.version,
            "app": {"name": "TwinLab Recorder", "version": st.VERSION},
            "page": self.page,
            "pairing": {
                "paired": self.paired(),
                "machine": self.cfg.values.get("machine"),
                "portal": self.cfg.values.get("portal"),
                "status": self.status,
                "error": self.portal_error,
                "busy": bool(s.get("pairing")),
            },  # fmt: skip
            "activity": {"text": self.activity, "state": self.activity_state},
            "recording": self._recording_view(rec, mode, now),
            "note": self.note,
            "record_tab": self.record_tab,
            "offered": {
                "note": self.offered_note(),
                "selected": self.offered_selected,
                "sessions": [
                    {
                        "id": o["id"],
                        "name": o.get("name") or "Session",
                        "label": ui.session_label(o),
                        "targets": [
                            {"name": ui.target_name(t), "surface": surface_of(t)}
                            for t in o.get("targets") or []
                        ],
                    }
                    for o in self.offered
                ],
            },  # fmt: skip
            "own": {
                "apps": [
                    {
                        "app_id": a["app_id"],
                        "name": a.get("name"),
                        "surface": surface_of(a.get("target") or {}),
                    }
                    for a in self.apps
                ],
                "empty": None
                if self.apps
                else (
                    "The portal's catalog has no application yet."
                    if self.catalog_known
                    else "None yet: the catalog appears here once this PC is "
                    "paired with the portal."
                ),
                "fill": self.own_fill,
            },  # fmt: skip
            "recordings": {
                "note": self.done_note(),
                "selected": self.recording_selected,
                "rows": [self._recording_row(r) for r in s["done"]],
                "review": self._review(selected),
                "details": s["details"],
                "busy": bool(s.get("sending")),
            },  # fmt: skip
            "uploads": {
                "rows": self.upload_rows,
                "note": self.upload_note,
                "error": self.upload_error,
            },
            "exclude": self._exclude_view(),
            "settings": dict(self.settings),
            "can": {
                "start_offered": self.idle() and self.offered_selected is not None,
                "start_own": self.idle(),
                "stop": bool(rec) and s.get("stop_asked") is None,
                "send": has_box and self.paired() and not busy_send,
                "details": has_box,
                "upload": selected is not None and self.paired() and not s["closing"],
                "pair": not s.get("pairing") and not s["closing"],
                "settings": self.idle(),
            },  # fmt: skip
            "dialog": self.dialog,
            "notices": self.notices,
        }

    def _recording_view(self, rec, mode: str, now: float) -> dict:
        s = self.state
        out = {"mode": mode, "name": None, "elapsed": None, "health": None, "apps": [], "lines": [],
               "browser_ready": False, "stop_waited": None, "stop_limit": STOP_WAIT_S if s["closing"] else None}  # fmt: skip
        if rec is None:
            return out
        out.update(name=rec.name, elapsed=ui.elapsed(now - rec.started), health=rec.health(),
                   browser_ready=rec.browser_ready, lines=list(s.get("extra_lines") or []))  # fmt: skip
        if s.get("stop_asked") is not None:
            out["stop_waited"] = int(now - s["stop_asked"])
        for t in s.get("targets") or []:
            surface = surface_of(t)
            ch = rec.channels.get(surface)
            if self.m._target_excluded(self.cfg, t):
                label, tone = "Excluded on this PC", "off"
            elif ch is None:
                label, tone = "Starting", "active"
            else:
                label, tone = CHANNEL_WORDS.get(ch["state"], (ch["state"], "off"))
                extra = []
                if ch["restarts"]:
                    extra.append(f"restarted {ch['restarts']}×")
                if ch["gaps"]:
                    extra.append(f"{ch['gaps']} gap{'s' if ch['gaps'] > 1 else ''}")
                if extra:
                    label += " · " + " · ".join(extra)
                    tone = "warn" if tone == "ok" else tone
                if ch.get("reason") and ch["state"] in (
                    "failed",
                    "ended early",
                    "cut off",
                ):
                    label += f": {ch['reason']}"
            out["apps"].append({"name": ui.target_name(t) or surface, "surface": surface,
                                "channel": surface, "status": label, "tone": tone})  # fmt: skip
        return out

    def _recording_row(self, r: dict) -> dict:
        label, tone = STATE_CHIPS.get(
            str(r.get("state") or ""), (str(r.get("state") or "unknown"), "off")
        )
        return {"id": r["id"], "name": r.get("name") or r["id"], "state": label, "tone": tone,
                "sent": bool(r.get("sent")), "row": ui.recording_row(r)}  # fmt: skip

    def _review(self, selected: dict | None) -> dict | None:
        if selected is None:
            return None
        box = self.state.get("box")
        if box is None:
            return {
                "error": self.state.get("box_error") or "",
                "summary": None,
                "json": None,
            }
        summary = ui.review_summary(box, self.m.session_notes(self.cfg, selected["id"]),
                                    self.cfg.values.get("portal"), self.cfg.values.get("machine"))  # fmt: skip
        return {"error": None, "summary": summary,
                "json": json.dumps(box, indent=1) if self.state["details"] else None}  # fmt: skip

    def _exclude_view(self) -> dict:
        rows = self.state["exclude_rows"]
        can_edit = self.state.get("exclude_can_edit", True)
        return {"rows": [{"index": i, "kind": r["kind"], "pattern": r["pattern"], "name": r["name"],
                          "source": r["source"], "locked": bool(r["locked"])} for i, r in enumerate(rows)],
                "note": self.ex_note, "can_edit": can_edit, "kinds": EXCLUDE_KINDS, "kind": self.ex_kind,
                "hint": EXCLUDE_HINTS[self.ex_kind]}  # fmt: skip

    def offered_note(self) -> str:
        if not self.paired():
            return "Pair with the portal to see the sessions it offers this PC. You can still record your own session."
        if not self.offered:
            return "The portal offers no session to this PC right now. It is asked again every 15 to 20 seconds."
        return "Choose a session, then press Start. Nothing is recorded until you do."

    def done_note(self) -> str:
        if not self.paired():
            return "Not paired: you can review recordings here; pair with the portal to send them."
        if not self.state["done"]:
            return "No recordings on this PC yet."
        return "Choose a recording to see what sending it would give the portal."

    # -- dialogs and notices ----------------------------------------------------------------------

    def ask(self, title: str, text: str, on_yes: Callable, *, yes: str = "OK", no: str = "Cancel",
            tone: str = "info", items: list[str] | None = None, on_no: Callable | None = None,
            lines: list[str] | None = None) -> dict:  # fmt: skip
        """A question in the page; `on_yes(chosen)` with the chosen items when `items` are given."""
        self.dialog = {"id": next(self._ids), "title": title, "text": text, "yes": yes, "no": no, "tone": tone,
                       "items": items, "lines": lines or []}  # fmt: skip
        self._answers = (on_yes, on_no)
        self._changed()
        return self.dialog

    def answer(
        self, dialog_id: int, yes: bool, chosen: list[str] | None = None
    ) -> None:
        if not self.dialog or self.dialog["id"] != dialog_id:
            return
        items = self.dialog.get("items")
        on_yes, on_no = self._answers
        self.dialog, self._answers = None, (None, None)
        self._changed()
        if yes:
            if items is not None:
                on_yes([c for c in chosen or [] if c in items])
            else:
                on_yes()
        elif on_no:
            on_no()

    def tell(self, title: str, text: str, tone: str = "info") -> dict:
        notice = {"id": next(self._ids), "title": title, "text": text, "tone": tone}
        self.notices = (self.notices + [notice])[-4:]
        self._changed()
        return notice

    def dismiss(self, notice_id: int) -> None:
        self.notices = [n for n in self.notices if n["id"] != notice_id]
        self._changed()

    # -- navigation --------------------------------------------------------------------------------

    def nav(self, page: str) -> None:
        if page in PAGES:
            self.page = page
            self._changed()

    def set_record_tab(self, tab: str) -> None:
        if tab in ("offered", "own"):
            self.record_tab = tab
            self._changed()

    # -- jobs, status --------------------------------------------------------------------------------

    def _show_job(self, job: Job) -> None:
        if job.kind == "poll" and job.state != "retrying":
            return  # polls show through the pairing status
        self.activity, self.activity_state = job.describe(), job.state
        self._changed()

    def refresh_status(self) -> None:
        if self.paired():
            self.status = f"Paired as {self.cfg.values.get('machine')} with {self.cfg.values.get('portal')}"
        else:
            self.status = "Not paired: pair with the portal to receive sessions and send what you record."
        self._changed()

    def refresh_done(self) -> None:
        self.state["done"] = self.m.sessions_list(self.cfg)
        if self.recording_selected and not any(
            r["id"] == self.recording_selected for r in self.state["done"]
        ):
            self.recording_selected, self.state["box"] = None, None
        self._changed()

    def selected_done(self) -> dict | None:
        return next(
            (r for r in self.state["done"] if r["id"] == self.recording_selected), None
        )

    def select_recording(self, session_id: str | None) -> None:
        self.recording_selected = session_id
        s = self.selected_done()
        self.state["box"], self.state["box_error"] = None, None
        if s is not None:
            try:
                self.state["box"] = self.m.outbox(self.cfg, s["id"])
            except SystemExit as exc:
                self.state["box_error"] = str(exc)
        else:
            self.recording_selected = None
        self._changed()

    def on_details(self) -> None:
        self.state["details"] = not self.state["details"]
        self._changed()

    # -- polling the portal ----------------------------------------------------------------------

    def _poll_loop(self) -> None:
        if self.state.get("destroyed"):
            return
        self.loop.after(int(random.uniform(*POLL_S) * 1000), self._poll_loop)
        self.poll_portal()

    def poll_portal(self) -> None:
        if (
            self.state["polling"]
            or not self.cfg.values.get("token")
            or self.state.get("destroyed")
        ):
            return
        self.state["polling"] = True

        def work(_report):
            p = self.make_portal()
            return p.sessions(), p.apps()

        # The error arrives as a bound argument (the reason string), never as a closure over the
        # handler's exception name, which Python deletes when the handler ends.
        self.jobs.run("poll", work, label="the portal", on_done=lambda got: self.apply(got[0], got[1], None),
                      on_failed=lambda reason: self.apply(None, None, reason))  # fmt: skip

    def apply(self, offered, apps, error) -> None:
        self.state["polling"] = False
        if error:
            if self.cfg.values.get("portal"):
                self.status = f"Paired as {self.cfg.values.get('machine')}: {error}"
                self.portal_error = error
            self._changed()
            return
        self.portal_error = None
        self.refresh_status()
        self.state["deny"] = offered["deny_keys"]
        self.offered = list(offered["sessions"])
        if not any(o["id"] == self.offered_selected for o in self.offered):
            self.offered_selected = None
        self.apps = list(apps["apps"])
        self.catalog_known = True
        self._changed()

    def select_offered(self, session_id: int | None) -> None:
        self.offered_selected = (
            session_id if any(o["id"] == session_id for o in self.offered) else None
        )
        self._changed()

    # -- settings --------------------------------------------------------------------------------

    def set_settings(self, values: dict) -> None:
        for key in ("browser", "keep_browser_profile", "cdp", "pictures"):
            if key in values:
                v = values[key]
                self.settings[key] = (
                    bool(v)
                    if key in ("keep_browser_profile", "pictures")
                    else str(v or "").strip()
                )
        if self.settings["browser"] not in ("edge", "chrome"):
            self.settings["browser"] = "edge"
        self.cfg.values.update(self.settings)
        self.cfg.save()
        self._changed()

    def options(self) -> dict:
        cdp = self.settings["cdp"].strip()
        if cdp and not cdp.startswith(("http://127.0.0.1:", "http://localhost:")):
            raise ValueError(
                "attach only to a browser on this PC, for example http://127.0.0.1:9222"
            )
        self.cfg.values.update(keep_browser_profile=self.settings["keep_browser_profile"], cdp=cdp,
                               browser=self.settings["browser"], pictures=self.settings["pictures"])  # fmt: skip
        self.cfg.save()
        return {"keep_browser_profile": self.settings["keep_browser_profile"], "cdp": cdp or None,
                "browser": self.settings["browser"], "pictures": self.settings["pictures"],
                "exclude": self.cfg.exclude_policy().as_session()}  # fmt: skip

    # -- recording -----------------------------------------------------------------------------------

    def begin(self, conf: dict) -> None:
        """Preflight first (in a worker thread: a check may wait a few seconds), then record."""
        if not self.idle():
            return
        try:
            conf.update(self.options())
        except ValueError as exc:
            self.tell("Not started", str(exc), "bad")
            return
        self.state["checking"] = True
        self.note = None
        self._changed()

        def work():
            try:
                block, error = self.m.preflight_checks(self.cfg, conf), None
            except Exception as exc:  # noqa: BLE001 - shown to the person
                block, error = None, f"{type(exc).__name__}: {exc}"
            self.loop.dispatcher.post(self.checked, conf, block, error)

        threading.Thread(target=work, name="tl-preflight", daemon=True).start()

    def checked(self, conf: dict, block: dict | None, error: str | None) -> None:
        self.state["checking"] = False
        self._changed()
        if self.state["closing"]:
            return
        if error:
            self.note = {
                "tone": "bad",
                "title": "Not recording: the checks could not run",
                "text": error,
                "lines": [],
            }
            return
        conf["preflight"] = block
        if block["ready"]:
            self.launch(conf)
            return
        failed = self.m.failed_checks(block)
        self.note = {
            "tone": "warn",
            "title": "Not ready",
            "text": "These checks failed:",
            "lines": failed,
        }

        def anyway():
            conf["override_preflight"] = True
            self.note = None
            self.launch(conf)

        def fix():
            self.note = {"tone": "warn", "title": "Not recording: fix the failed checks, then press Start again",
                         "text": "", "lines": failed}  # fmt: skip
            self._changed()

        self.ask("Not ready to record", "These checks failed. Record anyway? The recording will say that it "
                 "started with these checks failed. Choose Cancel to fix them first, then press Start again.",
                 anyway, yes="Record anyway", tone="warn", lines=failed, on_no=fix)  # fmt: skip

    def launch(self, conf: dict) -> None:
        cfg, s = self.cfg, self.state
        cfg_path = cfg.folder / "next-session.json"
        cfg_path.write_text(json.dumps(conf), encoding="utf-8")
        main_py = Path(self.m.__file__).resolve()
        cmd = [sys.executable] + (
            [] if getattr(sys, "frozen", False) else [str(main_py)]
        )
        cmd += ["run-session", "--config", str(cfg_path), "--parent", str(os.getpid())]
        cmd += ["--portable-data"] if cfg.portable else []
        (cfg.folder / "logs").mkdir(exist_ok=True)
        s["log"] = cfg.folder / "logs" / f"{conf['id']}.log"
        log = s["log"].open("w", encoding="utf-8")
        s["proc"] = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))  # fmt: skip
        log.close()
        self.watch_recording(conf)

    def watch_recording(self, conf: dict) -> None:
        """Show the recording `conf` that runs in state["proc"]: indicator, journal, end."""
        s = self.state
        s["session"] = conf["id"]
        s["targets"] = list(conf.get("targets") or [])
        rec = ui.RecordingStatus(
            conf.get("name") or "Session", conf.get("targets") or [], time.monotonic()
        )
        s["recording"] = rec
        pre = conf.get("preflight") or {"ready": True}
        s["extra_lines"] = ([] if pre.get("ready") else ["Recording anyway, with failed checks."]) + \
            self.m.mainframe_addresses(conf)  # fmt: skip
        s["watcher"] = ui.JournalWatcher(self.cfg.recordings / conf["id"] / "journal.jsonl", self.loop.dispatcher.post,
                                         self._journal_rows).start()  # fmt: skip
        self.page = "record"
        self._changed()
        self._tick()
        self.loop.after(1000, self._watch)

    def _journal_rows(self, rows: list[dict]) -> None:
        rec = self.state["recording"]
        if rec is not None:
            for r in rows:
                rec.feed(r)
            self._changed()

    def _tick(self) -> None:
        if self.state["recording"] is None or self.state.get("destroyed"):
            return
        self._changed()  # the elapsed time
        self.loop.after(1000, self._tick)

    def _watch(self) -> None:
        proc = self.state["proc"]
        if proc is None or self.state.get("destroyed"):
            return
        if proc.poll() is None:
            self.loop.after(1000, self._watch)
            return
        self.recorded(proc.returncode)

    def recorded(self, code: int) -> None:
        """The recording process ended (by itself, Stop, or the window closing)."""
        s = self.state
        if s["proc"] is None:
            return
        s["proc"] = None
        if s["watcher"]:
            s["watcher"].stop()
        s["recording"], s["stop_asked"] = None, None
        self._changed()
        if s["closing"]:
            return
        if code != 0:
            self.note = {"tone": "bad", "title": "The recording ended with an error",
                         "text": f"What was recorded until then is kept on this PC. The log is {s.get('log')}.",
                         "lines": []}  # fmt: skip
        else:
            self.note = {"tone": "ok", "title": "Recording saved on this PC",
                         "text": "Review it before you send it.", "lines": []}  # fmt: skip
        self.refresh_done()
        self.page = "recordings"
        if any(r["id"] == s["session"] for r in s["done"]):
            self.select_recording(s["session"])

    def on_stop(self) -> None:
        s = self.state
        if s["session"] and s["proc"] is not None:
            folder = self.cfg.recordings / s["session"]
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "stop").touch()
            if s["stop_asked"] is None:
                s["stop_asked"] = time.monotonic()
            if s["recording"]:
                s["recording"].stopping = True
            self._changed()

    def on_start_offered(self) -> None:
        o = next((x for x in self.offered if x["id"] == self.offered_selected), None)
        if o is None:
            self.tell("Choose a session", "Choose a session in the list first.")
            return
        self.begin(
            self.m.session_config(
                o["targets"],
                self.state["deny"],
                o["name"],
                o["id"],
                o.get("automation_id"),
            )
        )

    def on_start_own(
        self, name: str = "", app_ids: list | None = None, extra: str = ""
    ) -> None:
        chosen = set(app_ids or [])
        targets = [dict(a["target"]) for a in self.apps if a["app_id"] in chosen]
        for line in str(extra or "").splitlines():
            line = line.strip()
            if line.lower().startswith(("http://", "https://")):
                targets.append({"origin": line, "level": "full"})
            elif line.lower().endswith(".exe"):
                targets.append({"exe": line, "level": "full"})
            elif line.lower().startswith(("tn3270://", "tn3270s://")):
                targets.append({"tn3270": line})
        if not targets:
            self.tell("Nothing to record", "Tick at least one application from the catalog, or type one in the "
                      "box below it.", "warn")  # fmt: skip
            return
        self.begin(
            self.m.session_config(
                targets, self.state["deny"], str(name or "").strip() or "My session"
            )
        )

    # -- sending references ------------------------------------------------------------------------

    def on_send(self) -> None:
        s = self.selected_done()
        if s is None or self.state.get("sending"):
            return
        portal = self.cfg.values.get("portal") or "the portal"
        self.ask("Send the summary?", f"Send the summary shown here to {portal}? The recording itself stays on "
                 "this PC.", lambda: self._send(s), yes="Send summary")  # fmt: skip

    def _send(self, s: dict) -> None:
        self.state["sending"] = True
        self._changed()
        session_id = s["id"]

        def sent(result):
            self.state["sending"] = False
            self.tell(
                "Sent",
                f"Sent. It is recording {result.get('recording_id')} on the portal.",
                "ok",
            )
            self.refresh_done()

        def not_sent(reason):
            self.state["sending"] = False
            self.tell(
                "Not sent",
                f"Not sent: {reason}. The recording is still on this PC; try again later.",
                "bad",
            )

        self.jobs.run("send", lambda _r: self.m.send(self.cfg, session_id, self.ca_bundle), state="sending",
                      label=f"references of {s['name']}", retries=3, on_done=sent, on_failed=not_sent)  # fmt: skip

    # -- pairing -------------------------------------------------------------------------------------

    def on_pair(self, url: str, code: str, name: str) -> bool:
        """Pair with the portal at `url`: False (with a notice) when the form is not complete or
        the address is refused at once; the pairing itself runs in the background."""
        if self.state.get("pairing"):
            return False
        url, code, name = (
            str(url or "").strip(),
            str(code or "").strip(),
            str(name or "").strip(),
        )
        if not url or not code or not name:
            self.tell(
                "Not paired",
                "Fill in the portal address, the pairing code and a name for this PC.",
                "warn",
            )
            return False
        try:
            url = check_portal(url)
        except PortalError as exc:
            self.tell("Not paired", str(exc), "bad")
            return False
        self.state["pairing"] = True
        self._changed()

        def paired(doc):
            self.state["pairing"] = False
            self.cfg.paired(url, doc["machine_id"], doc["name"], doc["token"])
            self.portal_error = None
            self.refresh_status()
            if self.page == "pair":
                self.page = "record"
            self.tell("Paired", f"This PC is paired as {doc['name']} with {url}.", "ok")
            self.poll_portal()
            self.upload_worker.kick()

        def failed(reason):
            self.state["pairing"] = False
            self.tell("Not paired", f"Not paired: {reason}", "bad")

        self.jobs.run("pair", lambda _r: Portal(url, ca_bundle=self.ca_bundle).pair(code, name), state="connecting",
                      label=f"pairing with {url}", retries=2, on_done=paired, on_failed=failed)  # fmt: skip
        return True

    def pair_defaults(self) -> dict:
        return {"portal": self.cfg.values.get("portal") or "https://",
                "name": self.cfg.values.get("machine") or os.environ.get("COMPUTERNAME", "")}  # fmt: skip

    # -- uploads ---------------------------------------------------------------------------------------

    def show_uploads(self, found, active, error) -> None:
        now = time.time()
        names = {r["id"]: r.get("name") for r in self.state["done"]}
        rows = []
        for j in found:
            r = ui.upload_row(j, now, active)
            r["name"] = names.get(r["session"]) or r["session"]
            rows.append(r)
        self.upload_rows = rows
        busy = sum(r["state"] in ("sending", "queued", "retrying") for r in rows)
        self.upload_error = error
        self.upload_note = (f"{busy} upload(s) waiting or in progress; they continue in the background and "
                            "resume after a restart." if busy else "Nothing waiting to upload.")  # fmt: skip
        self._changed()

    def on_upload(self) -> None:
        s = self.selected_done()
        if s is None:
            return
        portal = self.cfg.values.get("portal") or "the portal"

        def go():
            self.upload_worker.add(self.cfg.recordings / s["id"], s["id"])
            self.page = "uploads"
            self._changed()

        self.ask("Upload the whole recording?", f"Upload the whole recording of {s['name']} (its folder, with "
                 f"what it captured) to {portal}? It is queued and sent in the background, and resumes after "
                 "a restart.", go, yes="Upload")  # fmt: skip

    def on_retry(self, job_id: str) -> None:
        self.upload_worker.retry(job_id)

    def on_retry_all(self) -> None:
        self.upload_worker.kick(force=True)

    # -- other buttons --------------------------------------------------------------------------------

    def on_profile(self) -> None:
        if self.state["proc"] is not None:
            self.tell(
                "Stop the recording first",
                "The kept profile can be opened when nothing is recording.",
            )
            return
        from twinlab2_recorder.workstation.session import open_profile

        try:
            open_profile(
                self.m.browser_profile(self.cfg),
                url="https://microsoftedge.microsoft.com/addons/search/uipath",
            )
        except RuntimeError as exc:
            self.tell("The browser did not open", str(exc), "bad")
            return
        self.set_settings({"keep_browser_profile": True})
        self.tell("The recorder's browser opened", "Install UiPath's extension there (or sign in to the "
                  "applications), then close that browser before you start recording.")  # fmt: skip

    def on_propose(self, folder: str | None) -> None:
        if not folder:
            return
        from twinlab2_recorder.workstation.propose import propose

        try:
            got = propose(Path(folder))
        except FileNotFoundError as exc:
            self.tell("No UiPath project there", str(exc), "bad")
            return
        lines = []
        for t in got["targets"]:
            if t.get("exe"):
                lines.append(t["exe"])
            elif t.get("origin"):
                lines.append(t["origin"])
            elif t.get("tn3270"):
                lines.append("tn3270://" + t["tn3270"])
        self.own_fill = {"seq": self.own_fill["seq"] + 1, "extra": "\n".join(lines)}
        if got["browser"]:
            self.set_settings({"browser": got["browser"]})
        self.record_tab = "own"
        self.tell("Filled in from the UiPath project", f"{len(lines)} target(s) from {got['files']} workflow "
                  "file(s). Check them before you start." + (" " + " ".join(got["notes"]) if got["notes"] else ""))  # fmt: skip

    # -- the exclude list -------------------------------------------------------------------------------

    def refresh_exclude(self) -> None:
        from twinlab2_recorder.workstation import exclude

        pol = self.cfg.exclude_policy()
        rows = self.state["exclude_rows"] = exclude.rows(pol)
        self.state["exclude_can_edit"] = pol.allow_user_changes
        locked = sum(1 for r in rows if r["rule"].locked)
        if not pol.allow_user_changes:
            self.ex_note = (
                "Your office manages this list on this PC: it cannot be changed here."
            )
        elif locked:
            self.ex_note = (f"{locked} entr{'y is' if locked == 1 else 'ies are'} set by your office's policy and "
                            "cannot be removed here. The rest are yours to change.")  # fmt: skip
        elif not rows:
            self.ex_note = "The list is empty: every application may be recorded as a session's target or as having been used."
        else:
            self.ex_note = (
                f"{len(rows)} entries. Changes apply to the next session you start."
            )
        self._changed()

    def exclude_kind(self, kind: str) -> None:
        if kind in EXCLUDE_KINDS:
            self.ex_kind = kind
            self._changed()

    def _own_rules(self):
        return [r["rule"] for r in self.state["exclude_rows"] if not r["rule"].locked]

    def exclude_add(
        self, pattern: str | None = None, kind: str | None = None, rule=None
    ) -> bool:
        from twinlab2_recorder.workstation import exclude

        if not self.state.get("exclude_can_edit", True):
            return False
        if rule is None:
            text = str(pattern or "").strip()
            if not text:
                self.tell(
                    "Nothing to add",
                    "Type a program, a window title or a web site to exclude.",
                    "warn",
                )
                return False
            rule = exclude.clean_rule(
                {"kind": EXCLUDE_KIND_OF[kind or self.ex_kind], "pattern": text}
            )
            if rule is None:
                self.tell(
                    "Not added",
                    "That entry would exclude every application: name one.",
                    "bad",
                )
                return False
        if any(r["rule"].key() == rule.key() for r in self.state["exclude_rows"]):
            return True
        self.cfg.set_exclude_rules(self._own_rules() + [rule])
        self.refresh_exclude()
        return True

    def exclude_remove(self, indices: list[int]) -> None:
        drop = {int(i) for i in indices}
        rows = self.state["exclude_rows"]
        if not self.state.get("exclude_can_edit", True) or any(
            rows[i]["locked"] for i in drop if i < len(rows)
        ):
            return
        self.cfg.set_exclude_rules(
            [
                r["rule"]
                for i, r in enumerate(rows)
                if i not in drop and not r["rule"].locked
            ]
        )
        self.refresh_exclude()

    def exclude_defaults(self) -> None:
        from twinlab2_recorder.workstation import exclude

        if not self.state.get("exclude_can_edit", True):
            return
        self.cfg.set_exclude_rules(exclude.restore_defaults(self._own_rules()))
        self.refresh_exclude()

    def exclude_pick(self) -> None:
        from twinlab2_recorder.workstation import exclude

        names = [n for n in exclude.running_programs()
                 if not any(r["rule"].matches_process(n) for r in self.state["exclude_rows"])]  # fmt: skip
        if not names:
            self.tell(
                "Nothing to pick",
                "Every program with a window open now is already excluded, or none is open.",
            )
            return

        def chosen(items):
            for name in items:
                self.exclude_add(
                    rule=exclude.clean_rule({"kind": "process", "pattern": name})
                )

        self.ask("Exclude running apps", "Programs with a window open now. Choose the ones to exclude:", chosen,
                 yes="Exclude selected", items=names)  # fmt: skip

    # -- closing --------------------------------------------------------------------------------------

    def destroy(self) -> None:
        self.state["destroyed"] = True
        self.upload_worker.stop()
        self.jobs.cancelled.set()
        if self.state["watcher"]:
            self.state["watcher"].stop()
        self._changed()
        if self.host is not None:
            self.host.close()

    def on_close(self) -> bool:
        """The person closes the window: True when it may close now."""
        s = self.state
        if s.get("destroyed"):
            return True
        if s["closing"]:
            self.activity, self.activity_state = (f"Still finishing the recording: the window closes when it is "
                                                  f"done (at most {STOP_WAIT_S} s).", "stopping")  # fmt: skip
            self._changed()
            return False
        if s["proc"] is None:
            self.destroy()
            return True
        self.ask("A recording is running", "Stop it and close? The window stays open until the recording is "
                 "finished.", self._close_recording, yes="Stop and close", tone="warn")  # fmt: skip
        return False

    def _close_recording(self) -> None:
        s = self.state
        proc = s["proc"]
        if proc is None:
            self.destroy()
            return
        s["closing"] = True
        self.on_stop()
        self.activity, self.activity_state = (
            "Finishing the recording before the window closes…",
            "stopping",
        )
        self._changed()

        def finished(code):
            self.recorded(code if code is not None else -1)
            message = ui.close_message(code, STOP_WAIT_S)
            if message:
                self.tell("The recording did not finish", message, "warn")
                if self.host is not None and hasattr(self.host, "warn"):
                    self.host.warn(message)
            self.destroy()

        self.jobs.run("stop", lambda _r: wait_process(proc, STOP_WAIT_S), state="stopping",
                      label="finishing the recording before the window closes", on_done=finished,
                      on_failed=lambda reason: finished(None))  # fmt: skip
