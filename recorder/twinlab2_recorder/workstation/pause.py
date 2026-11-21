"""Pausing a recording: while the file `paused` is in the session folder, nothing is captured.

The window (twinlab2_recorder.app) creates the file to pause and removes it to resume, as it
creates `stop` to stop. Every collector process reads it through a `PauseFlag` and drops what
it would have recorded at the point where it takes events in:

- desktop: the input hooks and UI Automation events are not taken (no element is read), the
  windows are not polled, a step already under way is written first (it began before the pause);
- web: no step and no network entry for any page (the same filters as for other origins, but
  nothing is reported as `outside`);
- mainframe: the proxy passes the terminal's traffic on, and writes no step or screen;
- the session's watcher: no "another application was used" row.

The session (in the recording process) writes a `pause` and a `resume` row to the journal at
each change, and the periods go to session.json (`pauses`) and the references' completeness
(`paused`, recorder contract 1.7). A pause is not a gap: the person chose it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import threading

FILE = "paused"
POLL_S = 0.2


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class PauseFlag:
    """Whether the session folder says "paused", read on a thread of its own every `interval`
    seconds: calling the flag costs nothing (an input hook asks it on every key)."""

    def __init__(
        self, folder: Path, interval: float = POLL_S, start: bool = True
    ) -> None:
        self.path = Path(folder) / FILE
        self.interval = interval
        self.paused = self.path.exists()
        self._stop = threading.Event()
        self._listeners: list = []
        if start:
            threading.Thread(target=self._loop, name="tl-pause", daemon=True).start()

    def __call__(self) -> bool:
        return self.paused

    def on_change(self, fn) -> None:
        """`fn(paused: bool)` on the flag's thread, at each change."""
        self._listeners.append(fn)

    def check(self) -> bool:
        now = self.path.exists()
        if now != self.paused:
            self.paused = now
            for fn in list(self._listeners):
                try:
                    fn(now)
                except Exception:  # noqa: BLE001 - a listener never stops the flag
                    pass
        return now

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self.check()


class Pauses:
    """The session's pause periods, from the flag's changes: journal rows and session.json."""

    def __init__(self, journal) -> None:
        self.journal = journal
        self.periods: list[dict] = []
        self._lock = threading.Lock()

    def changed(self, paused: bool) -> None:
        with self._lock:
            now = _now()
            if paused and not (self.periods and self.periods[-1]["to"] is None):
                self.periods.append({"from": now, "to": None})
                self.journal({"kind": "pause", "ts": now})
            elif not paused and self.periods and self.periods[-1]["to"] is None:
                self.periods[-1]["to"] = now
                self.journal({"kind": "resume", "ts": now})

    def close(self) -> list[dict]:
        """The periods, an open one ended now (the recording stopped while paused)."""
        self.changed(False)
        return [dict(p) for p in self.periods]


def summary(periods: list[dict]) -> dict | None:
    """`completeness.paused` (contract 1.7): how many pauses, their seconds, and the periods
    (at most 50). None when the recording was never paused."""
    out = []
    total = 0.0
    for p in periods or []:
        try:
            a = datetime.fromisoformat(str(p["from"]).replace("Z", "+00:00"))
            b = datetime.fromisoformat(str(p["to"]).replace("Z", "+00:00"))
        except (KeyError, ValueError, TypeError):
            continue
        seconds = max(0.0, (b - a).total_seconds())
        total += seconds
        out.append({"from": p["from"], "to": p["to"], "seconds": round(seconds, 1)})
    if not out:
        return None
    return {"count": len(out), "seconds": round(total, 1), "periods": out[:50]}


def paused_periods(rows: list[dict]) -> list[dict]:
    """The pause periods in a journal's rows (a recovered session's: its session.json may not
    have them); one still open ends at the last row's time."""
    periods: list[dict] = []
    last = None
    for r in rows:
        last = r.get("ts") or last
        if r.get("kind") == "pause" and not (periods and periods[-1]["to"] is None):
            periods.append({"from": r.get("ts"), "to": None})
        elif r.get("kind") == "resume" and periods and periods[-1]["to"] is None:
            periods[-1]["to"] = r.get("ts")
    if periods and periods[-1]["to"] is None:
        periods[-1]["to"] = last
    return [p for p in periods if p["from"] and p["to"]]
