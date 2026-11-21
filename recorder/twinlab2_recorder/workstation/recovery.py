# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: imports
# renamed to this repository's packages, unused paths cut. Source and changes: docs/UPSTREAM.md.
"""Rebuild a session's recordings from its capture journal (twinlab2_recorder.workstation.journal) after the
program that recorded it died.

For each recording a channel wrote (`web`, `web-2`..., `desktop`, `mainframe`), the steps are
the journal's events in capture order, less those the recorder dropped: an event whose step
was written has that step; an event whose step was not written yet (the page or the desktop
batch had not settled) is kept as `{"seq", "ts", "action", "event_id", "channel",
"captured_at", "incomplete": true}` with the target origin and tab when the web recorder knew
them. A web recording without `recording.har` gets one from the network journal's entries.
What was already in `steps.jsonl` is kept: a step there that the journal does not have (written
before the journal existed) stays where it was.
"""

from __future__ import annotations

import json
from pathlib import Path

from twinlab2_recorder.workstation import journal as capture
from twinlab2_recorder.workstation.timeline import read_jsonl

STEP_CHANNELS = ("web", "desktop", "mainframe")
SURFACE = {"web": None, "desktop": "desktop", "mainframe": "mainframe"}


def _stub(marker: dict, channel: str) -> dict:
    step = {"seq": marker.get("seq"), "ts": marker.get("ts") or marker.get("captured_at"),
            "action": marker.get("action"), "event_id": marker["event_id"], "channel": channel,
            "captured_at": marker.get("captured_at"), "incomplete": True}  # fmt: skip
    if SURFACE[channel]:
        step["surface"] = SURFACE[channel]
    if marker.get("origin"):
        step["origin"] = marker["origin"]
        step["url"] = marker["origin"] + "/"
    if marker.get("tab"):
        step["tab"] = marker["tab"]
    return step


def steps_of(records: list[dict], channel: str) -> dict[str, list[dict]]:
    """recording name -> its steps, from one channel's journal lines."""
    order: list[str] = []
    markers: dict[str, dict] = {}
    finished: dict[str, dict] = {}
    where: dict[str, str] = {}
    dropped: set[str] = set()
    for r in records:
        kind, eid = r.get("type"), r.get("event_id")
        if kind == "event" and eid and eid not in markers:
            markers[eid] = r
            order.append(eid)
            where[eid] = r.get("recording") or channel
        elif kind == "step" and eid and isinstance(r.get("step"), dict):
            if eid not in markers:
                order.append(eid)
                markers[eid] = {}
            finished[eid] = r["step"]
            where[eid] = r.get("recording") or where.get(eid) or channel
        elif kind == "drop":
            dropped.update(e for e in r.get("event_ids") or [] if e)
    out: dict[str, list[dict]] = {}
    for eid in order:
        if eid in dropped:
            continue
        step = finished.get(eid) or _stub(markers[eid], channel)
        out.setdefault(where[eid], []).append(step)
    return out


def _merge(existing: list[dict], journaled: list[dict], channel: str) -> list[dict]:
    have = {s.get("event_id") for s in journaled}
    legacy = [s for s in existing if not s.get("event_id") or s["event_id"] not in have]
    steps = legacy + journaled
    if channel == "web":  # numbered on arrival: the capture order is the seq order
        steps.sort(key=lambda s: (not isinstance(s.get("seq"), int), s.get("seq") or 0))
        from twinlab2_recorder.web.recorder import link_events

        link_events(steps)
    return steps


def _write_steps(path: Path, steps: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(s, ensure_ascii=False) + "\n" for s in steps),
                   encoding="utf-8")  # fmt: skip
    tmp.replace(path)


def rebuild(folder: Path) -> dict:
    """Write each recording's steps (and a missing HAR) from the journal. Returns what was done
    per channel: `{"events", "incomplete", "recordings"}`, and for `network` `{"entries",
    "rebuilt"}` (the recordings whose HAR came from the journal)."""
    folder = Path(folder)
    done: dict[str, dict] = {}
    for channel in STEP_CHANNELS:
        records = capture.read(capture.path_of(folder, channel))
        if not records:
            continue
        info = {"events": 0, "incomplete": 0, "recordings": []}
        for name, journaled in steps_of(records, channel).items():
            path = folder / name / "steps.jsonl"
            steps = _merge(read_jsonl(path), journaled, channel)
            _write_steps(path, steps)
            info["events"] += len(journaled)
            info["incomplete"] += sum(1 for s in journaled if s.get("incomplete"))
            info["recordings"].append(name)
        done[channel] = info
    records = capture.read(capture.path_of(folder, "network"))
    if records:
        by_rec: dict[str, list[dict]] = {}
        for r in records:
            if r.get("type") == "har" and isinstance(r.get("entry"), dict):
                entry = dict(r["entry"])
                if r.get("event_id"):
                    entry["_event_id"] = r["event_id"]
                by_rec.setdefault(r.get("recording") or "web", []).append(entry)
        rebuilt = []
        for name, entries in by_rec.items():
            har = folder / name / "recording.har"
            if har.is_file():
                continue
            har.parent.mkdir(parents=True, exist_ok=True)
            doc = {"log": {"version": "1.2", "creator": {"name": "twinlab-recover", "version": "1"},
                           "comment": "rebuilt from the session's network journal after the "
                                      "recorder stopped unexpectedly", "pages": [],
                           "entries": entries}}  # fmt: skip
            tmp = har.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc), encoding="utf-8")
            tmp.replace(har)
            rebuilt.append(name)
        done["network"] = {"entries": sum(len(v) for v in by_rec.values()), "rebuilt": rebuilt}
    return done
