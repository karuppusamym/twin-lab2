"""The build pack of a demo app: what a builder gets to make a repeatable copy of it.

The pack is a ZIP with `BUILD_TASK.md` (the scenarios, readable), `evidence.json` (the same in a
fixed schema, `format: 2`) and the reviewed attachments. Per selected event it carries the
reviewed references a recorder sent: the screen, the control and its label. Per scenario it
carries the starting conditions, actions with their linked events, the expected result, failure
cases, synthetic example values and an explicit list of unresolved questions.

Raw capture never enters a pack: the references payload a recorder sent (kept in
`session_refs`), typed values, URLs with queries or credentials and anything not in the flow
schema stay in the catalog database or on the recording machine. `build` reads only the
projected events, the flow labels and the owner-written scenarios.

From recorder contract 1.3 the pack also carries `screens`: per screen template of this
application, its distinct states (controls in page order with their identifiers, labels and
enabled/read-only/required/visible/checked states, regions shown or hidden, grids with column
headers and row-count shapes) and the transitions between them, limited to the states the owner's
selected events show (twinlab2_contract.structure). `BUILD_TASK.md` has the same as "Screens and
controls". A pack without structure has no `screens` key (older revisions keep their bytes).

A recording is named in a pack only by its id and the day it was recorded (`recorded_on`), never
by its free-text name: one recording can cover many applications, and its name ("Billing +
Payroll walkthrough") would tell each application's builder about the others.

Uploaded evidence is *restricted*: an attachment enters the pack only once the owner has marked
it reviewed or synthetic, and then only after `redact` has removed what it recognises (HAR
credentials and bodies, sensitive JSON fields, image metadata, e-mail addresses, card numbers,
…). Every string the pack carries passes the same text rules. `draft` returns, beside the pack,
the pre-freeze report: each item excluded or redacted, and why.

The archive is deterministic (sorted JSON, fixed ZIP timestamps), so the same catalog state
gives the same bytes; the portal stores each revision and serves the stored bytes.
"""

from collections import Counter
from dataclasses import dataclass, field
import hashlib
import io
import json
import re
from urllib.parse import urlsplit, urlunsplit
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from twinlab2 import artifacts
from twinlab2 import demo_target
from twinlab2 import redact
from twinlab2 import scenarios
from twinlab2_contract import structure

