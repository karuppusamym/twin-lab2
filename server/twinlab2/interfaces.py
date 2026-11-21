"""Interface contracts of the applications recorded together (twinlab2_contract.package).

One recording can show several applications handing data to each other: an order created in
Orders whose number is then typed in Billing. Each builder rebuilds one application from its own
package, so the package has to say what crosses the boundary. Per recording with at least two
applications that have selected events, `generate` infers a contract:

- **shared identifiers**: a fixture field (an example line `Field: value`) or a binding to an
  identifier that the scenarios of two or more of the applications use. Its producer is the
  application whose steps reach it first in the recording; the value and type are the
  producer's synthetic example;
- **relationships**: producer -> consumer, by identifier; by API call where a consumer's
  reviewed HAR (attachment kind API) calls the producer's web address (method, path and the
  JSON field names of the request and response); by file where the producer's step exports or
  downloads and a later step of the consumer imports or uploads (the columns of the exported
  grid as the schema);
- **launch order** (producers first, then the recording's order) and **reset order** (the
  reverse: consumers first, so no consumer keeps a reference to a record its producer removed);
- **when the producer is down**: what each consumer shows, left empty for the owner to write.

An owner of one of the applications may edit the contract (`save`); the stored one is then used
instead of the generated one, until it is removed (`reset`). `for_pack` gives an application's
package the contracts it is part of, with each identifier's `produced` (the producer's scenarios
name it) so the package validator can report a value typed in one application that its
producer never makes.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from twinlab2 import readiness_cache
from twinlab2 import redact
from twinlab2 import scenarios as scenario_store
from twinlab2_contract import package

EXPORT = re.compile(r"export|download|extract|print", re.I)
IMPORT = re.compile(r"import|upload|attach|load", re.I)


class Stale(Exception):
    """A save named a revision that is no longer the stored contract's."""


def _apps(db, recording_id):
    """[(app id, name, [selected event ids in recording order])] of a recording, by first event."""
    row = db.execute("SELECT events FROM recordings WHERE id = ?", (recording_id,)).fetchone()
    order = {e["event_id"]: i for i, e in enumerate(json.loads(row["events"]))} if row else {}
    out = []
    for link in db.execute("SELECT evidence.app_id, apps.name, evidence.selection FROM evidence "
                           "JOIN apps ON apps.id = evidence.app_id WHERE evidence.recording_id = ?",
                           (recording_id,)).fetchall():
        selected = sorted((e for e in json.loads(link["selection"]) if e in order), key=order.get)
        if selected:
            out.append((link["app_id"], link["name"], selected))
    out.sort(key=lambda a: order[a[2][0]])
    return out, order


def _steps(db, recording_id, app_id):
    steps = {}
    for flow in db.execute("SELECT flow FROM recording_apps WHERE recording_id = ? AND app_id = ? ORDER BY key",
                           (recording_id, app_id)):
        for step in json.loads(flow["flow"]):
            steps.setdefault(step.get("event_id"), step)
    return steps


def _uses(db, app_id, recording_id, order):
    """{identifier slug: (first recording position, field, type, value)} of what an application's
    scenarios name: their fixture fields and the identifiers their bindings use."""
    out = {}
    for s in scenario_store.load(db, app_id):
        fields = {package.slug(f["field"]): f for f in package.fixture_fields(scenario_store.lines(s["examples"]))}
        positions = {}
        for b in s.get("bindings") or []:
            name = package.slug(b.get("identifier") or b.get("field"))
            event = b["event"].split("#", 1)
            if len(event) == 2 and event[0] == recording_id and event[1] in order:
                positions[name] = min(positions.get(name, 10 ** 9), order[event[1]])
        linked = [order[r.split("#", 1)[1]] for r in s["events"]
                  if r.split("#", 1)[0] == recording_id and r.split("#", 1)[1] in order]
        start = min(linked) if linked else 10 ** 9
        for name in set(fields) | set(positions):
            f = fields.get(name) or {"field": name, "type": "identifier", "value": ""}
            at = positions.get(name, start)
            if name not in out or at < out[name][0]:
                out[name] = (at, f["field"], f["type"], f["value"])
    return out


