"""Structured business scenarios of a demo app: what the copy must do, step by step.

A scenario has a name, starting conditions, actions (owner-written lines plus links to events
selected from recordings), the expected result, failure cases, synthetic example values,
unresolved questions and free notes. Scenarios replace the single free-text requirements field:
`import_requirements` moves any existing requirements text into the notes of a scenario so nothing is lost.
"""

import json
import re

# The table is created by migrations.py (baseline).

# The text fields of a scenario, the form label of each, and its size limit.
FIELDS = {
    "name": ("Scenario name", 200),
    "starting": ("Starting conditions", 4000),
    "actions": ("Actions (one per line)", 8000),
    "expected": ("Expected result", 4000),
    "failures": ("Failure cases (one per line)", 4000),
    "examples": ("Synthetic example values (one per line, made-up data only)", 4000),
    "questions": ("Unresolved questions (one per line)", 4000),
    "notes": ("Notes", 20000),
}
MIGRATED_NAME = "Requirements (migrated)"
EVENT_REF = re.compile(r"^(?P<recording>[^#\s]{1,200})#(?P<seq>\d{1,12})$")


def import_requirements(db):
    """Move each app's free-text requirements into a scenario's notes. The caller commits."""
    rows = db.execute("SELECT id, requirements FROM apps WHERE trim(requirements) != ''").fetchall()
    for app_id, text in rows:
        db.execute("INSERT INTO scenarios(app_id, position, name, notes) VALUES (?, ?, ?, ?)",
                   (app_id, next_position(db, app_id), MIGRATED_NAME, text.strip()))
        db.execute("UPDATE apps SET requirements = '' WHERE id = ?", (app_id,))


def next_position(db, app_id):
    row = db.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM scenarios WHERE app_id = ?", (app_id,)).fetchone()
    return row[0]


def lines(text):
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def event_ref(recording_id, seq):
    return f"{recording_id}#{seq}"


def from_form(form, allowed_events):
    """The scenario fields of a submitted form, or ValueError. `allowed_events` is the set of
    `recording#seq` references of events selected for the app."""
    values = {}
    for field, (label, limit) in FIELDS.items():
        value = (form.get(field) or "").replace("\r\n", "\n").strip()
        if len(value) > limit:
            raise ValueError(f"{label} is longer than {limit} characters.")
        values[field] = value
    if not values["name"]:
        raise ValueError("Name the scenario.")
    events = []
    for value in form.getlist("event"):
        if not EVENT_REF.match(value) or value not in allowed_events:
            raise ValueError("Link only events selected for this app.")
        if value not in events:
            events.append(value)
    values["events"] = json.dumps(events)
    return values


def load(db, app_id):
    rows = db.execute("SELECT * FROM scenarios WHERE app_id = ? ORDER BY position, id", (app_id,)).fetchall()
    return [dict(row, events=json.loads(row["events"])) for row in rows]


def save(db, app_id, values, scenario_id=None):
    with db:
        if scenario_id is None:
            return db.execute(
                f"INSERT INTO scenarios(app_id, position, {', '.join(values)}) VALUES (?, ?{', ?' * len(values)}) RETURNING id",
                (app_id, next_position(db, app_id), *values.values()),
            ).fetchone()[0]
        updated = db.execute(
            f"UPDATE scenarios SET {', '.join(f'{k} = ?' for k in values)} WHERE id = ? AND app_id = ?",
            (*values.values(), scenario_id, app_id),
        ).rowcount
        if updated != 1:
            raise LookupError(scenario_id)
        return scenario_id


def remove(db, app_id, scenario_id):
    with db:
        db.execute("DELETE FROM scenarios WHERE id = ? AND app_id = ?", (scenario_id, app_id))
