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
from twinlab2 import baselines
from twinlab2 import demo_target
from twinlab2 import interfaces
from twinlab2 import pack_validator
from twinlab2 import redact
from twinlab2 import scenarios
from twinlab2_contract import package
from twinlab2_contract import structure

FORMAT = package.PACKAGE_FORMAT  # 3: bindings, visual baselines, confirmations, interfaces (2 stays readable)
TEXT_EXTENSIONS = {".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".sql", ".har"}
NAMED_KEY = re.compile(
    r"^(?:(?:Ctrl|Control|Alt|Shift|Win|Meta|Cmd)\+)*(?:Enter|Return|Tab|Esc|Escape|Backspace|Delete|Del|Insert"
    r"|Home|End|PageUp|PageDown|Page_Up|Page_Down|Up|Down|Left|Right|Arrow(?:Up|Down|Left|Right)|Space|F\d{1,2}"
    r"|PF\d{1,2}|PA[1-3]|Clear)$"
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
        elif step.get("action") == "aid" and step.get("control") == "key" and isinstance(step.get("label"), str) \
                and NAMED_KEY.match(step["label"]):
            # a recorder that keeps only Enter and F1-F12 as `key` still names a PF, PA or Clear AID by its label
            entry["key"] = step["label"]
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
        entry = {
            "recording_id": row["id"], "recorded_on": recorded_on(db, row["id"], events), "notes": row["notes"],
            "events": [event_entry(event, steps.get(event["event_id"]))
                       for event in events if event["event_id"] in selection],
        }
        found = capture(db, row["id"])
        if found:
            entry["capture"] = found
        out.append(entry)
    return out


def capture(db, recording_id):
    """The recording's completeness dimensions as statuses only (recorder contract 1.8; derived
    for an older recorder), for the validator: {} when the recording reported no completeness."""
    from twinlab2 import registry

    status = registry.recording_status(db, recording_id)
    if status["completeness"] is None:
        return {}
    return {d["id"]: d["status"] for d in status["dimensions"]}


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
    examples = scenarios.lines(scenario["examples"])
    fields = {f["field"]: f for f in package.fixture_fields(examples)}
    bindings = []
    for b in scenario.get("bindings") or []:
        if b["event"] not in events or b["event"] not in scenario["events"]:
            gaps.append(f"A binding names event {b['event']}, which is no longer linked.")
            continue
        entry = {"event_id": events[b["event"]]["event_id"], "recording_id": b["event"].split("#", 1)[0]}
        if b.get("field"):
            entry["field"] = b["field"]
            if b["field"] in fields:
                entry.update(type=fields[b["field"]]["type"], value=fields[b["field"]]["value"])
        else:
            entry["identifier"] = b["identifier"]
        bindings.append(entry)
    return {
        "name": scenario["name"], "starting_conditions": scenario["starting"],
        "fixture_fields": list(fields.values()), "bindings": bindings,
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
    toolkit = toolkit_of(db, app_id)
    if toolkit:
        app["toolkit"] = toolkit
    data = {
        "format": FORMAT, "app": app, "scenarios": items, "evidence": linked, "attachments": attachments,
        "target": target,
        "unresolved_questions": [f"Selected event {ref} is not part of any scenario." for ref in unlinked],
    }
    screens = screen_structure(db, app_id)
    if screens:
        data["screens"] = screens
    data["visual_baselines"], baseline_files = baselines.for_pack(db, app_id)
    files.update(baseline_files)
    data["interfaces"] = interfaces.for_pack(db, app_id)
    rows = [c.get("row") for sc in screens for st in sc.get("states") or [] for c in host_fields(st)]
    data["target"]["twin_label"] = package.twin_label(surface_of(data), rows)
    data["confirmations"] = [{"item": r["item"], "note": redact.redact_text(r["note"])} for r in db.execute(
        "SELECT item, note FROM item_confirmations WHERE app_id = ? ORDER BY item", (app_id,))]
    for section, label in (("scenarios", "Scenario text"), ("evidence", "Recorded steps and notes"),
                           ("screens", "Screen structure"), ("interfaces", "Interface contracts")):
        if section not in data:
            continue
        found = Counter()
        data[section] = redact_strings(data[section], found)
        if found:
            report.append({"item": f"{label} in evidence.json", "kind": "text", "path": "evidence.json",
                           "outcome": "redacted", "reasons": redact.describe(found)})
    return data, files, report


def toolkit_of(db, app_id):
    """The desktop toolkit (WinForms, WPF, …) the recorder detected for one of the application's
    identities (`app_identities.facts`), or None."""
    for row in db.execute("SELECT facts FROM app_identities WHERE app_id = ? ORDER BY key", (app_id,)):
        try:
            facts = json.loads(row[0] or "{}")
        except (TypeError, ValueError):
            continue
        value = facts.get("toolkit") if isinstance(facts, dict) else None
        if isinstance(value, str) and value:
            return redact.redact_text(value)[:40]
    return None


def redact_strings(value, found):
    """Every string in the value through the text rules (keys are the pack's own schema)."""
    if isinstance(value, dict):
        return {k: redact_strings(v, found) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_strings(v, found) for v in value]
    if isinstance(value, str):
        return redact.redact_text(value, found)
    return value


def task_markdown(data, revision, version_path, completeness=None):
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
        bullets("Bound values (the replay types these at these steps)", [
            f"event `{b['event_id']}` ← " + (f"field {b['field']}" + (f" ({b['type']}): `{b['value']}`" if "value" in b else
                                                                    " (no example line gives it)")
                                             if b.get("field") else f"shared identifier `{b['identifier']}`")
            for b in s.get("bindings") or []])
        bullets("Unresolved questions", s["unresolved_questions"])
        if s["notes"]:
            out += ["**Notes**", "", s["notes"], ""]
    if data.get("screens"):
        out += screens_markdown(data)
    elif surface_of(data) == "mainframe":
        out += host_screens_markdown(data)
    out += twin_label_markdown(data)
    out += baselines_markdown(data)
    out += interfaces.markdown(data)
    out += unrecorded_markdown(data)
    if data.get("target"):
        out += demo_target.markdown(data["target"])
    if completeness is not None:
        out += pack_validator.markdown(completeness, data.get("known_gaps"))
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
    "states, and no other interactive elements. Text and data are not compared (on a 3270 screen, except its "
    "title line); rows are given as a shape "
    "(0, 1, few = 2 to 9, many = 10 or more). Words that could be data were left out (counted as withheld). "
    "On a desktop screen, \"in `X`\" names the region a control or region is inside: nest it there, as the "
    "replay compares each control's chain of named containers.")

REPLAY_NOTE_3270 = (
    "On a 3270 screen the replay names the screen by its title line, all the protected text of its first non-empty "
    "row, and compares it exactly: put only the title given below on that row (no user id, date or other text "
    "beside it). After every step it also compares every input field by its row, column, length and attributes "
    "(numeric, non-display, intensified). Other protected text, typed data and the message line are not compared; "
    "where a layout table is given, put them where it says. Text that could be data is given as a shape "
    "(X a letter, 9 a digit).")

TITLE_ID = re.compile(r"^scr:[0-9a-f]+$")


def surface_of(data):
    """web, desktop or mainframe: the pack's target kind, else the application's surface."""
    return (data.get("target") or {}).get("kind") or (data.get("app") or {}).get("surface") or "web"


def host_fields(st):
    """A host state's fields with a position: its input controls and its protected fields. A
    recorder at contract 1.4 sends the protected fields as the state's `texts` (title, label,
    message and key line with their text, other protected text as a shape); they are given here
    as protected fields (`protected: true`, `text` or `shape`, `role` the text's kind)."""
    out = list(st.get("controls") or [])
    for t in st.get("texts") or []:
        if not all(isinstance(t.get(k), int) for k in ("row", "col", "len")):
            continue  # a desktop message box's text has no position
        f = {"kind": "field", "row": t["row"], "col": t["col"], "len": t["len"], "protected": True,
             "role": t.get("kind")}
        for k in ("text", "shape", "intensified", "hidden"):
            if t.get(k):
                f[k] = t[k]
        out.append(f)
    return out


def _messages(st):
    """The texts of a state that have no position: a desktop message box's message."""
    return [t["text"] for t in st.get("texts") or [] if t.get("text") and not isinstance(t.get("row"), int)]


def _title_line(sc):
    """The line that tells the builder which row holds the title of a host screen."""
    name = sc["screen"]
    if TITLE_ID.match(name):
        return ("- title: none recorded (this screen is known only by its layout id): put no protected text on "
                "its first rows that a scenario does not name")
    fields = [c for st in sc.get("states") or [] for c in host_fields(st)
              if c.get("protected") and c.get("text") and not c.get("hidden") and isinstance(c.get("row"), int)]
    titles = [c["row"] for c in fields if c.get("role") == "title"]
    rows = titles or [c["row"] for c in fields]
    row = min(rows) if rows else 1
    return f"- title (compared exactly): row {row} holds only `{name}`; put nothing else on that row"


def _ident(c):
    ident = (c.get("tag") or c["kind"]) + (f"#{c['id']}" if c.get("id") else "")
    ident += "".join(f".{x}" for x in c.get("classes") or [])
    for k in ("name", "type"):
        if c.get(k):
            ident += f' {k}="{c[k]}"'
    return ident


ROLE_WORDS = {"title": "title", "label": "label", "message": "message line", "keys": "key line",
              "text": "output"}


def _layout_table(controls):
    """The host screen layout: every field with a position, protected or not, in screen order."""
    out = ["| Row | Col | Length | Attribute | Text or shape |", "|---|---|---|---|---|"]
    placed = [c for c in controls if all(isinstance(c.get(k), int) for k in ("row", "col", "len"))]
    for c in sorted(placed, key=lambda c: (c["row"], c["col"])):
        attrs = ["protected" if c.get("protected") else "input"]
        attrs += [word for k, word in (("numeric", "numeric"), ("hidden", "non-display"),
                                       ("intensified", "intensified")) if c.get(k)]
        if c.get("protected"):
            what = (f"`{c['text']}`" if c.get("text") else f"shape `{c['shape']}`" if c.get("shape")
                    else "(withheld as possible data)")
            if c.get("role") in ROLE_WORDS:
                what = f"{ROLE_WORDS[c['role']]}: {what}"
        else:
            what = f"(input: {c['label']})" if c.get("label") else "(input)"
        out.append(f"| {c['row']} | {c['col']} | {c['len']} | {', '.join(attrs)} | {what} |")
    return out


def _control_line(c):
    if "row" in c:
        ident = f"field at row {c['row']}, column {c.get('col', '?')}, length {c.get('len', '?')}"
    else:
        ident = _ident(c)
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
    inside = f" in `{c['in']}`" if c.get("in") else ""  # contract 1.6: the region it is inside
    return f"- `{ident}` ({c['kind']}){label}{inside}" + (f": {'; '.join(states)}" if states else "")


def _region(r):
    inside = f" (in `{r['in']}`)" if r.get("in") else ""
    if not r.get("id") and not r.get("name") and r.get("label"):  # 1.6: named only by its caption
        return f'`{r["kind"]}` named "{r["label"]}" (no AutomationId)' + inside
    name = f"{r['kind']}#{r['id']}" if r.get("id") else f"{r['kind']}[name={r['name']}]"
    return "`" + name + "".join(f".{x}" for x in r.get("classes") or []) + "`" + inside


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
    surfaces = {sc.get("surface") for sc in data["screens"]}
    out = ["## Screens and controls", ""]
    if surfaces - {"mainframe"}:
        out += [REPLAY_NOTE, ""]
    if "mainframe" in surfaces:
        out += [REPLAY_NOTE_3270, ""]
    for sc in data["screens"]:
        n = len(sc["states"])
        host = sc.get("surface") == "mainframe"
        out += [f"### `{sc['screen']}` ({sc['surface']}, {n} state{'s' if n != 1 else ''})", ""]
        if host:
            out += [_title_line(sc), ""]
        for i, st in enumerate(sc["states"], 1):
            after = ", ".join(seqs.get(e, f"event `{e}`") for e in st["events"][:8])
            more = f" and {len(st['events']) - 8} more" if len(st["events"]) > 8 else ""
            out += [f"**State {i}** (`{st['id']}`), after {after}{more}", ""]
            inputs = [c for c in st["controls"] if not c.get("protected")]
            out += [_control_line(c) for c in inputs] or ["- no controls"]
            fields = host_fields(st) if host else []
            if host and len(inputs) < len(fields):
                out += ["", "Layout (every field with its position; protected text is the screen's own, text that "
                        "may be data is a shape):", ""]
                out += _layout_table(fields)
                out.append("")
            for message in _messages(st):
                out.append(f'- message (the application\'s own text, shown as recorded): "{message}"')
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


def twin_label_markdown(data):
    """The required "AI generated Twin App" label (twinlab2_contract.package)."""
    label = (data.get("target") or {}).get("twin_label")
    if not label:
        return []
    out = [f"## Required: the \"{label['text']}\" label", "",
           "Every screen of the copy shows that it is an AI generated twin, so nobody takes it for the original. "
           "This is not optional and not part of the recorded screens: Verify checks it on every replayed screen, "
           "leaves it out of the structural and visual comparisons, and a release without it cannot become Tested. "
           "Do not remove it, hide it or make it conditional.", ""]
    for surface, spec in label["surfaces"].items():
        for key, text in spec.items():
            out.append(f"- {surface} {key.replace('_', ' ')}: " + (f"`{text}`" if key in ("meta", "title_suffix") else str(text)))
    return out + [""]


def baselines_markdown(data):
    """The "Visual baselines" section: which state each approved picture shows, and how."""
    items = data.get("visual_baselines") or []
    if not items:
        return []
    out = ["## Visual baselines", "",
           "The owner approved these pictures, each for one screen state at one viewport, theme and scale. Match them "
           "at that size; a state without a picture here is not judged on appearance.", ""]
    for b in items:
        out.append(f"- `{b['screen']}` {_state_name(data.get('screens') or [], b['screen'], b['state'])} "
                   f"(`{b['state']}`), {b['viewport']}, {b['theme']} theme, scale {b['scale']}: `{b['path']}`")
    return out + [""]


def _screen_names(data):
    names = []
    for e in [e for s in data["scenarios"] for e in s["events"]] + [e for r in data["evidence"] for e in r["events"]]:
        if e.get("screen") and e["screen"] not in names:
            names.append(e["screen"])
    return names


def host_screens_markdown(data):
    """A host pack without screen structure: the title line of each recorded host screen."""
    names = _screen_names(data)
    if not names:
        return []
    out = ["## Host screens", "", REPLAY_NOTE_3270, "",
           "No field layout was recorded for these screens: place the input fields the scenarios name, and record "
           "where you put them in BUILD-NOTES.md.", ""]
    for name in names:
        out += [f"### `{name}`", "", _title_line({"screen": name, "states": []}), ""]
    return out


def _required(data):
    """{screen: [control]} of the controls the recorded structure marks required."""
    out = {}
    for sc in data.get("screens") or []:
        seen = []
        for st in sc["states"]:
            for c in st["controls"]:
                if not c.get("required") or c.get("protected"):
                    continue
                where = (f"field at row {c['row']}, column {c.get('col', '?')}" if "row" in c else f"`{_ident(c)}`")
                text = where + (f' "{c["label"]}"' if c.get("label") else "")
                if text not in seen:
                    seen.append(text)
        if seen:
            out[sc["screen"]] = seen
    return out


def _host_keys(data):
    """{host screen: [AID keys the recording shows on it]}, in recorded order."""
    keys = {}

    def add(screen, key):
        keys.setdefault(screen, [])
        if key not in keys[screen]:
            keys[screen].append(key)

    for rec in data["evidence"]:
        for e in rec["events"]:
            if e.get("screen") and e.get("key"):
                add(e["screen"], e["key"])
    for sc in data.get("screens") or []:
        for t in sc.get("transitions") or []:
            if t.get("key"):
                add(sc["screen"], t["key"])
    return keys


def unrecorded_markdown(data):
    """The "Behaviour not recorded" section: what the builder does where the recording and the
    owner say nothing (direct URLs, keys not listed, required fields), behind the owner's failure
    cases."""
    surface = surface_of(data)
    out = ["## Behaviour not recorded", "",
           "The recording shows one path through the application. Off that path, build this:", ""]
    if any(s["failure_cases"] for s in data["scenarios"]):
        out.append("- Follow the owner's failure cases (under each scenario) first, with their messages word for "
                   "word. They win over every default below.")
    else:
        out.append("- The owner wrote no failure cases or precondition rules. Follow the owner's failure cases "
                   "when a later revision adds them; until then keep to the defaults below, and list each rule you "
                   "assumed in BUILD-NOTES.md.")
    if surface == "web":
        out.append("- Navigation: pages are reached only as recorded; a direct URL to a later step returns to the "
                   "start (the scenario's first page), unless a scenario says otherwise. A form opens only after "
                   "the step that leads to it.")
    elif surface == "mainframe":
        out.append("- Keys: keys not listed do nothing (the screen stays as it is, with no message), unless a "
                   "scenario says otherwise. The keys each screen answers, as recorded:")
        out += [f"  - `{screen}`: {', '.join(keys)}" for screen, keys in _host_keys(data).items()]
        out.append("- Empty or wrong input: show only the messages the failure cases name; add none of your own.")
    else:
        out.append("- Menus, buttons and keys not recorded do nothing, and windows open only as recorded, unless a "
                   "scenario says otherwise.")
    required = _required(data)
    if required:
        out.append("- Required fields (the recorded screens mark them `required` or `aria-required`): do not go on "
                   "while one is empty; show the message the failure cases give, else say in BUILD-NOTES.md which "
                   "wording you chose:")
        out += [f"  - `{screen}`: " + ", ".join(items) for screen, items in required.items()]
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
    validation: dict = field(default_factory=dict)  # pack_validator.validate of the pack

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


def draft(db, app_id, version_path, known_gaps=None):
    """The pack of the catalog's current state, validated (`pack_validator`): evidence.json
    carries the result as `completeness` and BUILD_TASK.md as "Completeness". `known_gaps` is
    the owner's written reason for freezing over critical gaps: when there are any, the pack also
    carries it, with those gaps, as `known_gaps` and "Known gaps" (so the owner previews and
    approves exactly those bytes); without critical gaps it is not used."""
    data, attachments, report = manifest(db, app_id, version_path)
    validation = pack_validator.validate(data, attachments)
    data["completeness"] = pack_validator.summary(validation)
    if known_gaps and not validation["passed"]:
        data["known_gaps"] = {"reason": redact.redact_text(known_gaps),
                              "gaps": [{"item": g["item"], "title": g["title"]}
                                       for g in pack_validator.critical(validation)]}
    contents = json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2)
    revision = hashlib.sha256(contents.encode("utf-8")).hexdigest()
    files = {"BUILD_TASK.md": task_markdown(data, revision, version_path, validation).encode("utf-8"),
             "evidence.json": contents.encode("utf-8"), **attachments}
    return Draft(revision, contents, files, archive(files), report, validation)


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
