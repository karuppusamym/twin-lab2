"""How complete a recording is, as a recorder of contract 1.1 reports it, and what the pages say.

A recorder reports `completeness` per channel (web, desktop, mainframe, network): complete,
interrupted (with the gaps, from and to), missing, or not requested, plus whether the network
evidence is complete, whether the recording was recovered after the recorder stopped
unexpectedly and how many seconds may be lost. A stopped recording is **not** complete by
itself: only `overall: complete` is. A partial or failed recording needs attention until it is
re-recorded or its owner accepts it as it is (`needs_attention`).

An older recorder (contract 1.0.x) reports nothing: its completeness is unknown, which is shown as
such and does not hold up the journey.
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
    return {
        "overall": doc["overall"], "channels": channels,
        "network_evidence": doc.get("network_evidence") if doc.get("network_evidence") in NETWORK else "none",
        "recovered": doc.get("recovered") is True,
        "loss_window_seconds": loss if isinstance(loss, (int, float)) and not isinstance(loss, bool) and loss >= 0 else None,
    }


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
