"""Completeness in separate dimensions (recorder contract 1.8).

One word for a recording ("partial") hides what is missing. Contract 1.8 adds
`completeness.dimensions`, each judged on its own:

- `channels`: channel availability. Did every requested collector (web, desktop, mainframe,
  network) capture the whole session?
- `event_detail`: event-detail completeness. How many recorded events kept their detail (the
  step), and how many are kept only as an `incomplete` marker (the recorder stopped before the
  page or the desktop batch settled)?
- `screen_coverage`: screen/state coverage. How many steps of the fully recorded applications
  belong to a screen state in `screens`? A step outside any state gives a rebuild no structure.
- `network_evidence`: network evidence availability. Is there a HAR or network journal for the
  web recordings, and was it whole?
- `owner_scope`: owner-reviewed scope. Did the application owner decide the scope of every
  application in the recording (replicate or not), or accept the recording as it is? Only the
  portal knows this: a recorder never sends it.

Each dimension is `{"status", "detail", "have"?, "of"?}`. `status` is one of `complete`,
`partial`, `none`, `not_applicable` (nothing of that kind was asked for) or `unknown` (an
older recorder did not say). `have` of `of` are the counts behind it, when there are any.

`derive(completeness)` gives the dimensions an older completeness (contract 1.1 to 1.7) implies:
channels and network evidence from what it reported, event detail and screen coverage unknown.
`clean(doc)` keeps the known fields of what a recorder sent.
"""

from __future__ import annotations

KEYS = ("channels", "event_detail", "screen_coverage", "network_evidence", "owner_scope")
RECORDER_KEYS = KEYS[:4]
TITLES = {
    "channels": "Channel availability",
    "event_detail": "Event detail",
    "screen_coverage": "Screen and state coverage",
    "network_evidence": "Network evidence",
    "owner_scope": "Owner-reviewed scope",
}
STATUSES = ("complete", "partial", "none", "not_applicable", "unknown")
DETAIL_MAX = 300
# how good a dimension is, for "the worse wins" and for the validator (higher is better)
RANK = {"none": 0, "unknown": 1, "partial": 2, "complete": 3, "not_applicable": 3}


def _count(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def ratio(have: int, of: int) -> str:
    """complete when all, none when nothing, partial in between; not_applicable when `of` is 0."""
    if of <= 0:
        return "not_applicable"
    if have >= of:
        return "complete"
    return "none" if have <= 0 else "partial"


def item(status: str, detail: str, have: int | None = None, of: int | None = None) -> dict:
    out = {"status": status if status in STATUSES else "unknown", "detail": " ".join(str(detail or "").split())[:DETAIL_MAX]}
    if _count(have) is not None and _count(of) is not None:
        out["have"], out["of"] = have, of
    return out


def clean(doc, keys=RECORDER_KEYS) -> dict | None:
    """The dimensions a recorder sent, known fields only, or None."""
    if not isinstance(doc, dict):
        return None
    out = {}
    for key in keys:
        d = doc.get(key)
        if isinstance(d, dict) and d.get("status") in STATUSES:
            out[key] = item(d["status"], d.get("detail") if isinstance(d.get("detail"), str) else "",
                            _count(d.get("have")), _count(d.get("of")))
    return out or None


def channels_of(completeness: dict) -> dict:
    """Channel availability from the per-channel statuses of any 1.1+ completeness."""
    channels = completeness.get("channels") or {}
    want = {k: v.get("status") for k, v in channels.items()
            if isinstance(v, dict) and v.get("status") not in (None, "not_requested")}
    good = [k for k, s in want.items() if s == "complete"]
    status = ratio(len(good), len(want))
    if status == "none" and any(s == "interrupted" for s in want.values()):
        status = "partial"  # an interrupted channel still captured part of the session
    bad = [f"{k} {s}" for k, s in sorted(want.items()) if s != "complete"]
    detail = (f"{len(good)} of {len(want)} requested channels captured the whole session"
              + (f" ({', '.join(bad)})" if bad else "")) if want else "no channel was requested"
    return item(status, detail, len(good), len(want))


def network_of(completeness: dict) -> dict:
    """Network evidence availability from `network_evidence` and the network channel."""
    net = (completeness.get("channels") or {}).get("network") or {}
    if net.get("status") in (None, "not_requested") and completeness.get("network_evidence") in (None, "none"):
        return item("not_applicable", "no web application was recorded, so no network evidence was asked for")
    word = completeness.get("network_evidence")
    if word == "complete":
        return item("complete", "the network journal covers the whole web recording")
    if word == "incomplete":
        return item("partial", "network evidence covers only part of the web recording")
    return item("none", "no network evidence was kept for the web recording")


def derive(completeness: dict | None) -> dict:
    """Every recorder dimension, from what a completeness says: its own `dimensions` (1.8)
    where it has them, else what the older fields imply, else unknown."""
    if not isinstance(completeness, dict):
        return {k: item("unknown", "the recorder did not report completeness") for k in RECORDER_KEYS}
    sent = clean(completeness.get("dimensions")) or {}
    out = {}
    for key in RECORDER_KEYS:
        if key in sent:
            out[key] = sent[key]
        elif key == "channels" and completeness.get("channels") is not None:
            out[key] = channels_of(completeness)
        elif key == "network_evidence" and completeness.get("channels") is not None:
            out[key] = network_of(completeness)
        else:
            out[key] = item("unknown", "an older recorder (before contract 1.8) does not report this")
    return out


def worst(items: list[dict]) -> dict:
    """Several recordings' dimension as one: the worse status wins, counts are added."""
    items = [i for i in items if isinstance(i, dict)]
    if not items:
        return item("unknown", "no recording")
    status = min((i["status"] for i in items), key=lambda s: RANK.get(s, 1))
    if status == "not_applicable" and any(i["status"] == "complete" for i in items):
        status = "complete"
    have = sum(i.get("have") or 0 for i in items) if all("have" in i for i in items) else None
    of = sum(i.get("of") or 0 for i in items) if all("of" in i for i in items) else None
    detail = next((i["detail"] for i in items if i["status"] == status), items[0]["detail"])
    if len(items) > 1:
        detail = f"{len(items)} recordings; the weakest: {detail}"
    return item(status, detail, have, of)
