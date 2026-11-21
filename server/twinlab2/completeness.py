"""How complete a recording is, as a recorder of contract 1.1 reports it, and what the pages say.

A recorder reports `completeness` per channel (web, desktop, mainframe, network): complete,
interrupted (with the gaps, from and to), missing, or not requested, plus whether the network
evidence is complete, whether the recording was recovered after the recorder stopped
unexpectedly and how many seconds may be lost. A stopped recording is **not** complete by
itself: only `overall: complete` is. A partial or failed recording needs attention until it is
re-recorded or its owner accepts it as it is (`needs_attention`).

An older recorder (contract 1.0.x) reports nothing: its completeness is unknown, which is shown as
such and does not hold up the journey.

A 1.5 recorder may say that the PC's own exclude list kept applications out of the recording
(`local_exclusions`: how many, and the names of the exclude entries, never a title or address).
That is not a gap and does not make a recording partial: the pages say "N applications excluded on
this PC" so that the missing activity is not taken for a failure.

A 1.6 recorder may say that the person paused the recording (`paused`: how many times, for how
long, and each period). Nothing was captured meanwhile, by their choice: not a gap either, and
the pages say "Paused twice, for 3 min 21 s in all".
"""

from __future__ import annotations

from datetime import datetime, timezone

CHANNELS = ("web", "desktop", "mainframe", "network")
OVERALL = ("complete", "partial", "failed")
STATUSES = ("complete", "interrupted", "missing", "not_requested")
NETWORK = ("complete", "incomplete", "none")
ATTENTION = {"partial", "failed"}
UNKNOWN_OLDER = "Completeness unknown (older recorder)"
UNKNOWN_IMPORTED = "Completeness unknown (imported event log)"
CHANNEL_NAMES = {"web": "Web", "desktop": "desktop", "mainframe": "mainframe", "network": "network"}


def _text(value, limit):
    return value[:limit] if isinstance(value, str) and value else None


def clean(doc) -> dict | None:
    """The completeness a recorder sent, in the stored form (known fields only), or None."""
    if not isinstance(doc, dict) or doc.get("overall") not in OVERALL:
        return None
    channels = {}
    for name in CHANNELS:
        item = (doc.get("channels") or {}).get(name)
        if not isinstance(item, dict) or item.get("status") not in STATUSES:
            continue
        gaps = []
        for gap in item.get("gaps") or []:
            if isinstance(gap, dict) and parse(gap.get("from")) and parse(gap.get("to")):
                gaps.append({"from": gap["from"], "to": gap["to"], "reason": _text(gap.get("reason"), 200) or ""})
        notes = [n[:300] for n in item.get("notes") or [] if isinstance(n, str) and n]
        channels[name] = {"status": item["status"], "gaps": gaps[:500], "notes": notes[:50]}
    loss = doc.get("loss_window_seconds")
    out = {
        "overall": doc["overall"], "channels": channels,
        "network_evidence": doc.get("network_evidence") if doc.get("network_evidence") in NETWORK else "none",
        "recovered": doc.get("recovered") is True,
        "loss_window_seconds": loss if isinstance(loss, (int, float)) and not isinstance(loss, bool) and loss >= 0 else None,
    }
    local = clean_exclusions(doc.get("local_exclusions"))
    if local is not None:
        out["local_exclusions"] = local
    paused = clean_paused(doc.get("paused"))
    if paused is not None:
        out["paused"] = paused
    return out


def _seconds(value) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return round(float(value), 1)
    return None


def clean_paused(doc) -> dict | None:
    """`paused` (contract 1.6) in the stored form: how many pauses, their seconds in all, and
    the periods with a valid start and end (at most 50). None when there was no pause."""
    if not isinstance(doc, dict):
        return None
    periods = []
    for p in doc.get("periods") or []:
        if not isinstance(p, dict) or not parse(p.get("from")) or not parse(p.get("to")):
            continue
        seconds = _seconds(p.get("seconds"))
        if seconds is None:
            seconds = round(max(0.0, (parse(p["to"]) - parse(p["from"])).total_seconds()), 1)
        periods.append({"from": p["from"], "to": p["to"], "seconds": seconds})
    periods = periods[:50]
    count = max(_count(doc.get("count")) or 0, len(periods))
    if not count:
        return None
    total = _seconds(doc.get("seconds"))
    if total is None:
        total = round(sum(p["seconds"] for p in periods), 1)
    return {"count": count, "seconds": total, "periods": periods}


