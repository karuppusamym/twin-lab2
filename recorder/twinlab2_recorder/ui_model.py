"""What the recorder's window shows, without Tk: tested headlessly (tests/recorder/test_ui_model.py).

- `OfferedList`: the offered sessions in a list box that keeps the person's selection across
  polls and changes a row in place when its name or targets change under the same id;
- `app_choices`: whether the catalog's check boxes need relabelling or rebuilding;
- `UploadWorker` and `upload_row`: the upload queue (twinlab2_recorder.upload_queue) driven from one
  worker thread, with each job's progress and state, retry, and resuming after a restart;
- `review_summary`: what sending the references gives the portal, in words, before the JSON;
- `RecordingStatus`, `JournalTail` and `JournalWatcher`: the recording indicator, elapsed time,
  target applications and each channel's health, read from the session's journal off the Tk
  thread.

The Tk code in twinlab2_recorder.main only puts these on the screen.
"""

from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
import queue
import threading
import time

# --- offered sessions -------------------------------------------------------------------------------


def target_name(t: dict) -> str:
    return t.get("name") or t.get("exe") or t.get("origin") or t.get("tn3270") or t.get("key") or ""


def session_label(s: dict) -> str:
    names = ", ".join(sorted({target_name(t) for t in s.get("targets") or []} - {""}))
    return f"{s.get('name') or 'Session'}  ·  {names}" if names else str(s.get("name") or "Session")


class OfferedList:
    """The offered sessions behind a list box (anything with Tk's Listbox methods: size, get,
    insert, delete, curselection, selection_clear, selection_set, see)."""

    def __init__(self) -> None:
        self.sessions: list[dict] = []

    def selected(self, listbox) -> dict | None:
        sel = listbox.curselection()
        if not sel or sel[0] >= len(self.sessions):
            return None
        return self.sessions[sel[0]]

    def sync(self, listbox, sessions: list[dict]) -> None:
        """Show `sessions`: rows whose text changed are replaced where they are, rows that went
        are deleted, and the session that was selected stays selected (by id) if still offered."""
        chosen = self.selected(listbox)
        keep = chosen["id"] if chosen else None
        labels = [session_label(s) for s in sessions]
        for i, label in enumerate(labels):
            if i < listbox.size():
                if listbox.get(i) != label:
                    listbox.delete(i)
                    listbox.insert(i, label)
            else:
                listbox.insert("end", label)
        if listbox.size() > len(labels):
            listbox.delete(len(labels), "end")
        self.sessions = list(sessions)
        listbox.selection_clear(0, "end")
        for i, s in enumerate(sessions):
            if s["id"] == keep:
                listbox.selection_set(i)
                listbox.see(i)
                break


STATE_WORDS = {"stopped": "finished", "recovered": "recovered after a crash", "recording": "recording or unfinished"}


def recording_row(s: dict) -> str:
    """A row of "Recordings on this PC": `Month-end close  ·  finished  ·  not sent yet`."""
    state = STATE_WORDS.get(str(s.get("state") or ""), str(s.get("state") or "unknown"))
    return f"{s.get('name') or s.get('id')}  ·  {state}  ·  {'sent' if s.get('sent') else 'not sent yet'}"


def app_choices(shown: list[dict], apps: list[dict]) -> str:
    """'keep' when the catalog is the same, 'relabel' when only names or targets changed (same
    apps in the same order: the check boxes stay, their text and targets change), else
    'rebuild' (the window keeps the ticks of the apps still there)."""
    if [a["app_id"] for a in shown] != [a["app_id"] for a in apps]:
        return "rebuild"
    if [(a.get("name"), a.get("target")) for a in shown] != [(a.get("name"), a.get("target")) for a in apps]:
        return "relabel"
    return "keep"


# --- uploads ----------------------------------------------------------------------------------------


def upload_row(job: dict, now: float, active: str | None = None) -> dict:
    """One job as the window shows it: state is queued, sending, retrying, held, done or failed."""
    size = max(1, int(job.get("size") or 0))
    got = min(size, int(job.get("received_bytes") or 0))
    percent = 100 if job.get("state") == "confirmed" else int(got * 100 / size)
    if job.get("state") == "confirmed":
        state, detail = "done", f"confirmed {job.get('confirmed_at') or ''}".strip()
    elif job.get("held"):
        state, detail = "held", job["held"]
    elif job.get("state") == "failed":
        state, detail = "failed", job.get("last_error") or "failed"
    elif job.get("id") == active:
        state, detail = "sending", f"{_size(got)} of {_size(size)}"
    elif job.get("last_error"):
        wait = max(0, int((job.get("next_try") or 0) - now))
        state = "retrying"
        detail = (f"next try in {wait} s" if wait else "next try soon") + f": {job['last_error']}"
    else:
        state, detail = "queued", f"{_size(got)} of {_size(size)}" if got else "waiting to send"
    return {"id": job.get("id"), "session": job.get("session_id"), "state": state, "percent": percent,
            "detail": detail, "can_retry": state in ("failed", "retrying", "queued"),
            "can_transfer": state == "held", "destination": job.get("destination"),
            "transfers": len(job.get("transfers") or [])}  # fmt: skip