FORMAT = 2
TEXT_EXTENSIONS = {".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".sql", ".har"}
NAMED_KEY = re.compile(
    r"^(?:(?:Ctrl|Control|Alt|Shift|Win|Meta|Cmd)\+)*(?:Enter|Return|Tab|Esc|Escape|Backspace|Delete|Del|Insert"
    r"|Home|End|PageUp|PageDown|Page_Up|Page_Down|Up|Down|Left|Right|Arrow(?:Up|Down|Left|Right)|Space|F\d{1,2})$"
    r"|^(?:(?:Ctrl|Control|Alt|Win|Meta|Cmd)\+)+(?:Shift\+)?[A-Za-z0-9]$", re.I)


def screen_reference(value):
    """A screen reference without the parts that can carry data: for a URL, no credentials,
    query or fragment."""
    if not isinstance(value, str) or not value:
        return None
    if re.match(r"^[a-z][a-z0-9+.-]*://", value, re.I):
        parts = urlsplit(value)
        host = parts.hostname or ""
        if parts.port:
            host += f":{parts.port}"
        return urlunsplit((parts.scheme, host, parts.path, "", "")) or None
    return value.split("?", 1)[0] or None


def event_entry(event, step):
    """A selected event: its id, its display order and action, with the reviewed references of its
    flow step. `event_id` is the reference; `seq` only orders the events for reading."""
    entry = {"event_id": event["event_id"], "seq": event["seq"], "action": event["action"]}
    if step:
        screen = screen_reference(step.get("screen"))
        if screen:
            entry["screen"] = screen
        for field in ("control", "label"):
            if isinstance(step.get(field), str) and step[field]:
                entry[field] = step[field]
        if isinstance(step.get("key"), str) and NAMED_KEY.match(step["key"]):
            entry["key"] = step["key"]
    return entry


def recorded_on(db, recording_id, events):
    """The day (YYYY-MM-DD) a recording was made: the start the recorder sent, else the earliest
    capture time of its events, else the day in a workstation recording's id; None if unknown."""
    row = db.execute("SELECT payload FROM session_refs WHERE recording_id = ?", (recording_id,)).fetchone()
    if row is not None:
        try:
            started = (json.loads(row[0]).get("session") or {}).get("started")
        except (TypeError, ValueError):
            started = None
        if isinstance(started, str) and re.match(r"^\d{4}-\d{2}-\d{2}", started):
            return started[:10]
    times = sorted(e["time"] for e in events
                   if isinstance(e.get("time"), str) and re.match(r"^\d{4}-\d{2}-\d{2}", e["time"]))
    if times:
        return times[0][:10]
    m = re.match(r"^ws-(\d{4})(\d{2})(\d{2})T", recording_id)
    return f"{m[1]}-{m[2]}-{m[3]}" if m else None


def evidence(db, app_id):
    """Per linked recording, its notes and the selected events with their references. The
    recording is named by its id and `recorded_on`, not by its name (see the module)."""
    rows = db.execute("""
        SELECT recordings.id, recordings.events, evidence.selection, evidence.notes
        FROM evidence JOIN recordings ON recordings.id = evidence.recording_id
        WHERE evidence.app_id = ? ORDER BY recordings.id
    """, (app_id,)).fetchall()
    out = []
    for row in rows:
        steps = {}
        for flow in db.execute("SELECT flow FROM recording_apps WHERE recording_id = ? AND app_id = ? ORDER BY key",
                               (row["id"], app_id)):
            for step in json.loads(flow["flow"]):
                steps.setdefault(step.get("event_id"), step)
        selection = set(json.loads(row["selection"]))
        events = json.loads(row["events"])
        out.append({
            "recording_id": row["id"], "recorded_on": recorded_on(db, row["id"], events), "notes": row["notes"],
            "events": [event_entry(event, steps.get(event["event_id"]))
                       for event in events if event["event_id"] in selection],
        })
    return out


def screen_structure(db, app_id):
    """The screens of this application's linked recordings that its selected events show, one
    entry per screen template across recordings (states by content). Only the application's own
    rows are read: a recording's screens were assigned per application when it arrived."""
    merged = []
    for link in db.execute("SELECT recording_id, selection FROM evidence WHERE app_id = ? ORDER BY recording_id",
                           (app_id,)).fetchall():  # fmt: skip
        selected = set(json.loads(link["selection"]))
        mine = []
        for row in db.execute("SELECT screens FROM recording_apps WHERE recording_id = ? AND app_id = ? ORDER BY key",
                              (link["recording_id"], app_id)):  # fmt: skip
            mine = structure.merge(mine, json.loads(row["screens"] or "[]"))
        merged = structure.merge(merged, structure.select(mine, selected))
    out = []
    for sc in merged:
        screen = screen_reference(sc["screen"]) or sc["screen"]
        out.append(dict(sc, screen=screen, transitions=[dict(t, to=screen_reference(t["to"]) or t["to"])
                                                         for t in sc["transitions"]]))  # fmt: skip
    return out


def preview(db, recording_id, limit=10):
    """Per application linked to a recording, what its evidence would put in the build pack:
    the selected events (the first `limit`, with their capture time when known) and counts."""
    row = db.execute("SELECT events FROM recordings WHERE id = ?", (recording_id,)).fetchone()
    events = json.loads(row[0]) if row else []
    out = []
    for link in db.execute("""
        SELECT evidence.app_id, apps.name, evidence.selection, evidence.suggested, evidence.reviewed
        FROM evidence JOIN apps ON apps.id = evidence.app_id WHERE evidence.recording_id = ? ORDER BY apps.name
    """, (recording_id,)).fetchall():
        steps = {}
        for flow in db.execute("SELECT flow FROM recording_apps WHERE recording_id = ? AND app_id = ? ORDER BY key",
                               (recording_id, link["app_id"])):
            for step in json.loads(flow["flow"]):
                steps.setdefault(step.get("event_id"), step)
        selection = set(json.loads(link["selection"]))
        chosen = [dict(event_entry(e, steps.get(e["event_id"])), time=e.get("time"), channel=e.get("channel"))
                  for e in events if e["event_id"] in selection]
        out.append({"app_id": link["app_id"], "name": link["name"], "reviewed": bool(link["reviewed"]),
                    "count": len(chosen), "suggested": len(json.loads(link["suggested"])), "events": chosen[:limit]})
    return out


def scenario_entry(scenario, events):
    linked = [dict(events[ref], recording_id=ref.split("#", 1)[0]) for ref in scenario["events"] if ref in events]
    questions = scenarios.lines(scenario["questions"])
    gaps = []
    if not scenario["expected"]:
        gaps.append("The expected result of this scenario is not stated.")
    if not linked:
        gaps.append("No recorded event shows this scenario yet.")
    for event in linked:
        if "screen" not in event and "control" not in event:
            gaps.append(f"Event {event['seq']} of {event['recording_id']} has no recorded screen or control reference.")
    for ref in scenario["events"]:
        if ref not in events:
            gaps.append(f"Linked event {ref} is no longer selected for this app.")
    return {
        "name": scenario["name"], "starting_conditions": scenario["starting"],
        "actions": scenarios.lines(scenario["actions"]), "events": linked,
        "expected_result": scenario["expected"], "failure_cases": scenarios.lines(scenario["failures"]),
        "example_values": scenarios.lines(scenario["examples"]),
        "unresolved_questions": questions + gaps, "notes": scenario["notes"],
    }


def manifest(db, app_id, version_path=".twinlab/version"):
    app = db.execute("SELECT id, name, git_url, status, surface FROM apps WHERE id = ?", (app_id,)).fetchone()
    app_row = db.execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone()
    linked = evidence(db, app_id)
    events = {scenarios.event_ref(rec["recording_id"], ev["event_id"]): ev for rec in linked for ev in rec["events"]}
    items = [scenario_entry(s, events) for s in scenarios.load(db, app_id)]
    used = {ref for s in scenarios.load(db, app_id) for ref in s["events"]}
    unlinked = sorted(set(events) - used)
    attachments, files, report = [], {}, []
    for row in db.execute("SELECT * FROM attachments WHERE app_id = ? ORDER BY id", (app_id,)):
        path = f"attachments/{row['id']}{row['extension']}"
        share = row["share"] if "share" in row.keys() else "restricted"
        content, outcome, reasons = redact.attachment(artifacts.content(db, row, "content"), row["extension"], share)
        if outcome != "included":
            report.append({"item": f"Attachment {row['id']}: {row['title']}", "kind": row["kind"],
                           "path": path, "outcome": outcome, "reasons": reasons})
        if content is None:
            continue
        files[path] = content
        attachments.append({"path": path, "title": redact.redact_text(row["title"]), "kind": row["kind"],
                            "share": share, "sha256": hashlib.sha256(content).hexdigest()})
    target, deploy_files = demo_target.plan(db, app_row, version_path, {a["path"]: files[a["path"]] for a in attachments})
    target["data_model"]["tables"] = [redact.redact_text(t) for t in target["data_model"]["tables"]]
    files.update(deploy_files)
    app = dict(app)
    if "://" in (app.get("git_url") or ""):  # never a credential in the address: `https://bot:token@host/...`
        app["git_url"] = demo_target.public_git_url(app["git_url"])
    data = {
        "format": FORMAT, "app": app, "scenarios": items, "evidence": linked, "attachments": attachments,
        "target": target,
        "unresolved_questions": [f"Selected event {ref} is not part of any scenario." for ref in unlinked],
    }
    screens = screen_structure(db, app_id)
    if screens:
        data["screens"] = screens
    for section, label in (("scenarios", "Scenario text"), ("evidence", "Recorded steps and notes"),
                           ("screens", "Screen structure")):
        if section not in data:
            continue
        found = Counter()
        data[section] = redact_strings(data[section], found)
        if found:
            report.append({"item": f"{label} in evidence.json", "kind": "text", "path": "evidence.json",
                           "outcome": "redacted", "reasons": redact.describe(found)})
    return data, files, report


def redact_strings(value, found):
    """Every string in the value through the text rules (keys are the pack's own schema)."""
    if isinstance(value, dict):
        return {k: redact_strings(v, found) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_strings(v, found) for v in value]
    if isinstance(value, str):
        return redact.redact_text(value, found)
    return value


def task_markdown(data, revision, version_path):
    out = [f"# Build {data['app']['name']}", "", f"Brief revision `{revision}`", ""]
    out += ["Build a repeatable copy of this application for the business scenarios below. "
            "`evidence.json` has the same scenarios with the recorded events (screen, control and label), "
            "the reviewed recording notes and the attachments. Use only the synthetic example values: "
            "the pack contains no real captured data. Record assumptions and how you settled each unresolved "
            "question in BUILD-NOTES.md. Provide a local launcher, synthetic seed/reset steps, and a smoke "
            "check per scenario. Commit the source and run instructions to the demo repo.", ""]

    def bullets(title, values):
        if values:
            out.extend([f"**{title}**", "", *[f"- {v}" for v in values], ""])

    for number, s in enumerate(data["scenarios"], 1):
        out += [f"## Scenario {number}: {s['name']}", ""]
        if s["starting_conditions"]:
            out += ["**Starting conditions**", "", s["starting_conditions"], ""]
        bullets("Actions", s["actions"])
        bullets("Recorded events", [
            f"{e['recording_id']} #{e['seq']} {e['action']} (event `{e['event_id']}`)"
            + "".join(f" · {k} `{e[k]}`" for k in ("screen", "control", "label", "key") if k in e)
            for e in s["events"]])
        if s["expected_result"]:
            out += ["**Expected result**", "", s["expected_result"], ""]
        bullets("Failure cases", s["failure_cases"])
        bullets("Synthetic example values", s["example_values"])
        bullets("Unresolved questions", s["unresolved_questions"])
        if s["notes"]:
            out += ["**Notes**", "", s["notes"], ""]
    if data.get("screens"):
        out += screens_markdown(data)
    if data.get("target"):
        out += demo_target.markdown(data["target"])
    if data["unresolved_questions"]:
        out += ["## Other unresolved questions", ""]
        out += [f"- {q}" for q in data["unresolved_questions"]] + [""]
    out += ["## Version endpoint", "",
            f"A server demo answers `GET /{version_path}` (relative to its deployed URL) with HTTP 200 "
            "and JSON `{\"commit\": \"<full 40-character Git commit it was built from>\"}`, so the "
            "catalog can verify which release a deployment runs.", ""]
    return "\n".join(out)


REPLAY_NOTE = (
    "After every recorded step an independent replay compares the rebuilt screen with the recorded one: the "
    "interactive elements (tag, id and classes) and how many there are of each, whether each element with an "
    "id (or a control with a name) is shown, which controls are enabled, the option lists of selects, and the "
    "value format of fields. Build each state below with exactly these controls, identifiers, classes and "
    "states, and no other interactive elements. Text and data are not compared; rows are given as a shape "
    "(0, 1, few = 2 to 9, many = 10 or more). Words that could be data were left out (counted as withheld).")


def _control_line(c):
    if "row" in c:
        ident = f"field at row {c['row']}, column {c.get('col', '?')}, length {c.get('len', '?')}"
    else:
        ident = (c.get("tag") or c["kind"]) + (f"#{c['id']}" if c.get("id") else "")
        ident += "".join(f".{x}" for x in c.get("classes") or [])
        for k in ("name", "type"):
            if c.get(k):
                ident += f' {k}="{c[k]}"'
    states = []
    if c.get("visible") is False:
        states.append("hidden")
    if c.get("enabled") is False:
        states.append("disabled")
    for k, word in (("readonly", "read-only"), ("required", "required"), ("checked", "checked"),
                    ("selected", "selected"), ("numeric", "numeric"), ("intensified", "intensified")):
        if c.get(k):
            states.append(word)
    if c.get("hidden") and "hidden" not in states:
        states.append("non-display")
    if c.get("value"):
        states.append(c["value"])
    if c.get("repeat"):
        states.append(f"one per row ({c['repeat']} rows)")
    if "options" in c:
        texts = c.get("option_texts")
        states.append(f"{c['options']} options" + (": " + ", ".join(f'"{t}"' for t in texts) if texts else ""))
    label = f' "{c["label"]}"' if c.get("label") else ""
    return f"- `{ident}` ({c['kind']}){label}" + (f": {'; '.join(states)}" if states else "")


def _region(r):
    name = f"{r['kind']}#{r['id']}" if r.get("id") else f"{r['kind']}[name={r['name']}]"
    return "`" + name + "".join(f".{x}" for x in r.get("classes") or []) + "`"


def _state_name(screens, screen, state_id):
    for sc in screens:
        if sc["screen"] == screen:
            for i, st in enumerate(sc["states"], 1):
                if st["id"] == state_id:
                    return f"state {i}"
    return "a state"


def screens_markdown(data):
    """The "Screens and controls" section of BUILD_TASK.md."""
    seqs = {ev["event_id"]: f"{rec['recording_id']} #{ev['seq']}" for rec in data["evidence"] for ev in rec["events"]}
    out = ["## Screens and controls", "", REPLAY_NOTE, ""]
    for sc in data["screens"]:
        n = len(sc["states"])
        out += [f"### `{sc['screen']}` ({sc['surface']}, {n} state{'s' if n != 1 else ''})", ""]
        for i, st in enumerate(sc["states"], 1):
            after = ", ".join(seqs.get(e, f"event `{e}`") for e in st["events"][:8])
            more = f" and {len(st['events']) - 8} more" if len(st["events"]) > 8 else ""
            out += [f"**State {i}** (`{st['id']}`), after {after}{more}", ""]
            out += [_control_line(c) for c in st["controls"]] or ["- no controls"]
            shown = [_region(r) for r in st["regions"] if r.get("visible", True)]
            hidden = [_region(r) for r in st["regions"] if r.get("visible") is False]
            if shown:
                out.append("- shown: " + ", ".join(shown))
            if hidden:
                out.append("- hidden: " + ", ".join(hidden))
            for g in st["grids"]:
                name = g["kind"] + (f"#{g['id']}" if g.get("id") else "")
                cols = ", ".join(c or "(unnamed)" for c in g.get("columns") or []) or "no column headers"
                out.append(f"- grid `{name}`: columns {cols}; rows: {g['rows']}"
                           + ("" if g.get("visible", True) else "; hidden"))
            if st.get("withheld"):
                out.append(f"- withheld as possible data: {st['withheld']}")
            out.append("")
        if sc["transitions"]:
            out += ["**Transitions**", ""]
            for t in sc["transitions"]:
                via = " ".join(str(t[k]) for k in ("action", "control") if t.get(k)) or "a step"
                if t.get("label"):
                    via += f" `{t['label']}`"
                if t.get("key"):
                    via += f" key {t['key']}"
                to = _state_name(data["screens"], t["to"], t["to_state"])
                if t["to"] != sc["screen"]:
                    to = f"`{t['to']}` {to}"
                times = f" ({t['count']} times)" if t.get("count", 1) > 1 else ""
                out.append(f"- from {_state_name(data['screens'], sc['screen'], t['from_state'])}: {via} → {to}{times}")
            out.append("")
    return out


@dataclass
class Draft:
    """A pack built from the catalog's current state, not yet frozen."""
    revision: str  # SHA-256 of evidence.json
    manifest: str  # evidence.json text
    files: dict  # path -> bytes, in archive order
    package: bytes  # the ZIP
    report: list = field(default_factory=list)  # items excluded or redacted, with reasons

    @property
    def sha256(self):
        return hashlib.sha256(self.package).hexdigest()

    @property
    def excluded(self):
        return [item for item in self.report if item["outcome"] == "excluded"]

    @property
    def redacted(self):
        return [item for item in self.report if item["outcome"] == "redacted"]

    def summary(self):
        """The report in one line: N items excluded, reasons; M items redacted."""
        reasons = Counter(reason for item in self.excluded for reason in item["reasons"])
        text = f"{len(self.excluded)} item{'s' if len(self.excluded) != 1 else ''} excluded"
        if reasons:
            text += ": " + "; ".join(f"{n} × {reason}" if n > 1 else reason for reason, n in reasons.items())
        text += f". {len(self.redacted)} item{'s' if len(self.redacted) != 1 else ''} redacted."
        return text


def draft(db, app_id, version_path):
    data, attachments, report = manifest(db, app_id, version_path)
    contents = json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2)
    revision = hashlib.sha256(contents.encode("utf-8")).hexdigest()
    files = {"BUILD_TASK.md": task_markdown(data, revision, version_path).encode("utf-8"),
             "evidence.json": contents.encode("utf-8"), **attachments}
    return Draft(revision, contents, files, archive(files), report)


def build(db, app_id, version_path):
    """(revision, manifest JSON text, {path: bytes} in archive order, archive bytes)."""
    d = draft(db, app_id, version_path)
    return d.revision, d.manifest, d.files, d.package


def archive(files):
    package = io.BytesIO()
    with ZipFile(package, "w", ZIP_STORED) as out:
        for path, content in files.items():
            info = ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            out.writestr(info, content)
    return package.getvalue()


def read(archive_bytes):
    """The files of a stored pack, in archive order, for the preview page."""
    with ZipFile(io.BytesIO(archive_bytes)) as package:
        return {name: package.read(name) for name in package.namelist()}


def is_text(path):
    return any(path.endswith(ext) for ext in TEXT_EXTENSIONS)
