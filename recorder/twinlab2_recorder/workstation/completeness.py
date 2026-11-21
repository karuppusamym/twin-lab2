# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: imports
# renamed to this repository's packages, unused paths cut. Source and changes: docs/UPSTREAM.md.
"""What a session recording holds, and what it missed: the completeness manifest of recorder
contract 1.1.0.

    {"overall": "complete|partial|failed",
     "channels": {"web|desktop|mainframe|network": {"status": "complete|interrupted|missing|
                  not_requested", "gaps": [{"from", "to", "reason"}], "notes": [str]}},
     "network_evidence": "complete|incomplete|none",
     "recovered": bool, "loss_window_seconds": number | null,
     "local_exclusions": {"count", "names", "entries", "managed"}}  (contract 1.5, optional)

A channel is requested when a target needs it (a web origin: `web` and `network`; a desktop
executable or identity: `desktop`; a TN3270 host: `mainframe`); a session says which in
`session.json` `channels`. From the capture journal (twinlab2_recorder.workstation.journal) a requested
channel is:

- `missing` when its collector never started capturing (no browser, not Windows...);
- `complete` when it started, closed cleanly and has no gap;
- `interrupted` otherwise: it stopped on an error, it has a gap (the browser was closed and
  opened again), or it never closed (the program died: the gap is the loss window after its
  last durable line).

A stopped recording is therefore not complete by itself: `overall` is `complete` only when every
requested channel is; `failed` when every requested channel is missing; `partial` in between.

A folder recorded before the journal (contract 1.0) has no capture journal: its channels are
judged by the recordings present, and `loss_window_seconds` is null.

A collector supervisor that watches the collectors (heartbeats, exits) may know more than the
journal: whatever it reports per channel, in `session.json` `channel_status` (the same shape
as a channel here), is merged in; the worse status wins and the gaps and notes are added.

`local_exclusions` (contract 1.5) says how many applications this PC's exclude list kept out of the
recording (twinlab2_recorder.workstation.exclude): the names of the entries that matched, never a
title, address or time. They are not gaps: an excluded application was never meant to be recorded,
and a channel is not less complete for it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from twinlab2_recorder.workstation import journal as capture

RANK = {"not_requested": 0, "complete": 1, "interrupted": 2, "missing": 3}


def requested(meta: dict) -> list[str]:
    """The channels a session needs, from `channels` or else from its targets."""
    if isinstance(meta.get("channels"), list):
        return [c for c in capture.CHANNELS if c in meta["channels"]]
    out: set[str] = set()
    for t in meta.get("targets") or []:
        if t.get("origin"):
            out |= {"web", "network"}
        elif t.get("tn3270"):
            out.add("mainframe")
        elif t.get("exe") or str(t.get("key") or "").startswith(("desktop:", "exe:")):
            out.add("desktop")
    return [c for c in capture.CHANNELS if c in out]


def _later(ts: str, seconds: float) -> str:
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return ts
    return (t.astimezone(UTC) + timedelta(seconds=seconds)).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")  # fmt: skip


def _last_at(records: list[dict]) -> str | None:
    for r in reversed(records):
        at = r.get("at") or r.get("captured_at")
        if at:
            return at
    return None


def from_journal(records: list[dict], channel: str) -> dict:
    """One channel's status, gaps and notes from its journal lines."""
    gaps = [{"from": r.get("from"), "to": r.get("to"), "reason": r.get("reason") or ""}
            for r in records if r.get("type") == "gap"]  # fmt: skip
    notes: list[str] = []
    opened = any(r.get("type") == "open" for r in records)
    close = next((r for r in reversed(records) if r.get("type") == "close"), None)
    if not opened:
        return {"status": "missing", "gaps": gaps,
                "notes": ["the collector never started capturing"]}  # fmt: skip
    if close is None:
        last = _last_at(records)
        if last:
            gaps.append({"from": last, "to": _later(last, capture.LOSS_WINDOW_S),
                         "reason": "the recorder stopped unexpectedly: events after its last "
                                   "durable journal line may be lost"})  # fmt: skip
        notes.append("the channel did not close cleanly")
    elif not close.get("clean", True):
        gaps.append({"from": close.get("at"), "to": close.get("at"),
                     "reason": "the collector stopped on an error"})  # fmt: skip
        notes.append("the collector stopped on an error")
    events = {r["event_id"] for r in records if r.get("type") == "event" and r.get("event_id")}
    done = {r["event_id"] for r in records if r.get("type") == "step" and r.get("event_id")}
    dropped = {e for r in records if r.get("type") == "drop" for e in r.get("event_ids") or []}
    unfinished = events - done - dropped
    if close is None and unfinished:
        notes.append(f"{len(unfinished)} event(s) kept without their detail (not settled yet)")
    if channel == "network":
        n = sum(1 for r in records if r.get("type") == "har")
        notes.append(f"{n} request(s) in the network journal")
    status = (
        "complete" if close is not None and close.get("clean", True) and not gaps else "interrupted"
    )
    return {"status": status, "gaps": gaps, "notes": notes}


