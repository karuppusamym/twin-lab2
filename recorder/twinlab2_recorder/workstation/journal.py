# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: imports
# renamed to this repository's packages, unused paths cut. Source and changes: docs/UPSTREAM.md.
"""The capture journal: every event of a workstation session on disk as it happens.

Each channel (`web`, `desktop`, `mainframe`, `network`) appends to its own file,
`capture/<channel>.jsonl` in the session folder, one JSON object per line:

- `{"type": "open", "channel", "at", "pid"}`: the collector started capturing;
- `{"type": "event", "event_id", "channel", "captured_at", "recording", "action", "seq", "ts",
  "origin"?}`: an event, the moment it was captured. `event_id` is a UUIDv7 given then and never
  changed: finalising, renumbering, recovery and reordering keep it; nothing refers to an event
  by its `seq` or its position. Only the action, the times and (web) the target origin are here:
  the event's detail (selectors, snapshots, typed shapes) comes with its `step`;
- `{"type": "step", "event_id", "recording", "step"}`: the event as its recorder finished it
  (the desktop recorder after its batch settles, the web recorder after the page settles, the
  mainframe recorder at once), cleaned the way the recorder cleans what it writes;
- `{"type": "drop", "event_ids", "reason"}`: events the recorder folded into another (two clicks
  into a double click) or found to be no step (a value typed back to what it was);
- `{"type": "har", "recording", "event_id"?, "entry"}`: one HAR entry the recorder kept (the
  network evidence it is permitted to keep: target pages' requests, credential headers removed,
  values typed so far taken out). `event_id` is the last web event captured before the request
  started, when there is one;
- `{"type": "gap", "from", "to", "reason"}`: a time the collector knows it did not capture;
- `{"type": "tick", "at"}`: the collector was alive then (written with each sync when nothing
  else was);
- `{"type": "close", "at", "clean"}`: the collector stopped; `clean` false when it stopped on an
  error.

**Loss window.** Every line is flushed to the operating system when it is written, so a
collector that is killed loses nothing it wrote. Lines are fsync'd every `FSYNC_INTERVAL_S`
(0.5 s), so a machine that loses power or crashes loses at most the lines of the last interval.
An event is in the journal the moment the recorder hears of it (its `event` line), so the
**loss window is `LOSS_WINDOW_S` = 2 s**: an event captured more than 2 s before the collector
died is on disk. A recovered recording marks the 2 s after its last durable line as a gap.
What the window does not cover: an event's detail, which is written when the recorder finishes
the step (up to `desktop.recorder.MAX_BATCH_S` = 3 s later on the desktop, after the page
settles on the web, up to 15 s). A recovered event without its detail is kept with its action
and times and `incomplete: true`.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

CHANNELS = ("web", "desktop", "mainframe", "network")
DIR = "capture"
FSYNC_INTERVAL_S = 0.5
TICK_S = 1.0
LOSS_WINDOW_S = 2.0
EVENT_FIELDS = ("event_id", "channel", "captured_at")

_last_ms = 0
_counter = 0
_seq_lock = threading.Lock()


def uuid7() -> str:
    """A UUID version 7 (RFC 9562): 48 bits of Unix milliseconds, then random bits. Ids made in
    one process sort in the order they were made: within one millisecond the 12 bits after the
    time count up (RFC 9562 method 1)."""
    global _last_ms, _counter
    with _seq_lock:
        ms = time.time_ns() // 1_000_000
        if ms <= _last_ms:
            ms, _counter = _last_ms, _counter + 1
            if _counter > 0xFFF:
                ms, _counter = ms + 1, 0
        else:
            _counter = secrets.randbits(11)  # room to count up within the millisecond
        _last_ms = ms
        rand_a = _counter
        rand_b = secrets.randbits(62)
    value = (ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    h = f"{value:032x}"
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def now_iso() -> str:
    """ISO-8601 UTC with milliseconds: `2026-11-20T09:15:02.123Z`."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def stamp(step: dict, channel: str, captured_at: str | None = None) -> dict:
    """Give a step its permanent id, its channel and when it was captured (in place). A step
    that has an id keeps it."""
    step.setdefault("event_id", uuid7())
    step.setdefault("channel", channel)
    step.setdefault("captured_at", captured_at or now_iso())
    return step


def path_of(folder: Path, channel: str) -> Path:
    return Path(folder) / DIR / f"{channel}.jsonl"