def _size(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


class UploadWorker:
    """The one thread that owns the upload queue. The window asks it to `add` a recorded
    session, `retry` a job or `kick` a run; it reports with `on_jobs(jobs, active, error)` (posted
    to the Tk thread) after every change and every chunk.

    Started, it runs the jobs that are due (an upload a closed recorder left half sent resumes
    where the portal stopped: upload_queue's resumable protocol), then waits until the next job
    is due (or `idle` seconds, or a request)."""

    def __init__(self, make_queue: Callable, make_portal: Callable, post: Callable,
                 on_jobs: Callable[[list[dict], str | None, str | None], None],
                 clock: Callable[[], float] = time.time, idle: float = 60) -> None:  # fmt: skip
        self.make_queue, self.make_portal, self.post, self.on_jobs = make_queue, make_portal, post, on_jobs
        self.clock, self.idle = clock, idle
        self.requests: queue.SimpleQueue = queue.SimpleQueue()
        self.stopped = threading.Event()
        self.active: str | None = None
        self.thread: threading.Thread | None = None
        self.queue = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._loop, name="tl-uploads", daemon=True)
        self.thread.start()

    def add(self, folder: Path, session_id: str) -> None:
        self.requests.put(("add", folder, session_id))

    def retry(self, job_id: str) -> None:
        self.requests.put(("retry", job_id))

    def transfer(self, job_id: str) -> None:
        """A person decided: the held job goes to the current destination (audited by the queue)."""
        self.requests.put(("transfer", job_id))

    def kick(self, force: bool = False) -> None:
        self.requests.put(("run", force))

    def stop(self) -> None:
        self.stopped.set()
        self.requests.put(("stop",))

    def _publish(self, error: str | None = None) -> None:
        self.post(self.on_jobs, self.queue.jobs(), self.active, error)

    def _changed(self, job: dict) -> None:
        self.active = job["id"] if job.get("state") not in ("confirmed", "failed") else None
        self._publish()

    def _next_wait(self) -> float:
        due = [j.get("next_try") or 0 for j in self.queue.jobs()
               if j["state"] not in ("confirmed", "failed") and not j.get("held")]  # fmt: skip
        if not due:
            return self.idle
        return max(0.5, min(self.idle, min(due) - self.clock()))

    def _run(self, force: bool, only: str | None = None) -> None:
        from twinlab2_recorder.client import PortalError

        self.queue.check_holds()
        if not any(j["state"] not in ("confirmed", "failed") and not j.get("held") for j in self.queue.jobs()):
            self._publish()
            return
        try:
            portal = self.make_portal()
        except PortalError as exc:
            message = str(exc)
            self._publish(message)
            return
        self.queue.run(portal, only=only, force=force)
        self.active = None
        self._publish()

    def step(self, request: tuple) -> None:
        """Handle one request (the loop's body; tests call it directly)."""
        kind = request[0]
        if kind == "add":
            try:
                job = self.queue.add_session(request[1], request[2])
            except OSError as exc:
                message = f"could not queue {request[2]}: {exc}"
                self._publish(message)
                return
            self._run(force=True, only=job["id"])
        elif kind == "retry":
            self.queue.retry(request[1])
            self._run(force=True, only=request[1])
        elif kind == "transfer":
            try:
                self.queue.transfer(request[1])
            except ValueError as exc:
                self._publish(f"not transferred: {exc}")
                return
            self._run(force=True, only=request[1])
        elif kind == "run":
            self._run(force=bool(request[1]))

    def _loop(self) -> None:
        self.queue = self.make_queue(self._changed)
        self.step(("run", False))  # resume after a restart
        while not self.stopped.is_set():
            try:
                request = self.requests.get(timeout=self._next_wait())
            except queue.Empty:
                request = ("run", False)
            if request[0] == "stop":
                return
            try:
                self.step(request)
            except Exception as exc:  # noqa: BLE001 - the worker keeps going; the window shows why
                message = f"{type(exc).__name__}: {exc}"
                self.active = None
                self._publish(message)


# --- the send review --------------------------------------------------------------------------------