def _har_calls(db, app_id):
    """[(method, scheme://host, path, request field names, response field names)] of an
    application's reviewed HAR attachments (redacted first, as the package carries them)."""
    calls = []
    for row in db.execute("SELECT * FROM attachments WHERE app_id = ? AND extension = '.har' ORDER BY id", (app_id,)):
        share = row["share"] if "share" in row.keys() else "restricted"
        from twinlab2 import artifacts

        content, _outcome, _ = redact.attachment(artifacts.content(db, row, "content"), ".har", share)
        if content is None:
            continue
        try:
            entries = json.loads(content.decode("utf-8")).get("log", {}).get("entries") or []
        except (ValueError, AttributeError):
            continue
        for entry in entries:
            req = entry.get("request") or {}
            parts = urlsplit(str(req.get("url") or ""))
            if not parts.netloc:
                continue
            calls.append((str(req.get("method") or "GET").upper(), f"{parts.scheme}://{parts.netloc}",
                          re.sub(r"/\d+(?=/|$)", "/{id}", parts.path or "/"),
                          _fields((req.get("postData") or {}).get("text")),
                          _fields(((entry.get("response") or {}).get("content") or {}).get("text"))))
    return calls


def _fields(text):
    try:
        doc = json.loads(text) if isinstance(text, str) and text.strip() else None
    except ValueError:
        return []
    if isinstance(doc, list) and doc and isinstance(doc[0], dict):
        doc = doc[0]
    return sorted(str(k) for k in doc)[:40] if isinstance(doc, dict) else []


def _origins(db, app_id):
    return [row["key"][4:] for row in db.execute(
        "SELECT key FROM app_identities WHERE app_id = ? AND key LIKE 'web:%'", (app_id,))]


def generate(db, recording_id) -> dict | None:
    """The inferred contract of a recording's applications, or None when fewer than two have
    selected events in it."""
    apps, order = _apps(db, recording_id)
    if len(apps) < 2:
        return None
    first = {a[0]: order[a[2][0]] for a in apps}
    uses = {a[0]: _uses(db, a[0], recording_id, order) for a in apps}
    identifiers, relationships = [], []
    names = sorted({n for u in uses.values() for n in u if n})
    for name in names:
        users = [(u[name][0], app) for app, u in uses.items() if name in u]
        if len(users) < 2:
            continue
        users.sort(key=lambda x: (x[0], first[x[1]]))
        producer = users[0][1]
        _at, field, kind, value = uses[producer][name]
        consumers = [app for _at, app in users[1:]]
        identifiers.append({"name": name, "field": field, "type": kind if kind != "text" else "identifier",
                            "value": value, "producer": producer, "consumers": consumers})
        relationships += [{"kind": "identifier", "identifier": name, "producer": producer, "consumer": c,
                           "when_down": ""} for c in consumers]
    for producer, _name, _ev in apps:
        origins = _origins(db, producer)
        for consumer, _n, _e in apps:
            if consumer == producer:
                continue
            for method, origin, path, req, resp in _har_calls(db, consumer):
                if any((origin + path).startswith(o) or origin == o.split("/", 3)[0] + "//" + o.split("/", 3)[2]
                       for o in origins if "://" in o):
                    rel = {"kind": "api", "producer": producer, "consumer": consumer, "when_down": "",
                           "detail": {"method": method, "path": path, "request": req, "response": resp}}
                    if rel not in relationships:
                        relationships.append(rel)
    for producer, _name, events in apps:
        steps = _steps(db, recording_id, producer)
        exports = [e for e in events if EXPORT.search(f"{steps.get(e, {}).get('label') or ''} "
                                                      f"{steps.get(e, {}).get('control') or ''}")]
        for event in exports:
            for consumer, _n, cevents in apps:
                if consumer == producer:
                    continue
                csteps = _steps(db, recording_id, consumer)
                if any(order[c] > order[event] and IMPORT.search(f"{csteps.get(c, {}).get('label') or ''}")
                       for c in cevents):
                    relationships.append({"kind": "file", "producer": producer, "consumer": consumer,
                                          "when_down": "", "detail": {"format": "", "columns": _columns(
                                              db, producer, steps.get(event, {}).get("screen"))}})
    launch = _launch(apps, relationships)
    return {"version": package.INTERFACES_VERSION, "recording_id": recording_id,
            "apps": [{"id": a[0], "name": a[1]} for a in apps], "identifiers": identifiers,
            "relationships": relationships, "launch_order": launch, "reset_order": list(reversed(launch))}