def _legacy(folder: Path, channel: str, meta: dict) -> dict:
    """A channel of a folder recorded without a journal (contract 1.0)."""
    parts = [folder / "web"] + sorted(folder.glob("web-*"))
    present = {
        "web": any((p / "steps.jsonl").is_file() for p in parts),
        "desktop": (folder / "desktop" / "steps.jsonl").is_file(),
        "mainframe": (folder / "mainframe" / "steps.jsonl").is_file(),
        "network": any((p / "recording.har").is_file() for p in parts),
    }[channel]
    if not present:
        return {"status": "missing", "gaps": [], "notes": ["no recording for this channel"]}
    if meta.get("state") != "stopped":
        return {"status": "interrupted", "gaps": [],
                "notes": ["recorded before the capture journal: what was lost is not known"]}  # fmt: skip
    return {"status": "complete", "gaps": [], "notes": []}


def _merge(ours: dict, theirs: dict | None) -> dict:
    if not isinstance(theirs, dict):
        return ours
    status = theirs.get("status") if theirs.get("status") in RANK else ours["status"]
    if RANK[status] < RANK[ours["status"]]:
        status = ours["status"]
    gaps = list(ours["gaps"])
    for g in theirs.get("gaps") or []:
        g = {"from": g.get("from"), "to": g.get("to"), "reason": str(g.get("reason") or "")}
        if g not in gaps:
            gaps.append(g)
    notes = ours["notes"] + [
        str(n) for n in theirs.get("notes") or [] if str(n) not in ours["notes"]
    ]
    if status == "complete" and gaps:
        status = "interrupted"
    return {"status": status, "gaps": gaps, "notes": notes}


# The contract's limits (recorder-contract.schema.json): what the manifest holds is cut to them
# when it is built, so `send` never refuses a recording over the length of a note.
NOTE_MAX, NOTES_MAX, REASON_MAX, GAPS_MAX, DETAIL_MAX = 300, 50, 200, 500, 300


def fit(text, limit: int) -> str:
    """`text` within `limit` characters: cut at a word where it can be, ending in "..."."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 3]
    if " " in cut[limit // 2 :]:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(" ,;:") + "..."


def _fit_channel(block: dict) -> dict:
    notes = [fit(n, NOTE_MAX) for n in block.get("notes") or []]
    if len(notes) > NOTES_MAX:
        notes = notes[: NOTES_MAX - 1] + [f"{len(notes) - NOTES_MAX + 1} more note(s) not listed"]
    gaps = [dict(g, reason=fit(g.get("reason"), REASON_MAX)) for g in block.get("gaps") or []]
    return dict(block, notes=notes, gaps=gaps[:GAPS_MAX])


def manifest(folder: Path, meta: dict) -> dict:
    """The completeness manifest of a session folder (`meta`: its session.json)."""
    folder = Path(folder)
    want = requested(meta)
    journaled = (folder / capture.DIR).is_dir()
    channels: dict[str, dict] = {}
    supervised = meta.get("channel_status") if isinstance(meta.get("channel_status"), dict) else {}
    for ch in capture.CHANNELS:
        if ch not in want:
            channels[ch] = {"status": "not_requested", "gaps": [], "notes": []}
            continue
        if journaled:
            state = from_journal(capture.read(capture.path_of(folder, ch)), ch)
        else:
            state = _legacy(folder, ch, meta)
        channels[ch] = _fit_channel(_merge(state, supervised.get(ch)))
    statuses = [channels[c]["status"] for c in want]
    if all(s == "complete" for s in statuses):
        overall = "complete"
    elif all(s == "missing" for s in statuses):
        overall = "failed"
    else:
        overall = "partial"
    net = channels["network"]
    if net["status"] == "complete":
        evidence = "complete"
    elif net["status"] == "interrupted" or (
        net["status"] == "missing" and "network" in want and _has_har(folder)
    ):
        evidence = "incomplete"
    else:
        evidence = "none"
    out = {"overall": overall, "channels": channels, "network_evidence": evidence,
           "recovered": bool(meta.get("recovered") or meta.get("state") == "recovered"),
           "loss_window_seconds": capture.LOSS_WINDOW_S if journaled else None}  # fmt: skip
    local = local_exclusions(folder, meta)
    if local is not None:
        out["local_exclusions"] = local
    return out


def local_exclusions(folder: Path, meta: dict) -> dict | None:
    """What this PC's exclude list kept out (exclude.summary), from the journal's `excluded`
    rows (names only) and session.json `local_policy`."""
    from twinlab2_recorder.workstation import exclude
    from twinlab2_recorder.workstation.timeline import read_jsonl

    policy = meta.get("local_policy") if isinstance(meta.get("local_policy"), dict) else {}
    names = [r.get("name") for r in read_jsonl(Path(folder) / "journal.jsonl") if r.get("kind") == "excluded"]
    entries = policy.get("entries") if isinstance(policy.get("entries"), int) else 0
    return exclude.summary(names, entries, bool(policy.get("managed")))


def _has_har(folder: Path) -> bool:
    return any((p / "recording.har").is_file() for p in [folder / "web", *folder.glob("web-*")])


def preflight(meta: dict) -> dict | None:
    """The preflight block `{"checks": [{"name", "ok", "detail"}], "ready"}` a session was given
    (twinlab2_recorder.workstation.preflight), or None."""
    pre = meta.get("preflight")
    if not isinstance(pre, dict) or not isinstance(pre.get("checks"), list):
        return None
    checks = [{"name": str(c.get("name") or ""), "ok": bool(c.get("ok")),
               "detail": fit(c.get("detail"), DETAIL_MAX)}
              for c in pre["checks"] if isinstance(c, dict)]  # fmt: skip
    ready = pre.get("ready")
    return {
        "checks": checks,
        "ready": bool(ready) if ready is not None else all(c["ok"] for c in checks),
    }