SURFACE = {"web": "web pages", "desktop": "desktop windows", "mainframe": "mainframe screens"}


def review_summary(box: dict, notes: list[str] | None = None, portal: str | None = None,
                   machine: str | None = None) -> str:  # fmt: skip
    """What sending these references gives the portal, in words: the destination, the
    applications with their steps, the evidence by category, and what is withheld and why."""
    session = box.get("session") or {}
    flows = box.get("flows") or {}
    apps = box.get("apps") or []
    lines = [f"Session: {session.get('name') or '(no name)'}  ({session.get('id')})",
             f"Destination: {portal or 'not paired: pair with the portal first'}"
             + (f"  (as {machine})" if portal and machine else ""), "", f"Applications ({len(apps)}):"]  # fmt: skip
    for a in apps:
        level = "steps only" if a.get("level") == "steps" else "full"
        lines.append(f"  • {a.get('display') or a['key']}  [{a.get('surface') or a['key'].split(':')[0]}, "
                     f"{level}]: {len(flows.get(a['key'], []))} steps")  # fmt: skip
    if not apps:
        lines.append("  none: no target application was recorded (see the notes)")
    steps = sum(len(v) for v in flows.values())
    by_surface: dict[str, int] = {}
    for key, flow in flows.items():
        by_surface[key.split(":")[0]] = by_surface.get(key.split(":")[0], 0) + len(flow)
    screens = box.get("screens") or {}
    states = sum(len(sc.get("states") or []) for per in screens.values() for sc in per)
    lines += ["", "Evidence sent:"]
    lines.append(f"  • steps: {steps}" + (" (" + ", ".join(f"{n} {SURFACE.get(k, k)}" for k, n in
                                                         sorted(by_surface.items())) + ")" if by_surface else ""))  # fmt: skip
    if screens:
        lines.append(f"  • screen structure: {sum(len(v) for v in screens.values())} screens, {states} states "
                     "(controls and labels, no values)")  # fmt: skip
    timeline = box.get("timeline") or []
    lines.append(f"  • timeline: {len(timeline)} entries")
    pre = box.get("preflight")
    if isinstance(pre, dict):
        checks = pre.get("checks") or []
        failed = [c for c in checks if not c.get("ok")]
        lines.append(f"  • checks before recording: {len(checks)} ({len(failed)} failed"
                     + (", recorded anyway" if failed and not pre.get("ready") else "") + ")")  # fmt: skip
    comp = box.get("completeness")
    if isinstance(comp, dict):
        bad = [f"{k} {v.get('status')}" for k, v in (comp.get("channels") or {}).items()
               if isinstance(v, dict) and v.get("status") not in ("complete", "not_requested")]  # fmt: skip
        lines.append(f"  • completeness: {comp.get('overall')}" + (f" ({', '.join(bad)})" if bad else ""))
    rb = box.get("robot")
    if isinstance(rb, dict):
        lines.append(f"  • robot log: {rb.get('activities')} activities, {rb.get('covered')} of "
                     f"{rb.get('ui_activities')} UI activities seen")  # fmt: skip
    presence = sum(r.get("steps", 0) for r in timeline if r.get("app") == "presence")
    targets = session.get("targets") or []
    lines += ["", "Withheld (stays on this PC):"]
    lines.append(f"  • other applications: used {presence} time(s), not named (they are not targets)")
    local = comp.get("local_exclusions") if isinstance(comp, dict) else None
    if isinstance(local, dict) and local.get("count"):
        names = ", ".join(local.get("names") or [])
        lines.append(f"  • excluded on this PC: {local['count']} application(s), nothing recorded"
                     + (f"; the portal sees only their names: {names}" if names else ""))  # fmt: skip
    lines.append("  • typed values, page contents, network traffic (HAR), pictures and the raw journals: "
                 "the recording folder is not sent; only these references are")  # fmt: skip
    if any(a.get("level") == "steps" for a in apps):
        lines.append("  • steps-only applications: what was done, not the controls' content")
    if len(targets) > len(apps):
        lines.append(f"  • {len(targets) - len(apps)} target(s) with nothing recorded: no steps, nothing sent")
    if notes:
        lines += ["", "Notes:"] + [f"  • {n}" for n in notes]
    return "\n".join(lines)


# --- the recording indicator -------------------------------------------------------------------------