def _columns(db, app_id, screen):
    from twinlab2 import buildpack

    for sc in buildpack.screen_structure(db, app_id):
        if sc["screen"] == screen:
            for st in sc["states"]:
                for g in st.get("grids") or []:
                    if g.get("columns"):
                        return [c for c in g["columns"] if c]
    return []


def _launch(apps, relationships):
    """Producers before their consumers, else in the recording's order."""
    pending = [a[0] for a in apps]
    out = []
    while pending:
        ready = [a for a in pending if not any(r["consumer"] == a and r["producer"] in pending and r["producer"] != a
                                               for r in relationships)] or pending[:1]
        out.append(ready[0])
        pending.remove(ready[0])
    return out


def stored(db, recording_id):
    return db.execute("SELECT * FROM interface_contracts WHERE recording_id = ?", (recording_id,)).fetchone()


def recordings_of(db, app_id):
    return [r["recording_id"] for r in db.execute(
        "SELECT recording_id FROM evidence WHERE app_id = ? ORDER BY recording_id", (app_id,))]


def current(db, recording_id) -> dict | None:
    """{recording_id, edited, revision, doc, generated} of a recording's contract, or None."""
    generated = generate(db, recording_id)
    row = stored(db, recording_id)
    if generated is None and row is None:
        return None
    doc = json.loads(row["doc"]) if row else generated
    return {"recording_id": recording_id, "edited": row is not None, "revision": row["revision"] if row else 0,
            "edited_by": row["edited_by"] if row else None, "edited_at": row["edited_at"] if row else None,
            "doc": doc, "generated": generated}


def of_app(db, app_id) -> list[dict]:
    return [c for c in (current(db, r) for r in recordings_of(db, app_id)) if c is not None]


def _clean(doc, recording_id):
    """The owner's contract in its stored form, or ValueError with what is wrong."""
    if not isinstance(doc, dict):
        raise ValueError("The interface contract must be an object.")
    doc = _redacted(json.loads(json.dumps(doc)))
    out = {"version": package.INTERFACES_VERSION, "recording_id": recording_id,
           "apps": [{"id": a.get("id"), "name": str(a.get("name") or "")[:200]} for a in doc.get("apps") or []
                    if isinstance(a, dict)],
           "identifiers": [], "relationships": [],
           "launch_order": list(doc.get("launch_order") or []), "reset_order": list(doc.get("reset_order") or [])}
    for i in doc.get("identifiers") or []:
        if isinstance(i, dict):
            out["identifiers"].append({"name": package.slug(i.get("name")), "field": str(i.get("field") or i.get("name") or "")[:60],
                                       "type": i.get("type") or "identifier", "value": str(i.get("value") or "")[:200],
                                       "producer": i.get("producer"), "consumers": list(i.get("consumers") or [])})
    for r in doc.get("relationships") or []:
        if isinstance(r, dict):
            item = {"kind": r.get("kind"), "producer": r.get("producer"), "consumer": r.get("consumer"),
                    "when_down": str(r.get("when_down") or "")[:1000]}
            if r.get("kind") == "identifier":
                item["identifier"] = package.slug(r.get("identifier"))
            if isinstance(r.get("detail"), dict):
                item["detail"] = r["detail"]
            out["relationships"].append(item)
    problems = package.check_interfaces(out)
    if problems:
        raise ValueError("; ".join(problems[:5]).capitalize() + ".")
    return out


