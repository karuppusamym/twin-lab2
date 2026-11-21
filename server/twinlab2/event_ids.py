"""Event ids: what everything the portal keeps about a recorded event refers to.

A recorder of contract 1.1 assigns each event an `event_id` (a UUIDv7) when it captures it, and
never changes it: finalisation, crash recovery and reordering may recompute `seq`, the display
order, but not the id. Evidence selections, suggestions, scenario links and build-pack events
therefore refer to `event_id`, never to a position or a `seq`.

An event from an older recorder (contract 1.0.x) or from an imported steps.jsonl without ids gets
an id derived once, when it is first received: a UUIDv5 of the recording and the event's number
at that time. It is stored with the event, and a resend of the same recording reuses the stored
id (`stored_ids`), so the id never changes afterwards either.

`migrate` gives the rows stored before event ids existed their ids, in place: recordings'
events, recorded flows, evidence selections and suggestions (lists of numbers) and scenario links
(`recording#number`). It runs at start-up and does nothing once every row has its id.
"""

from __future__ import annotations

import json
import re
import uuid

# The namespace of derived ids (a fixed random UUID: never change it, or stored ids no longer match).
NAMESPACE = uuid.UUID("6f1c3b0e-8d2a-4b7e-9a51-2c4d9e7f0a13")
EVENT_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def derived(recording_id: str, number: int) -> str:
    """The id of an event that arrived without one: stable for the recording and its number."""
    return str(uuid.uuid5(NAMESPACE, f"{recording_id}#{number}"))


def clean(value) -> str | None:
    """A received event id in canonical form, or None."""
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    return value if EVENT_ID.match(value) else None


def is_derived(event_id: str) -> bool:
    """Whether the portal derived the id (an older recorder) rather than the recorder assigning it."""
    return len(event_id) == 36 and event_id[14] == "5"


def _number(step):
    """The number an older recorder gave a step (`n`), or an imported event (`seq`)."""
    value = step.get("n", step.get("seq"))
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def stored_ids(db, recording_id: str) -> dict:
    """{number: event_id} of the steps already stored for a recording: a resend from an older
    recorder reuses these instead of deriving again."""
    found = {}
    for row in db.execute("SELECT flow FROM recording_apps WHERE recording_id = ?", (recording_id,)):
        for step in json.loads(row["flow"] if hasattr(row, "keys") else row[0]):
            if _number(step) is not None and clean(step.get("event_id")):
                found.setdefault(_number(step), step["event_id"])
    row = db.execute("SELECT events FROM recordings WHERE id = ?", (recording_id,)).fetchone()
    for event in json.loads(row[0]) if row else []:
        if _number(event) is not None and clean(event.get("event_id")):
            found.setdefault(_number(event), event["event_id"])
    return found


def assign(items, recording_id: str, known: dict | None = None) -> list:
    """`items` (steps or events) with an `event_id` each: their own, else the stored one for their
    number, else one derived now. Changes the dicts in place and returns them."""
    known = known or {}
    for item in items:
        if clean(item.get("event_id")):
            item["event_id"] = clean(item["event_id"])
            continue
        number = _number(item)
        item["event_id"] = known.get(number) or derived(recording_id, number if number is not None else -1)
    return items


# --- the migration ---------------------------------------------------------------------------

REF = re.compile(r"^(?P<recording>[^#\s]{1,200})#(?P<number>\d{1,12})$")


def migrate(db) -> dict:
    """Give every stored event its id and move numeric references to ids. Returns the counts
    changed per table (all zero once migrated)."""
    changed = {"recordings": 0, "recording_apps": 0, "evidence": 0, "scenarios": 0}
    # portable SQL only: the tables exist (portal.create_app runs the other migrations first)
    ids = {}  # recording_id -> {number: event_id}
    with db:
        for rec_id, events in db.execute("SELECT id, events FROM recordings").fetchall():
            events = json.loads(events)
            mapping = ids.setdefault(rec_id, {})
            missing = [e for e in events if not clean(e.get("event_id"))]
            assign(events, rec_id)
            for event in events:
                if _number(event) is not None:
                    mapping.setdefault(_number(event), event["event_id"])
            if missing:
                db.execute("UPDATE recordings SET events = ? WHERE id = ?", (json.dumps(events), rec_id))
                changed["recordings"] += 1
        rows = db.execute("SELECT recording_id, app_id, key, flow FROM recording_apps").fetchall()
        for rec_id, app_id, key, flow in rows:
            steps = json.loads(flow)
            if all(clean(s.get("event_id")) for s in steps):
                continue
            assign(steps, rec_id, ids.get(rec_id, {}))
            db.execute("UPDATE recording_apps SET flow = ? WHERE recording_id = ? AND app_id = ? AND key = ?",
                       (json.dumps(steps), rec_id, app_id, key))  # fmt: skip
            changed["recording_apps"] += 1

        def to_id(rec_id, value):
            if isinstance(value, int) and not isinstance(value, bool):
                return ids.get(rec_id, {}).get(value) or derived(rec_id, value)
            return value

        for app_id, rec_id, selection, suggested in db.execute(
                "SELECT app_id, recording_id, selection, suggested FROM evidence").fetchall():
            values = [json.loads(selection), json.loads(suggested)]
            if not any(isinstance(v, int) for lst in values for v in lst):
                continue
            moved = [json.dumps(sorted({to_id(rec_id, v) for v in lst})) for lst in values]
            db.execute("UPDATE evidence SET selection = ?, suggested = ? WHERE app_id = ? AND recording_id = ?",
                       (*moved, app_id, rec_id))  # fmt: skip
            changed["evidence"] += 1
        for scenario_id, events in db.execute("SELECT id, events FROM scenarios").fetchall():
            refs = json.loads(events)
            moved = []
            for ref in refs:
                match = REF.match(ref) if isinstance(ref, str) else None
                if match:
                    rec_id = match["recording"]
                    ref = f"{rec_id}#{to_id(rec_id, int(match['number']))}"
                moved.append(ref)
            if moved != refs:
                db.execute("UPDATE scenarios SET events = ? WHERE id = ?", (json.dumps(moved), scenario_id))
                changed["scenarios"] += 1
    return changed
