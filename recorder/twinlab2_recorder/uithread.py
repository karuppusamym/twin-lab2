"""Keeping the recorder's window responsive: work off the Tk thread, results back onto it.

The window (twinlab2_recorder.main.gui) runs Tk on one thread. Anything that can take more than
a moment runs as a background **job** (`Jobs.run`): pairing, polling the portal, sending
references, uploads and finishing a recording when the window closes. A job has an explicit
state, shown to the person:

    connecting   reaching the portal (pairing, polling)
    sending      sending references or an upload
    retrying     the portal could not be reached; trying again (`reason` says why, `attempt`)
    stopping     a recording is being finished
    done         finished (`result`)
    failed       gave up (`reason`)

**The guard.** `mark_ui_thread()` names the Tk thread; `assert_off_ui_thread(what)` raises
`UIThreadViolation` when called on it. Every portal call (twinlab2_recorder.client) and every long
wait (`wait_process`) asserts, so a network call or a wait that slips onto the Tk thread fails
loudly in tests instead of freezing the window. Outside the window (the command line) no thread is
marked and the guard does nothing.

**Back onto the Tk thread.** Workers never touch Tk. They `post()` a callable to a `Dispatcher`,
a thread-safe queue the window drains every 50 ms on the Tk thread (`drain()`). A callable is
posted with its values already bound: a closure over an `except ... as exc` name would run after
Python has deleted that name (the recorder's "portal offline" status never showed because of
that; tests/recorder/test_ui_threads.py checks the whole recorder for the pattern).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import itertools
import queue
import subprocess
import threading
import time

STATES = ("connecting", "sending", "retrying", "stopping", "done", "failed")
_ui_ident: int | None = None


class UIThreadViolation(RuntimeError):
    """A network call or a long wait ran on the Tk thread."""


def mark_ui_thread(ident: int | None = None) -> None:
    """Name the thread that runs Tk (the calling one by default)."""
    global _ui_ident
    _ui_ident = threading.get_ident() if ident is None else ident


def clear_ui_thread() -> None:
    global _ui_ident
    _ui_ident = None


def on_ui_thread() -> bool:
    return _ui_ident is not None and threading.get_ident() == _ui_ident


def assert_off_ui_thread(what: str) -> None:
    if on_ui_thread():
        raise UIThreadViolation(f"{what} on the window's thread: run it as a background job")


def wait_process(proc: subprocess.Popen, timeout: float) -> int | None:
    """Wait for `proc` (off the Tk thread); its exit code, or None when it is still running."""
    assert_off_ui_thread("waiting for a process")
    try:
        return proc.wait(timeout)
    except subprocess.TimeoutExpired:
        return None


class Dispatcher:
    """Callables posted from any thread, run on the Tk thread by `drain()`."""

    def __init__(self) -> None:
        self._queue: queue.SimpleQueue = queue.SimpleQueue()

    def post(self, fn: Callable, *args) -> None:
        self._queue.put((fn, args))

    def drain(self, limit: int = 200) -> int:
        n = 0
        while n < limit:
            try:
                fn, args = self._queue.get_nowait()
            except queue.Empty:
                break
            fn(*args)
            n += 1
        return n


@dataclass
class Job:
    id: int
    kind: str
    state: str
    label: str = ""
    reason: str | None = None
    attempt: int = 1
    result: object = None
    started: float = field(default_factory=time.monotonic)

    @property
    def finished(self) -> bool:
        return self.state in ("done", "failed")

    def describe(self) -> str:
        text = {"connecting": "Connecting", "sending": "Sending", "retrying": "Retrying",
                "stopping": "Stopping", "done": "Done", "failed": "Failed"}[self.state]  # fmt: skip
        if self.label:
            text += f": {self.label}"
        if self.state == "retrying":
            text += f" (attempt {self.attempt})"
        if self.reason and self.state in ("retrying", "failed"):
            text += f" — {self.reason}"
        return text


def _unreachable(exc: BaseException) -> bool:
    return getattr(exc, "unreachable", False) is True


class Jobs:
    """Runs jobs on worker threads; reports each state change on the Tk thread.

    `listener(job)` (on the Tk thread, through the dispatcher) hears every state; `on_done(result)`
    and `on_failed(reason)` the end. A job retries while `retry_if(exc)` (by default: the portal
    could not be reached) up to `retries` more times, `delay(attempt)` seconds apart."""

    def __init__(self, dispatcher: Dispatcher, listener: Callable[[Job], None] | None = None,
                 sleep: Callable[[float], None] | None = None) -> None:  # fmt: skip
        self.dispatcher = dispatcher
        self.listener = listener
        self.cancelled = threading.Event()
        self._sleep = sleep or (lambda s: self.cancelled.wait(s))
        self._ids = itertools.count(1)
        self.threads: list[threading.Thread] = []

    def _report(self, job: Job, **changes) -> None:
        for k, v in changes.items():
            setattr(job, k, v)
        snapshot = Job(**{k: getattr(job, k) for k in job.__dataclass_fields__})
        if self.listener:
            self.dispatcher.post(self.listener, snapshot)

    def run(self, kind: str, work: Callable[[Callable[..., None]], object], *, state: str = "connecting",
            label: str = "", retries: int = 0, delay: Callable[[int], float] = lambda n: min(30, 2 ** n),
            retry_if: Callable[[BaseException], bool] = _unreachable,
            on_done: Callable[[object], None] | None = None,
            on_failed: Callable[[str], None] | None = None) -> Job:  # fmt: skip
        """Start `work(report)` on a worker; `report(state=..., reason=...)` lets it change state."""
        if state not in STATES[:4]:
            raise ValueError(f"a job starts connecting, sending or stopping, not {state}")
        job = Job(next(self._ids), kind, state, label)

        def report(**changes) -> None:
            self._report(job, **changes)

        def body() -> None:
            report()
            attempt = 1
            while True:
                try:
                    result = work(report)
                except (Exception, SystemExit) as exc:  # noqa: BLE001 - every failure is shown, with its reason
                    reason = str(exc) or type(exc).__name__
                    if attempt <= retries and retry_if(exc) and not self.cancelled.is_set():
                        report(state="retrying", reason=reason, attempt=attempt + 1)
                        self._sleep(delay(attempt))
                        attempt += 1
                        report(state=state)
                        continue
                    report(state="failed", reason=reason)
                    if on_failed:
                        self.dispatcher.post(on_failed, reason)
                    return
                report(state="done", result=result, reason=None)
                if on_done:
                    self.dispatcher.post(on_done, result)
                return

        thread = threading.Thread(target=body, name=f"tl-job-{kind}-{job.id}", daemon=True)
        self.threads = [t for t in self.threads if t.is_alive()] + [thread]
        thread.start()
        return job

    def join(self, timeout: float = 10) -> None:
        """Wait for the jobs started so far (tests; off the Tk thread)."""
        assert_off_ui_thread("waiting for jobs")
        end = time.monotonic() + timeout
        for t in list(self.threads):
            t.join(max(0, end - time.monotonic()))