class Journal:
    """An append-only JSON Lines file, flushed on every write and fsync'd every
    `fsync_interval` seconds by a thread of its own. Thread-safe."""

    def __init__(self, path: Path, channel: str | None = None, *,
                 fsync_interval: float = FSYNC_INTERVAL_S, ticks: bool = True,
                 fsync: Callable[[int], None] = os.fsync) -> None:  # fmt: skip
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.channel = channel
        self.fsync_interval = fsync_interval
        self.ticks = ticks
        self._fsync = fsync
        self._lock = threading.Lock()
        self._file = self.path.open("a", encoding="utf-8")
        self._dirty = False
        self.opened = False
        self._last_write = 0.0
        self._closed = threading.Event()
        self.synced_at = 0.0  # time.monotonic() of the last fsync
        self.syncs = 0
        self._thread = threading.Thread(target=self._run, name=f"tl-journal-{channel or 'session'}",
                                        daemon=True)  # fmt: skip
        self._thread.start()

    # -- writing -----------------------------------------------------------------------------------

    def write(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._lock:
            if self._file.closed:
                return
            self._file.write(line)
            self._file.flush()
            self._dirty = True
            self._last_write = time.monotonic()

    def open(self) -> None:
        """The collector started capturing (once: a second call writes nothing)."""
        if self.opened:
            return
        self.opened = True
        self.write({"type": "open", "channel": self.channel, "at": now_iso(), "pid": os.getpid()})

    def event(self, step: dict, recording: str | None = None) -> dict:
        """The event line of a step just captured (stamped first if it has no id yet)."""
        stamp(step, self.channel or "", None)
        rec = {"type": "event", "event_id": step["event_id"], "channel": step["channel"],
               "captured_at": step["captured_at"], "recording": recording,
               "action": step.get("action"), "seq": step.get("seq"), "ts": step.get("ts")}  # fmt: skip
        if step.get("origin"):
            rec["origin"] = step["origin"]
        if step.get("tab"):
            rec["tab"] = step["tab"]
        self.write(rec)
        return step

    def step(self, step: dict, recording: str | None = None) -> None:
        """The finished step (as the recorder writes it: no `_` bookkeeping keys)."""
        public = {k: v for k, v in step.items() if not str(k).startswith("_")}
        self.write({"type": "step", "event_id": step.get("event_id"), "recording": recording,
                    "step": public})  # fmt: skip

    def drop(self, event_ids, reason: str) -> None:
        ids = [e for e in event_ids if e]
        if ids:
            self.write({"type": "drop", "event_ids": ids, "reason": reason})

    def har(self, entry: dict, recording: str | None = None, event_id: str | None = None) -> None:
        rec = {"type": "har", "recording": recording, "entry": entry}
        if event_id:
            rec["event_id"] = event_id
        self.write(rec)

    def gap(self, start: str, end: str, reason: str) -> None:
        self.write({"type": "gap", "from": start, "to": end, "reason": reason})

    # -- durability --------------------------------------------------------------------------------

    def sync(self) -> None:
        """fsync what was written; a tick first when nothing was written for `TICK_S`."""
        with self._lock:
            if self._file.closed:
                return
            if self.ticks and time.monotonic() - self._last_write >= TICK_S:
                self._file.write(json.dumps({"type": "tick", "at": now_iso()}) + "\n")
                self._file.flush()
                self._dirty = True
                self._last_write = time.monotonic()
            if not self._dirty:
                return
            self._fsync(self._file.fileno())
            self._dirty = False
            self.synced_at = time.monotonic()
            self.syncs += 1

    def _run(self) -> None:
        while not self._closed.wait(self.fsync_interval):
            try:
                self.sync()
            except (OSError, ValueError):  # a disk error: the next interval tries again
                pass

    def close(self, clean: bool = True) -> None:
        if self._closed.is_set():
            return
        self.write({"type": "close", "at": now_iso(), "clean": bool(clean)})
        self._closed.set()
        self._thread.join(5)
        with self._lock:
            if not self._file.closed:
                self._file.flush()
                try:
                    self._fsync(self._file.fileno())
                except OSError:
                    pass
                self._file.close()

    @property
    def closed(self) -> bool:
        return self._closed.is_set()


def read(path: Path) -> list[dict]:
    """The lines of a journal; a last line cut short by a crash is left out, and so is any other
    line that is not JSON (a crash can leave a torn line before the next process appends)."""
    if not Path(path).is_file():
        return []
    out = []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out