def duration_text(seconds: float) -> str:
    """"45 s", "3 min 21 s", "1 h 2 min"."""
    s = int(round(seconds))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min" + (f" {s % 60} s" if s % 60 else "")
    return f"{s // 3600} h" + (f" {s % 3600 // 60} min" if s % 3600 // 60 else "")


def paused_text(data) -> str | None:
    """"paused once, for 45 s" / "paused 3 times, for 3 min 21 s in all", or None."""
    paused = (data or {}).get("paused")
    if not paused or not paused.get("count"):
        return None
    n = paused["count"]
    times = {1: "once", 2: "twice"}.get(n, f"{n} times")
    return f"paused {times}, for {duration_text(paused.get('seconds') or 0)}" + (" in all" if n > 1 else "")


def _count(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def clean_exclusions(doc) -> dict | None:
    """`local_exclusions` (contract 1.5) in the stored form: the count, the entries' names (each
    at most 80 characters, at most 50), the list's length and whether it was managed."""
    if not isinstance(doc, dict):
        return None
    names = sorted({" ".join(n.split())[:80] for n in doc.get("names") or [] if isinstance(n, str) and n.strip()})[:50]
    count = _count(doc.get("count"))
    if count is None:
        count = len(names)
    return {"count": max(count, len(names)), "names": names, "entries": _count(doc.get("entries")) or 0,
            "managed": doc.get("managed") is True}


def exclusions_text(data) -> str | None:
    """"2 applications excluded on this PC (Microsoft Excel, Microsoft Word)", or None."""
    local = (data or {}).get("local_exclusions")
    if not local or not local.get("count"):
        return None
    n = local["count"]
    text = f"{n} application{'s' if n != 1 else ''} excluded on this PC"
    if local.get("names"):
        shown = local["names"][:5]
        more = len(local["names"]) - len(shown)
        text += " (" + ", ".join(shown) + (f" and {more} more" if more > 0 else "") + ")"
    return text + (" by the office's policy" if local.get("managed") else "")


def clean_preflight(doc) -> dict | None:
    if not isinstance(doc, dict) or not isinstance(doc.get("ready"), bool):
        return None
    checks = [{"name": c["name"][:80], "ok": c["ok"], "detail": _text(c.get("detail"), 300) or ""}
              for c in doc.get("checks") or []
              if isinstance(c, dict) and isinstance(c.get("name"), str) and c["name"] and isinstance(c.get("ok"), bool)]
    return {"checks": checks[:100], "ready": doc["ready"]}


def parse(value):
    """An ISO-8601 time as an aware UTC datetime, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return at.astimezone(timezone.utc) if at.tzinfo else None


def clock(value) -> str:
    at = parse(value)
    return at.strftime("%H:%M") if at else str(value or "")


def gap_text(gaps) -> str:
    return ", ".join(f"{clock(g['from'])}–{clock(g['to'])}" for g in gaps)


def channel_text(name, item) -> str:
    """`desktop interrupted 10:02–10:05`, `mainframe missing`, `Web complete`."""
    label = CHANNEL_NAMES.get(name, name)
    status = item["status"].replace("_", " ")
    if item["status"] == "interrupted" and item["gaps"]:
        return f"{label} interrupted {gap_text(item['gaps'])}"
    return f"{label} {status}"


def summary(data) -> str:
    """One sentence: "Web complete; desktop interrupted 10:02–10:05; network evidence incomplete"."""
    if data is None:
        return UNKNOWN_OLDER
    parts = [channel_text(name, data["channels"][name]) for name in CHANNELS
             if name in data["channels"] and name != "network" and data["channels"][name]["status"] != "not_requested"]
    if data["network_evidence"] != "none" or "network" in data["channels"]:
        parts.append(f"network evidence {data['network_evidence']}")
    excluded = exclusions_text(data)
    if excluded:
        parts.append(excluded)
    paused = paused_text(data)
    if paused:
        parts.append(paused)
    text = "; ".join(parts) or f"Recording {data['overall']}"
    return text[0].upper() + text[1:]


def label(data, accepted=False) -> str:
    """The recording's state in a few words, for lists."""
    if data is None:
        return "Completeness unknown"
    if data["overall"] == "complete":
        return "Complete"
    word = data["overall"].capitalize()
    return f"{word}: accepted as it is" if accepted else f"{word}: needs attention"


def needs_attention(data, accepted=False) -> bool:
    """A partial or failed recording needs attention until its owner accepts it."""
    return data is not None and data["overall"] in ATTENTION and not accepted


def ranges(data, started, ended) -> list[dict]:
    """Per channel with gaps, the affected time ranges as percentages of the session, for the
    timeline: [{"channel", "status", "gaps": [{"left", "width", "text", "reason"}]}]."""
    if data is None:
        return []
    start, end = parse(started), parse(ended)
    span = (end - start).total_seconds() if start and end and end > start else None
    out = []
    for name in CHANNELS:
        item = data["channels"].get(name)
        if not item or item["status"] == "not_requested":
            continue
        bars = []
        for gap in item["gaps"]:
            a, b = parse(gap["from"]), parse(gap["to"])
            left = width = None
            if span and a and b:
                left = max(0.0, min(100.0, (a - start).total_seconds() / span * 100))
                right = max(left, min(100.0, (b - start).total_seconds() / span * 100))
                left, width = round(left, 2), round(max(right - left, 0.5), 2)
            bars.append({"left": left, "width": width, "text": f"{clock(gap['from'])}–{clock(gap['to'])}",
                         "reason": gap["reason"]})
        if item["status"] == "missing" and not bars:
            bars.append({"left": 0, "width": 100, "text": "whole session", "reason": "not recorded"})
        out.append({"channel": name, "label": CHANNEL_NAMES[name].capitalize(), "status": item["status"], "gaps": bars,
                    "notes": item["notes"]})
    return out


def in_gap(data, captured_at, channel=None) -> bool:
    """Whether an event's capture time falls inside a reported gap of its channel (of any
    channel when the event's channel is not known)."""
    at = parse(captured_at)
    if data is None or at is None:
        return False
    for name, item in data["channels"].items():
        if channel and name != channel:
            continue
        for gap in item["gaps"]:
            a, b = parse(gap["from"]), parse(gap["to"])
            if a and b and a <= at <= b:
                return True
    return False
