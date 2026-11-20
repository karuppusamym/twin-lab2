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

import redact
import scenarios

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
    """A selected event: its number and action, with the reviewed references of its flow step."""
    entry = {"seq": event["seq"], "action": event["action"]}
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


def evidence(db, app_id):
    """Per linked recording, its notes and the selected events with their references."""
    rows = db.execute("""
        SELECT recordings.id, recordings.name, recordings.events, evidence.selection, evidence.notes
        FROM evidence JOIN recordings ON recordings.id = evidence.recording_id
        WHERE evidence.app_id = ? ORDER BY recordings.id
    """, (app_id,)).fetchall()
    out = []
    for row in rows:
        steps = {}
        for flow in db.execute("SELECT flow FROM recording_apps WHERE recording_id = ? AND app_id = ? ORDER BY key",
                               (row["id"], app_id)):
            for step in json.loads(flow["flow"]):
                steps.setdefault(step.get("n"), step)
        selection = set(json.loads(row["selection"]))
        out.append({
            "recording_id": row["id"], "recording": row["name"], "notes": row["notes"],
            "events": [event_entry(event, steps.get(event["seq"]))
                       for event in json.loads(row["events"]) if event["seq"] in selection],
        })
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


def manifest(db, app_id):
    app = db.execute("SELECT id, name, git_url, status, surface FROM apps WHERE id = ?", (app_id,)).fetchone()
    linked = evidence(db, app_id)
    events = {scenarios.event_ref(rec["recording_id"], ev["seq"]): ev for rec in linked for ev in rec["events"]}
    items = [scenario_entry(s, events) for s in scenarios.load(db, app_id)]
    used = {ref for s in scenarios.load(db, app_id) for ref in s["events"]}
    unlinked = sorted(set(events) - used)
    attachments, files, report = [], {}, []
    for row in db.execute("SELECT * FROM attachments WHERE app_id = ? ORDER BY id", (app_id,)):
        path = f"attachments/{row['id']}{row['extension']}"
        share = row["share"] if "share" in row.keys() else "restricted"
        content, outcome, reasons = redact.attachment(bytes(row["content"]), row["extension"], share)
        if outcome != "included":
            report.append({"item": f"Attachment {row['id']}: {row['title']}", "kind": row["kind"],
                           "path": path, "outcome": outcome, "reasons": reasons})
        if content is None:
            continue
        files[path] = content
        attachments.append({"path": path, "title": redact.redact_text(row["title"]), "kind": row["kind"],
                            "share": share, "sha256": hashlib.sha256(content).hexdigest()})
    data = {
        "format": FORMAT, "app": dict(app), "scenarios": items, "evidence": linked, "attachments": attachments,
        "unresolved_questions": [f"Selected event {ref} is not part of any scenario." for ref in unlinked],
    }
    for section, label in (("scenarios", "Scenario text"), ("evidence", "Recorded steps and notes")):
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
            f"{e['recording_id']} #{e['seq']} {e['action']}"
            + "".join(f" · {k} `{e[k]}`" for k in ("screen", "control", "label", "key") if k in e)
            for e in s["events"]])
        if s["expected_result"]:
            out += ["**Expected result**", "", s["expected_result"], ""]
        bullets("Failure cases", s["failure_cases"])
        bullets("Synthetic example values", s["example_values"])
        bullets("Unresolved questions", s["unresolved_questions"])
        if s["notes"]:
            out += ["**Notes**", "", s["notes"], ""]
    if data["unresolved_questions"]:
        out += ["## Other unresolved questions", ""]
        out += [f"- {q}" for q in data["unresolved_questions"]] + [""]
    out += ["## Version endpoint", "",
            f"A server demo answers `GET /{version_path}` (relative to its deployed URL) with HTTP 200 "
            "and JSON `{\"commit\": \"<full 40-character Git commit it was built from>\"}`, so the "
            "catalog can verify which release a deployment runs.", ""]
    return "\n".join(out)


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
    data, attachments, report = manifest(db, app_id)
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
