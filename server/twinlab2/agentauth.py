"""What a recorder machine's token may do (scopes), and how often it may call (rate limit).

Credentials of the agent API (`/api/agent/*`, `/api/uploads*`; docs/AGENT-API-SECURITY.md):

- a **pairing code** (10 characters, one use, `registry.CODE_MINUTES` minutes): only
  `POST /api/agent/pair` takes it, never as a bearer token; wrong codes are limited per portal
  (`registry.FAILURES_PER_10_MIN`). It carries the scopes its machine will get.
- a **machine token** (from pairing): `Authorization: Bearer`, stored as SHA-256 only. Its scopes:
  `record` (the sessions offered to the machine, the applications to record, sending a
  recording's references) and `upload` (starting an evidence upload). A token without the
  scope gets 403 `{"error": ..., "scope": ...}`; the recorder shows it and stops (a person
  re-pairs the machine with a code that allows it).
- an **upload token** (per upload, from `POST /api/uploads`): the chunks, parts and completion
  of that one upload; it needs its machine to be paired, not revoked, and still to hold `upload`.

Every authenticated agent call counts against its machine's **rate limit**:
`TWINLAB2_AGENT_RATE` requests per minute (default 600; 0 turns it off), counted in the
database (`agent_rate`), so every replica shares it. Over it: 429 `{"error", "retry_after"}`
with `Retry-After`; the recorder's upload queue retries later with backoff.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os

from twinlab2.rest import Problem, db

SCOPES = ("record", "upload")
DEFAULT_SCOPES = "record upload"
WHAT = {"record": "receive recording sessions or send a recording's references",
        "upload": "upload evidence"}
DEFAULT_RATE = 600


def parse_scopes(values) -> str:
    """The stored form of a list of scopes (ValueError for an unknown or empty list)."""
    chosen = [s for s in SCOPES if s in set(values or ())]
    unknown = set(values or ()) - set(SCOPES)
    if unknown:
        raise ValueError(f"Unknown scope {sorted(unknown)[0]!r}: choose from {', '.join(SCOPES)}.")
    if not chosen:
        raise ValueError("A machine needs at least one scope.")
    return " ".join(chosen)


def scopes_of(row) -> list[str]:
    try:
        stored = row["scopes"]
    except (KeyError, IndexError):
        stored = None
    return (stored or DEFAULT_SCOPES).split()


def require_scope(row, scope):
    """403 (the agent API's error shape) unless the machine's token holds `scope`."""
    if scope not in scopes_of(row):
        raise Problem(403, body={"error": f"this machine's token may not {WHAT[scope]}: pair the machine again "
                                          f"with a code that allows {scope!r}", "scope": scope})


def rate() -> int:
    try:
        return max(0, int(os.environ.get("TWINLAB2_AGENT_RATE", "") or DEFAULT_RATE))
    except ValueError:
        return DEFAULT_RATE


def limit(machine_id, now=None):
    """Count one call of the machine; 429 once it is over the rate for this minute."""
    allowed = rate()
    if not allowed:
        return
    now = now or datetime.now(timezone.utc)
    minute = now.strftime("%Y-%m-%dT%H:%M")
    with db():
        hits = db().execute(
            "INSERT INTO agent_rate(machine_id, minute, hits) VALUES (?, ?, 1) "
            "ON CONFLICT(machine_id, minute) DO UPDATE SET hits = agent_rate.hits + 1 RETURNING hits",
            (machine_id, minute)).fetchone()[0]
        if hits == 1:  # the first call of a minute: forget the machine's older minutes
            old = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M")
            db().execute("DELETE FROM agent_rate WHERE machine_id = ? AND minute < ?", (machine_id, old))
    if hits > allowed:
        wait = 60 - now.second
        raise Problem(429, headers={"Retry-After": str(wait)},
                      body={"error": f"too many requests from this machine ({allowed} a minute): retry in {wait} s",
                            "retry_after": wait})