def _redacted(value):
    """Every string through the text rules: an owner's edit is synthetic, but must not carry data."""
    if isinstance(value, dict):
        return {k: _redacted(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redacted(v) for v in value]
    return redact.redact_text(value) if isinstance(value, str) else value


def save(db, recording_id, doc, user, now, expected_revision=None):
    """Store the owner's edit of a recording's contract (+1 revision). Its applications must be
    the recording's."""
    generated = generate(db, recording_id)
    if generated is None:
        raise LookupError(recording_id)
    doc = dict(doc or {}, apps=generated["apps"])
    clean = _clean(doc, recording_id)
    row = stored(db, recording_id)
    have = row["revision"] if row else 0
    if expected_revision is not None and expected_revision != have:
        raise Stale(recording_id)
    with db:
        for app in clean["apps"]:
            readiness_cache.forget(db, app["id"])
        if row:
            db.execute("UPDATE interface_contracts SET doc = ?, revision = revision + 1, edited_by = ?, edited_at = ? "
                       "WHERE recording_id = ?", (json.dumps(clean, sort_keys=True), user, now, recording_id))
        else:
            db.execute("INSERT INTO interface_contracts(recording_id, doc, revision, edited_by, edited_at) "
                       "VALUES (?, ?, 1, ?, ?)", (recording_id, json.dumps(clean, sort_keys=True), user, now))
    return current(db, recording_id)


def reset(db, recording_id):
    """Back to the generated contract."""
    with db:
        for app in (generate(db, recording_id) or {}).get("apps") or []:
            readiness_cache.forget(db, app["id"])
        db.execute("DELETE FROM interface_contracts WHERE recording_id = ?", (recording_id,))


def for_pack(db, app_id) -> list[dict]:
    """The contracts an application's package carries: each contract it is part of, with the
    relationships it is in and, per identifier, whether the producer's scenarios name it."""
    out = []
    for c in of_app(db, app_id):
        doc = c["doc"]
        if app_id not in {a["id"] for a in doc["apps"]}:
            continue
        rec = doc.get("recording_id") or c["recording_id"]
        _apps_, order = _apps(db, rec)
        named = {a["id"]: set(_uses(db, a["id"], rec, order)) for a in doc["apps"]}
        # only the applications this one exchanges data with: a package never names the others
        rels = [r for r in doc["relationships"] if app_id in (r["producer"], r["consumer"])]
        idents = [i for i in doc["identifiers"] if app_id == i.get("producer") or app_id in (i.get("consumers") or [])]
        related = {app_id} | {r["producer"] for r in rels} | {r["consumer"] for r in rels} |             {i.get("producer") for i in idents if i.get("producer") is not None}
        if len(related) < 2:
            continue
        idents = [dict(i, consumers=[x for x in i.get("consumers") or [] if x in related],
                       produced=i.get("producer") is not None and i["name"] in named.get(i["producer"], set()))
                  for i in idents]
        out.append({"recording_id": rec, "edited": c["edited"], "apps": [a for a in doc["apps"] if a["id"] in related],
                    "identifiers": idents, "relationships": rels,
                    "launch_order": [a for a in doc["launch_order"] if a in related],
                    "reset_order": [a for a in doc["reset_order"] if a in related]})
    return out


def markdown(data) -> list[str]:
    """The "Interfaces with other applications" section of BUILD_TASK.md."""
    contracts = data.get("interfaces") or []
    if not contracts:
        return []
    me = (data.get("app") or {}).get("id")
    out = ["## Interfaces with other applications", "",
           "This application was recorded together with others that hand it data or take data from it. Build "
           "your side of each interface as written here; the other side is built from its own package. Use "
           "the shared synthetic values exactly: the other application's package uses the same ones.", ""]
    for c in contracts:
        name = {a["id"]: a["name"] for a in c["apps"]}
        label = lambda i: (f"{name.get(i, i)} (this application)" if i == me else name.get(i, str(i)))  # noqa: E731
        out += [f"### Recording {c['recording_id']}" + (" (contract edited by the owner)" if c["edited"] else
                                                        " (contract inferred from the recording)"), ""]
        out.append("- Launch order: " + " → ".join(label(i) for i in c["launch_order"]))
        out.append("- Reset order: " + " → ".join(label(i) for i in c["reset_order"]))
        for i in c["identifiers"]:
            out.append(f"- Shared identifier `{i['name']}` ({i['type']}): synthetic value `{i['value']}`, produced by "
                       f"{label(i['producer'])}, used by " + ", ".join(label(x) for x in i["consumers"]))
        for r in c["relationships"]:
            what = {"identifier": f"identifier `{r.get('identifier')}`", "api": "API call", "file": "file"}[r["kind"]]
            line = f"- {label(r['producer'])} → {label(r['consumer'])} by {what}"
            schema = r.get("detail") or {}
            if r["kind"] == "api":
                line += f": `{schema.get('method')} {schema.get('path')}`"
                if schema.get("request"):
                    line += f", request fields {', '.join(schema['request'])}"
                if schema.get("response"):
                    line += f", response fields {', '.join(schema['response'])}"
            elif r["kind"] == "file" and schema.get("columns"):
                line += f": columns {', '.join(schema['columns'])}"
            line += (f". When {name.get(r['producer'])} is down: {r['when_down']}" if r.get("when_down")
                     else f". What {name.get(r['consumer'])} does when {name.get(r['producer'])} is down is not stated: "
                          "show an error, keep the user's input, and say so in BUILD-NOTES.md")
            out.append(line)
        out.append("")
    return out


def verify(db, manifest, app_id, deployed_commit) -> list[dict]:
    """What Verify can check of a package's interface contracts against the deployments:
    [{what, status (pass | fail | blocked | not-checked), notes}]. A shared identifier this
    application uses must be one its producer makes (fail otherwise: it holds Tested); each
    producer this application consumes from must have a verified deployment that answers (blocked
    otherwise: the cross-application replay cannot run, a peer being down is not this release's
    fault). What a consumer does when its producer is down needs the producer stopped, and the API
    and file schemas a live exchange: those are reported as not checked."""
    out = []
    for c in manifest.get("interfaces") or []:
        names = {a.get("id"): a.get("name") for a in c.get("apps") or []}
        for i in c.get("identifiers") or []:
            if app_id not in (i.get("consumers") or []):
                continue
            ok = i.get("producer") is not None and i.get("produced")
            out.append({"what": f"shared identifier {i.get('name')}", "status": "pass" if ok else "fail",
                        "notes": (f"produced by {names.get(i.get('producer'))} with the value {i.get('value')}" if ok else
                                  f"{names.get(i.get('producer'), 'no application')} never produces it")})
        producers = []
        for r in c.get("relationships") or []:
            if r.get("consumer") == app_id and r.get("producer") not in producers:
                producers.append(r.get("producer"))
        for producer in producers:
            row = db.execute(
                "SELECT deployments.url FROM deployments JOIN releases ON releases.id = deployments.release_id "
                "WHERE releases.app_id = ? AND deployments.verification = 'verified' ORDER BY releases.id DESC LIMIT 1",
                (producer,)).fetchone()
            name = names.get(producer, f"application {producer}")
            if row is None:
                out.append({"what": f"peer {name}", "status": "blocked",
                            "notes": "no verified deployment: start it first (launch order " +
                                     " → ".join(str(names.get(a, a)) for a in c.get("launch_order") or []) + ")"})
            elif deployed_commit(row["url"]) is None:
                out.append({"what": f"peer {name}", "status": "blocked", "notes": f"{row['url']} does not answer"})
            else:
                out.append({"what": f"peer {name}", "status": "pass", "notes": f"{row['url']} answers"})
        for r in c.get("relationships") or []:
            if r.get("consumer") == app_id:
                out.append({"what": f"when {names.get(r.get('producer'))} is down", "status": "not-checked",
                            "notes": (r.get("when_down") or "not written") + " (needs the producer stopped)"})
                if r.get("kind") in ("api", "file"):
                    out.append({"what": f"{r['kind']} schema from {names.get(r.get('producer'))}", "status": "not-checked",
                                "notes": "needs a live exchange between the two deployments"})
    return out