def elapsed(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


class RecordingStatus:
    """The persistent indicator while a session records: name, elapsed time, targets and the
    health of each channel, from the session journal's `channel` and `browser` rows
    (twinlab2_recorder.workstation.supervisor)."""

    def __init__(self, name: str, targets: list[dict], started: float) -> None:
        self.name, self.started = name, started
        self.targets = sorted({target_name(t) for t in targets} - {""})
        self.channels: dict[str, dict] = {}
        self.browser_ready = False
        self.stopping = False
        self.paused = False  # the journal's last pause row has no resume yet
        self.pauses = 0

    def feed(self, row: dict) -> None:
        if row.get("kind") == "browser":
            self.browser_ready = True
            return
        if row.get("kind") in ("pause", "resume"):
            if row["kind"] == "pause" and not self.paused:
                self.pauses += 1
            self.paused = row["kind"] == "pause"
            return
        if row.get("kind") != "channel" or not isinstance(row.get("channel"), str):
            return
        ch = self.channels.setdefault(row["channel"], {"state": "starting", "gaps": 0, "restarts": 0, "reason": None})
        event = row.get("event")
        if event in ("started", "ready"):
            ch["state"] = "starting" if event == "started" else "ok"
        elif event == "gap":
            ch["gaps"] += 1
        elif event == "failed":
            ch.update(state="failed", reason=row.get("reason"))
        elif event == "restarted":
            ch.update(state="restarting", restarts=ch["restarts"] + 1)
        elif event in ("ended", "ended_early", "cut_off"):
            ch.update(state={"ended": "ended", "ended_early": "ended early", "cut_off": "cut off"}[event],
                      reason=row.get("reason") or ch["reason"])  # fmt: skip

    def health(self) -> str:
        """'ok', 'starting' or 'trouble' over every channel."""
        states = [c["state"] for c in self.channels.values()]
        if any(s in ("failed", "ended early", "cut off", "restarting") for s in states) or any(
                c["gaps"] for c in self.channels.values()):  # fmt: skip
            return "trouble"
        if not states or any(s == "starting" for s in states):
            return "starting"
        return "ok"

    def lines(self, now: float) -> list[str]:
        head = "■ STOPPING" if self.stopping else "‖ PAUSED" if self.paused else "● RECORDING"
        out = [f"{head}  {elapsed(now - self.started)}  {self.name}",
               "Targets: " + (", ".join(self.targets) or "none")]  # fmt: skip
        if not self.channels:
            out.append("Channels: starting…")
        else:
            parts = []
            for name in sorted(self.channels):
                c = self.channels[name]
                text = f"{name} {c['state']}"
                if c["restarts"]:
                    text += f" (restarted {c['restarts']}×)"
                if c["gaps"]:
                    text += f" ({c['gaps']} gap{'s' if c['gaps'] > 1 else ''})"
                if c["reason"] and c["state"] in ("failed", "ended early", "cut off"):
                    text += f": {c['reason']}"
                parts.append(text)
            out.append("Channels: " + " · ".join(parts))
        if self.browser_ready:
            out.append("The browser is ready: start the process now.")
        return out


class JournalTail:
    """The rows appended to a JSON-lines file since the last read (a line still being written
    waits for its newline)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.offset = 0
        self.partial = b""

    def read(self) -> list[dict]:
        try:
            with self.path.open("rb") as f:
                f.seek(self.offset)
                data = f.read()
        except OSError:
            return []
        self.offset += len(data)
        data = self.partial + data
        *lines, self.partial = data.split(b"\n")
        rows = []
        for line in lines:
            try:
                row = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows


class JournalWatcher:
    """Reads a session journal's new rows every `interval` seconds on its own thread and posts
    them (`on_rows(rows)`) to the Tk thread."""

    def __init__(self, path: Path, post: Callable, on_rows: Callable[[list[dict]], None],
                 interval: float = 1.0) -> None:  # fmt: skip
        self.tail, self.post, self.on_rows, self.interval = JournalTail(path), post, on_rows, interval
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="tl-journal", daemon=True)

    def start(self) -> JournalWatcher:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.stopped.set()

    def _loop(self) -> None:
        while not self.stopped.wait(self.interval):
            rows = self.tail.read()
            if rows:
                self.post(self.on_rows, rows)


def close_message(exit_code: int | None, waited: float) -> str | None:
    """What the person is told when the window closes during a recording: None when the
    recording finished, else why the window closes anyway and what happens to the recording."""
    if exit_code is None:
        return (f"The recording did not finish within {int(waited)} seconds. The window closes now; the "
                "recording process stops on its own when it sees the window gone, and what it recorded "
                "is kept. The next start of TwinLab Recorder finishes it (or run `TwinLabRecorder "
                "recover`).")  # fmt: skip
    if exit_code != 0:
        return (f"The recording ended with an error (exit code {exit_code}). What was recorded until then "
                "is kept on this PC; its log is in the data folder's logs.")  # fmt: skip
    return None
