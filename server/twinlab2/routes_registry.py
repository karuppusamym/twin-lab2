"""The routes of registry.py's work: pairing and machines, recording sessions offered to a
machine, the recorder's agent API (`/api/agent/...`, unchanged: the shipped recorder talks to it),
the references a recorder sends, recordings, application identities, merging and automations.

The agent API answers exactly as it always has: JSON, `{"error": ...}` on a refusal, the machine
token as `Authorization: Bearer`, and contract refusals (registry.contract_refusal) as 426/400.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import re
import secrets
from urllib.parse import quote

from fastapi import Depends, File, Request, UploadFile
from fastapi.responses import JSONResponse
import yaml

from twinlab2 import agentauth
from twinlab2 import api_models as m
from twinlab2 import audit
from twinlab2 import auth
from twinlab2 import event_ids
from twinlab2 import readiness_cache
from twinlab2 import shapes
from twinlab2.auth import require_role
from twinlab2.registry import (CODE_ALPHABET, CODE_LENGTH, CODE_MINUTES, FAILURES_PER_10_MIN, HOST_EXES, LEVELS,
                               clean_references, contract_refusal, discovered_name, merge_references,
                               normalise_key, now, path_matches,
                               recorder_of, recording_status, sha, target_of, web_parts)
from twinlab2.rest import Routes, current, db, fail, g, json_body
from twinlab2.views import ensure_reviewer, get_app, get_recording, record, reviewer_of_recording
from twinlab2_contract import recorder_contract as contract
from twinlab2_contract import structure
from twinlab2 import completeness as compl

V1 = "/api/v1"
routes = Routes()


def current_user():
    return g.get("user")


def refusal(error):
    body, status = contract_refusal(error)
    return JSONResponse(body, status_code=status)


def agent_error(message, status):
    return JSONResponse({"error": message}, status_code=status)


def owned(app_ids):
    """403 unless the user owns every application in `app_ids`."""
    for app_id in app_ids:
        auth.ensure("owner", app_id=app_id)


def agent_machine(scope="record"):
    """The paired, not revoked machine of the request's bearer token (401 otherwise), counted
    against its rate limit (429) and holding `scope` (403; agentauth). `scope=None`: any paired
    machine (its own updates)."""
    request = g.request
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        fail(401)
    token = header[7:].strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-]{40,100}", token):
        fail(401)
    row = db().execute("SELECT * FROM machines WHERE token_sha = ? AND revoked_at IS NULL",
                       (sha(token),)).fetchone()  # fmt: skip
    if row is None:
        fail(401)
    agentauth.limit(row["id"])
    if scope is not None:
        agentauth.require_scope(row, scope)
    with db():
        db().execute("UPDATE machines SET last_seen = ?, recorder = ? WHERE id = ?",
                     (now(), request.headers.get("X-TwinLab-Recorder", "")[:60], row["id"]))  # fmt: skip
    return row


# -- machines and pairing ---------------------------------------------------------------------


def machine_rows():
    """A user sees the machines they paired; a portal admin sees all."""
    return [row for row in db().execute("""
        SELECT machines.*, machine_contracts.contract_version FROM machines
        LEFT JOIN machine_contracts ON machine_contracts.machine_id = machines.id
        ORDER BY revoked_at IS NOT NULL, name
    """).fetchall() if g.is_admin or row["paired_by"] == current_user()]


@routes.get(V1 + "/machines", auth.signed_in, response_model=m.MachineList)
def list_machines():
    return {"machines": [shapes.machine(r) for r in machine_rows()], "code_minutes": CODE_MINUTES}


@routes.post(V1 + "/machines/pairing-codes", auth.signed_in, response_model=m.PairingCode, status_code=201,
             idempotent="stored")
def make_code(body: m.PairingCodeInput | None = None):
    """A one-time pairing code, returned once (only its SHA-256 is stored). `scopes`: what the
    machine paired with it may do (agentauth; default both)."""
    try:
        scopes = agentauth.parse_scopes(body.scopes) if body is not None and body.scopes is not None             else agentauth.DEFAULT_SCOPES
    except ValueError as error:
        fail(400, str(error), errors=[{"field": "scopes", "message": str(error)}])
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    created = datetime.now(timezone.utc)
    expires = (created + timedelta(minutes=CODE_MINUTES)).isoformat()
    with db():
        db().execute("INSERT INTO pairing_codes(code_sha, created_by, created_at, expires_at, scopes) "
                     "VALUES (?, ?, ?, ?, ?)", (sha(code), current_user(), created.isoformat(), expires, scopes))
    return {"code": code, "display": f"{code[:5]}-{code[5:]}", "minutes": CODE_MINUTES, "expires_at": expires,
            "scopes": scopes.split()}


@routes.post(V1 + "/machines/{machine_id:int}/revoke", auth.signed_in, response_model=m.Machine)
def revoke(machine_id: int):
    row = db().execute("SELECT paired_by FROM machines WHERE id = ?", (machine_id,)).fetchone()
    if row is None:
        fail(404, "There is no such machine.")
    if not g.is_admin and row["paired_by"] != current_user():
        fail(403, "Only the user who paired a machine, or a portal admin, can revoke it.", "role")
    with db():
        db().execute("UPDATE machines SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                     (now(), machine_id))  # fmt: skip
    return shapes.machine(next(r for r in machine_rows() if r["id"] == machine_id))


@routes.post("/api/agent/pair", auth.agent_token)
def agent_pair(request: Request, body=Depends(json_body)):
    body = body if isinstance(body, dict) else {}
    try:  # before the code is looked at: an outdated recorder does not use up the code
        contract.validate(body, "pair")
    except contract.ContractError as error:
        return refusal(error)
    code = str(body.get("code") or "").strip().upper().replace("-", "").replace(" ", "")
    name = str(body.get("name") or "").strip()[:80]
    if not name or re.search(r"[\x00-\x1f]", name):
        return agent_error("give the machine a name", 400)
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    failures = db().execute("SELECT COUNT(*) FROM pairing_failures WHERE at > ?", (cutoff,)).fetchone()[0]
    if failures >= FAILURES_PER_10_MIN:
        return agent_error("too many wrong codes: wait ten minutes", 429)
    row = None
    if len(code) == CODE_LENGTH and set(code) <= set(CODE_ALPHABET):
        row = db().execute("SELECT * FROM pairing_codes WHERE code_sha = ? AND used_at IS NULL",
                           (sha(code),)).fetchone()  # fmt: skip
    if row is None or row["expires_at"] < datetime.now(timezone.utc).isoformat():
        with db():
            db().execute("INSERT INTO pairing_failures(at) VALUES (?)", (now(),))
        return agent_error("the code is wrong, used or expired", 403)
    token = secrets.token_urlsafe(32)
    with db():
        used = db().execute("UPDATE pairing_codes SET used_at = ? WHERE code_sha = ? AND used_at IS NULL",
                            (now(), row["code_sha"])).rowcount  # fmt: skip
        if used != 1:
            fail(403)
        machine = db().execute(
            "INSERT INTO machines(name, token_sha, paired_by, paired_at, recorder, scopes) VALUES (?,?,?,?,?,?) "
            "RETURNING id",
            (name, sha(token), row["created_by"], now(), request.headers.get("X-TwinLab-Recorder", "")[:60],
             row["scopes"] or agentauth.DEFAULT_SCOPES),
        ).fetchone()[0]  # fmt: skip
        db().execute("UPDATE pairing_codes SET machine_id = ? WHERE code_sha = ?", (machine, row["code_sha"]))
        rec_name, rec_version = recorder_of(body, request.headers.get("X-TwinLab-Recorder", ""))
        db().execute("""INSERT INTO machine_contracts(machine_id, contract_version, recorder, updated_at) VALUES (?, ?, ?, ?)
                        ON CONFLICT(machine_id) DO UPDATE SET contract_version = excluded.contract_version,
                        recorder = excluded.recorder, updated_at = excluded.updated_at""",
                     (machine, body["contract_version"], f"{rec_name} {rec_version}", now()))  # fmt: skip
    return {"token": token, "machine_id": machine, "name": name, "contract_version": contract.CONTRACT_VERSION,
            "contract_supported": contract.supported()}


# -- sessions offered to a machine ---------------------------------------------------------------


def identities(app_id):
    return db().execute("SELECT * FROM app_identities WHERE app_id = ? ORDER BY key", (app_id,)).fetchall()


def deny_keys():
    """Keys the recorder must not record in full. A path identity (`web:https://host/finance/payroll`)
    is sent too: a recorder of contract 1.2 sends the steps under it as presence when no target's
    longer prefix claims them; an older recorder compares whole identity keys, so a path key
    never denies the applications sharing its host. A `host` application's path key is left out:
    it claims what no path claims, which a recorder cannot tell from its key."""
    return [r["key"] for r in db().execute("""
        SELECT key, apps.url_match FROM app_identities JOIN apps ON apps.id = app_identities.app_id
        WHERE apps.status = 'not_replicated' AND key NOT LIKE 'exe:%' ORDER BY key
    """) if not (r["key"].startswith("web:") and web_parts(r["key"])[1] and r["url_match"] == "host")]  # fmt: skip


def web_identities(origin):
    """The identities on one origin as (key, app_id, prefix): a `host` application's prefix is
    empty, so its addresses stand for the whole host."""
    rows = db().execute("""
        SELECT app_identities.key, app_identities.app_id, apps.url_match FROM app_identities
        JOIN apps ON apps.id = app_identities.app_id
        WHERE app_identities.key = ? OR substr(app_identities.key, 1, ?) = ?
    """, (origin, len(origin) + 1, origin + "/")).fetchall()
    return [(r["key"], r["app_id"], "" if r["url_match"] == "host" else web_parts(r["key"])[1]) for r in rows]


def resolve_web(key):
    """The identity a web address belongs to: the longest matching path prefix wins; a
    host-only identity (or a `host` application) matches every path on its host."""
    origin, path = web_parts(key)
    best = None
    for ident, app_id, prefix in web_identities(origin):
        if path_matches(prefix, path):
            rank = (len(prefix), ident == key, -app_id)
            if best is None or rank > best[0]:
                best = (rank, ident, app_id)
    return (best[1], best[2]) if best else None


def web_clash(app_id, key, url_match):
    """Another application that already claims the same host and path as `key` would."""
    origin, path = web_parts(key)
    prefix = "" if url_match == "host" else path
    for ident, other, other_prefix in web_identities(origin):
        if other != app_id and other_prefix == prefix:
            return other
    return None


def capture_view(row):
    targets = {}
    for target in json.loads(row["targets"]):
        targets.setdefault(target["app_id"], {"app_id": target["app_id"], "name": target.get("name") or "",
                                              "level": target.get("level") or "full"})
    return {"id": row["id"], "name": row["name"], "status": row["status"], "machine": row["machine"],
            "automation": row["automation"], "targets": list(targets.values()), "recording_id": row["recording_id"],
            "created_by": row["created_by"], "created_at": row["created_at"]}


def capture_rows():
    """Sessions the user requested (all for an admin)."""
    return [row for row in db().execute("""
        SELECT capture_sessions.*, machines.name AS machine, automations.name AS automation
        FROM capture_sessions JOIN machines ON machines.id = capture_sessions.machine_id
        LEFT JOIN automations ON automations.id = capture_sessions.automation_id
        ORDER BY capture_sessions.id DESC
    """).fetchall() if g.is_admin or row["created_by"] == current_user()]


@routes.get(V1 + "/capture-sessions", auth.signed_in, response_model=m.CapturePage)
def list_capture_sessions():
    """Sessions the user requested (all for an admin), for applications the user owns."""
    apps = [row for row in db().execute("""
        SELECT apps.*, (SELECT COUNT(*) FROM app_identities WHERE app_id = apps.id) AS ids
        FROM apps WHERE status != 'not_replicated' ORDER BY name
    """).fetchall() if auth.can("owner", app_id=row["id"])]
    machines = [mach for mach in db().execute("SELECT * FROM machines WHERE revoked_at IS NULL ORDER BY name")
                .fetchall() if g.is_admin or mach["paired_by"] == current_user()]
    return {"sessions": [capture_view(r) for r in capture_rows()],
            "machines": [shapes.ref(r) for r in machines],
            "apps": [{"id": a["id"], "name": a["name"], "identity_count": a["ids"]} for a in apps],
            "automations": [shapes.ref(r) for r in db().execute("SELECT * FROM automations ORDER BY name").fetchall()]}


@routes.post(V1 + "/capture-sessions", auth.signed_in, response_model=m.CaptureSession, status_code=201,
             idempotent="stored")
def request_recording(data: m.NewCaptureSession):
    name = data.name.strip()[:120]
    machine_id, automation_id = data.machine_id, data.automation_id or None
    if not name or db().execute("SELECT 1 FROM machines WHERE id = ? AND revoked_at IS NULL",
                                (machine_id,)).fetchone() is None:  # fmt: skip
        fail(400, "Name the session and choose a paired machine.")
    targets = []
    for target in data.targets:
        app_id, level = target.app_id, target.level or "full"
        if app_id is None:
            fail(400, "Invalid application.")
        if level not in LEVELS:
            fail(400, "Choose full or steps.")
        item = db().execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone()
        if item is not None:
            owned([app_id])
        ids = identities(app_id) if item else []
        if item is None or not ids:
            fail(400, "Each target application needs an identity (an executable or a web address).")
        for row in ids:
            targets.append(target_of(row["key"], level, app_id, item["name"]))
    if not targets:
        fail(400, "Choose at least one target application.")
    if automation_id is not None and db().execute("SELECT 1 FROM automations WHERE id = ?",
                                                  (automation_id,)).fetchone() is None:  # fmt: skip
        fail(400, "Unknown automation.")
    with db():
        session_id = db().execute("""INSERT INTO capture_sessions(name, machine_id, targets, automation_id,
                        created_by, created_at, status) VALUES (?,?,?,?,?,?, 'offered') RETURNING id""",
                                  (name, machine_id, json.dumps(targets), automation_id, current_user(),
                                   now())).fetchone()[0]  # fmt: skip
    return capture_view(next(r for r in capture_rows() if r["id"] == session_id))


@routes.post(V1 + "/capture-sessions/{session_id:int}/withdraw", auth.signed_in, response_model=m.CaptureSession)
def withdraw_session(session_id: int):
    row = db().execute("SELECT created_by FROM capture_sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        fail(404, "There is no such recording session.")
    if not g.is_admin and row["created_by"] != current_user():
        fail(403, "Only who requested a recording session, or a portal admin, can withdraw it.", "role")
    with db():
        db().execute("UPDATE capture_sessions SET status = 'withdrawn' WHERE id = ? AND status = 'offered'",
                     (session_id,))  # fmt: skip
    return capture_view(next(r for r in capture_rows() if r["id"] == session_id))


@routes.get("/api/agent/sessions", auth.agent_token)
def agent_sessions():
    machine = agent_machine()
    rows = db().execute("SELECT * FROM capture_sessions WHERE machine_id = ? AND status = 'offered' ORDER BY id",
                        (machine["id"],)).fetchall()  # fmt: skip
    return {"machine": {"id": machine["id"], "name": machine["name"]}, "deny_keys": deny_keys(),
            "sessions": [{"id": r["id"], "name": r["name"], "automation_id": r["automation_id"],
                          "targets": json.loads(r["targets"])} for r in rows]}  # fmt: skip


@routes.get("/api/agent/apps", auth.agent_token)
def agent_apps():
    """Catalog applications with an identity, for a session started on the recorder."""
    agent_machine()
    rows = db().execute("""
        SELECT apps.id, apps.name, app_identities.key FROM apps
        JOIN app_identities ON app_identities.app_id = apps.id
        WHERE apps.status != 'not_replicated' ORDER BY apps.name
    """).fetchall()
    return {"deny_keys": deny_keys(), "apps": [
        {"app_id": r["id"], "name": r["name"], "target": target_of(r["key"], "full", r["id"], r["name"])}
        for r in rows]}  # fmt: skip


@routes.get("/api/agent/update", auth.agent_token, response_model=m.RecorderUpdate)
def agent_update(version: str = ""):
    """The newest recorder release this portal offers (recorder_releases)."""
    from twinlab2 import recorder_releases

    agent_machine(scope=None)  # every paired machine may update itself, whatever its scopes
    return recorder_releases.answer(version.strip()[:20] or None)


@routes.get("/api/agent/update/{file}", auth.agent_token)
def agent_update_file(file: str):
    from fastapi.responses import FileResponse

    from twinlab2 import recorder_releases

    agent_machine(scope=None)
    rel = recorder_releases.latest()
    if rel is None or file != rel["file"]:
        return agent_error("There is no such recorder release.", 404)
    return FileResponse(rel["path"], media_type="application/octet-stream", filename=rel["file"],
                        headers={"Cache-Control": "no-store"})  # fmt: skip


# -- references -------------------------------------------------------------------------------------


def app_for(item):
    """The catalog application of a recorded one: by identity key, by the target's catalog id,
    or by its executable name; else a new `discovered` one."""
    row = db().execute("SELECT app_id FROM app_identities WHERE key = ?", (item["key"],)).fetchone()
    if row is not None:
        return row["app_id"], False
    app_id = None
    shared_host = False
    if item.get("app_id") and db().execute("SELECT 1 FROM apps WHERE id = ?", (item["app_id"],)).fetchone():
        app_id = item["app_id"]
        if item["key"].startswith("web:"):
            # a target known by path does not claim the rest of its host
            origin = web_parts(item["key"])[0]
            if any(other == app_id for _, other, prefix in web_identities(origin) if prefix):
                app_id = None
                shared_host = True
    # an executable name finds a catalog application, unless it is a host's (java.exe runs many)
    if app_id is None and item.get("exe") and item["exe"].lower() not in HOST_EXES:
        row = db().execute("SELECT app_id FROM app_identities WHERE key = ?",
                           ("exe:" + item["exe"].lower(),)).fetchone()  # fmt: skip
        app_id = row["app_id"] if row else None
    created = False
    if app_id is None:
        name = discovered_name(db(), item, shared_host)
        app_id = db().execute(
            "INSERT INTO apps(name, git_url, requirements, status, surface) VALUES (?, '', '', 'discovered', ?) RETURNING id",
            (name, item["surface"]),
        ).fetchone()[0]
        created = True
    facts = {k: item[k] for k in ("exe", "company", "product", "version", "toolkit") if k in item}
    db().execute("INSERT INTO app_identities(key, app_id, surface, facts) VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                 (item["key"], app_id, item["surface"], json.dumps(facts)))  # fmt: skip
    readiness_cache.forget(db(), app_id)
    return app_id, created


def _claim(item, screen):
    """The (identity key, app_id) a web origin's step or screen on `screen` goes to: the identity
    with the longest path prefix it is under, else None (the recorded application's own)."""
    url = normalise_key(screen or "") if str(screen or "").startswith("http") else None
    url = url if url and web_parts(url)[0] == web_parts(item["key"])[0] else item["key"]
    return resolve_web(url)


def assign_steps(item, steps, screens=()):
    """The recorded application's steps and screens per catalog application, as (app_id,
    identity key, steps, created, screens). A web origin's steps go by the path of their screen
    to the identity with the longest matching path prefix, and its screens (contract 1.3) by the
    same rule, so a screen lands on the application its steps land on; what no identity claims
    goes to `app_for(item)`."""
    groups, rest, shapes_of, rest_screens = {}, [], {}, []
    if item["key"].startswith("web:"):
        for step in steps:
            found = _claim(item, step.get("screen"))
            if found is None:
                rest.append(step)
            else:
                groups.setdefault(found, []).append(step)
        for screen in screens:
            found = _claim(item, screen.get("screen"))
            if found is None:
                rest_screens.append(screen)
            else:
                groups.setdefault(found, [])
                shapes_of.setdefault(found, []).append(screen)
        if not steps:
            found = resolve_web(item["key"])
            if found is not None:
                groups.setdefault(found, [])
    else:
        rest, rest_screens = list(steps), list(screens)
    out = [(app_id, key, group, False, shapes_of.get((key, app_id), [])) for (key, app_id), group in groups.items()]
    if rest or rest_screens or not groups:
        app_id, created = app_for(item)
        out.append((app_id, item["key"], rest, created, rest_screens))
    return out


def link_evidence(app_id, recording_id, numbers, seen):  # numbers: the event ids received
    """The application's evidence link to the recording. A first receipt (or one the owner
    has not reviewed yet) selects every step, as before. Once the owner has reviewed the
    selection it is never changed by a resend: events that are new in the resend are kept as
    suggestions, shown as new and not yet reviewed until the owner saves the assignment."""
    row = db().execute("SELECT * FROM evidence WHERE app_id = ? AND recording_id = ?",
                       (app_id, recording_id)).fetchone()  # fmt: skip
    if row is None:
        # the owner removed this application's assignment: a resend does not bring it back
        if numbers and not db().execute("SELECT 1 FROM evidence_removals WHERE app_id = ? AND recording_id = ?",
                                        (app_id, recording_id)).fetchone():  # fmt: skip
            db().execute("""INSERT INTO evidence(app_id, recording_id, selection, notes, reviewed, suggested)
                            VALUES (?, ?, ?, '', 0, '[]')""", (app_id, recording_id, json.dumps(sorted(numbers))))
    elif not row["reviewed"]:
        # every change is a new revision of the assignment (revisions.py): an owner's open copy is stale
        if numbers and json.dumps(sorted(numbers)) != row["selection"]:
            db().execute("UPDATE evidence SET selection = ?, revision = revision + 1 WHERE app_id = ? AND recording_id = ?",
                         (json.dumps(sorted(numbers)), app_id, recording_id))  # fmt: skip
    else:
        selection = set(json.loads(row["selection"]))
        suggested = (set(json.loads(row["suggested"])) | (numbers - seen)) & numbers
        value = json.dumps(sorted(suggested - selection))
        if value != row["suggested"]:
            db().execute("UPDATE evidence SET suggested = ?, revision = revision + 1 WHERE app_id = ? AND recording_id = ?",
                         (value, app_id, recording_id))  # fmt: skip


@routes.post("/api/agent/references", auth.agent_token)
def agent_references(request: Request, doc=Depends(json_body)):
    machine = agent_machine()
    try:
        contract.validate(doc, "references")
    except contract.ContractError as error:
        return refusal(error)
    try:
        refs = clean_references(doc)
    except (ValueError, TypeError) as error:
        return agent_error(f"invalid references: {error}", 400)
    rec_name, rec_version = recorder_of(doc, request.headers.get("X-TwinLab-Recorder", ""))
    sender = {"contract_version": doc["contract_version"], "recorder": rec_name,
              "recorder_version": rec_version,
              "twinlab_commit": (doc.get("recorder") or {}).get("twinlab_commit")
              or refs["session"].get("twinlab_commit")}  # fmt: skip
    s = refs["session"]
    recording_id = "ws-" + s["id"]
    capture = None
    if s.get("portal_session_id"):
        capture = db().execute("SELECT * FROM capture_sessions WHERE id = ? AND machine_id = ?",
                               (s["portal_session_id"], machine["id"])).fetchone()  # fmt: skip
    automation_id = capture["automation_id"] if capture else None
    if automation_id is None and s.get("automation_id"):
        if db().execute("SELECT 1 FROM automations WHERE id = ?", (s["automation_id"],)).fetchone():
            automation_id = s["automation_id"]
    owner = db().execute("SELECT machine_id FROM session_refs WHERE recording_id = ?",
                         (recording_id,)).fetchone()  # fmt: skip
    if owner is not None and owner["machine_id"] != machine["id"]:
        return agent_error("this session belongs to another machine", 409)
    # an older recorder's steps get the id stored for them by an earlier send, else one
    # derived now (event_ids): every reference below is to event_id, never to n or seq
    known = event_ids.stored_ids(db(), recording_id)
    for steps in refs["flows"].values():
        event_ids.assign(steps, recording_id, known)
    if owner is not None:  # an earlier send of this recording: this one adds to it
        earlier = db().execute("SELECT payload FROM session_refs WHERE recording_id = ?", (recording_id,)).fetchone()
        before = json.loads(earlier["payload"])
        for steps in before["flows"].values():  # a payload stored before event ids had none
            event_ids.assign(steps, recording_id, known)
        refs = merge_references(before, refs)
    names = {a["key"]: a.get("display") or a["key"] for a in refs["apps"]}
    events = []
    for key, steps in refs["flows"].items():
        for step in steps:
            event = {"event_id": step["event_id"], "seq": step.get("seq", step["n"]),
                     "source": names.get(key, key)[:150], "action": step.get("action", "other")}
            if step.get("captured_at"):
                event["time"] = step["captured_at"]
            if step.get("channel"):
                event["channel"] = step["channel"]
            events.append(event)
    events.sort(key=lambda e: (e["seq"], e.get("time") or "", e["event_id"]))
    mapped, discovered = [], []
    with db():
        db().execute("""INSERT INTO recordings(id, name, events) VALUES (?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET name = excluded.name, events = excluded.events""",
                     (recording_id, s["name"], json.dumps(events)))  # fmt: skip
        db().execute("""INSERT INTO session_refs(recording_id, session_key, machine_id, capture_session_id,
                        automation_id, payload, received_at) VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(recording_id) DO UPDATE SET payload = excluded.payload,
                        received_at = excluded.received_at, automation_id = excluded.automation_id""",
                     (recording_id, s["id"], machine["id"], capture["id"] if capture else None,
                      automation_id, json.dumps(refs), now()))  # fmt: skip
        seen = {}  # what earlier sends of this recording had, per application
        for row in db().execute("SELECT app_id, flow FROM recording_apps WHERE recording_id = ?", (recording_id,)):
            seen.setdefault(row["app_id"], set()).update(st["event_id"] for st in json.loads(row["flow"]))
        db().execute("DELETE FROM recording_apps WHERE recording_id = ?", (recording_id,))
        received = {}  # app_id -> event ids in this send
        denied = {r[0] for r in db().execute("SELECT id FROM apps WHERE status = 'not_replicated'")}
        for item in refs["apps"]:
            for app_id, key, steps, created, screens in assign_steps(item, refs["flows"].get(item["key"], []),
                                                                     (refs.get("screens") or {}).get(item["key"], [])):
                if app_id in denied and not created:
                    # marked not replicated: no flow, screens or evidence link for it (an older
                    # recorder, or a path application on a shared host, may still send them)
                    continue
                old = db().execute("SELECT flow, screens FROM recording_apps WHERE recording_id = ? AND app_id = ? AND key = ?",
                                   (recording_id, app_id, key)).fetchone()  # fmt: skip
                if old is not None:  # two references resolved to one identity
                    steps = json.loads(old["flow"]) + steps
                    screens = structure.merge(json.loads(old["screens"]), screens)
                db().execute("""INSERT INTO recording_apps(recording_id, app_id, key, level, flow, screens)
                                VALUES (?, ?, ?, ?, ?, ?)
                                ON CONFLICT(recording_id, app_id, key) DO UPDATE SET level = excluded.level,
                                flow = excluded.flow, screens = excluded.screens""",
                             (recording_id, app_id, key, item["level"], json.dumps(steps), json.dumps(screens)))  # fmt: skip
                received.setdefault(app_id, set()).update(st["event_id"] for st in steps)
                if automation_id is not None:
                    db().execute("INSERT INTO automation_apps(automation_id, app_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
                                 (automation_id, app_id))
                if app_id not in mapped and app_id not in discovered:
                    (discovered if created else mapped).append(app_id)
        for app_id, numbers in received.items():
            link_evidence(app_id, recording_id, numbers, seen.get(app_id, set()))
        # the recording's events, flows and recorded day changed for the applications it had and has
        readiness_cache.forget(db(), *seen)
        readiness_cache.forget_recording(db(), recording_id)
        db().execute("INSERT INTO recording_uploads(recording_id, added_by, added_at) VALUES (?, ?, ?) "
                     "ON CONFLICT DO NOTHING", (recording_id, machine["paired_by"], now()))
        for app_id in discovered:
            if auth.valid_name(machine["paired_by"]) and not auth.owners(db(), app_id):
                auth.assign(db(), app_id, machine["paired_by"], "owner", f"machine:{machine['id']}", now())
                audit.record(db(), f"machine:{machine['id']}", "role.assign", "role",
                             f"{app_id}/{machine['paired_by']}", app_id=app_id, user=machine["paired_by"],
                             role="owner", previous=None, reason="paired the machine that discovered it")
        report = refs.get("completeness")
        old = db().execute("SELECT * FROM recording_completeness WHERE recording_id = ?", (recording_id,)).fetchone()
        text = json.dumps(report, sort_keys=True) if report else None
        # an owner's acceptance of a partial recording holds only for what they accepted
        accepted = (old["accepted_by"], old["accepted_at"]) if old is not None and old["completeness"] == text else (None, None)
        db().execute("""INSERT INTO recording_completeness(recording_id, completeness, preflight, accepted_by,
                        accepted_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(recording_id) DO UPDATE SET completeness = excluded.completeness,
                        preflight = excluded.preflight, accepted_by = excluded.accepted_by,
                        accepted_at = excluded.accepted_at, updated_at = excluded.updated_at""",
                     (recording_id, text, json.dumps(refs["preflight"], sort_keys=True) if refs.get("preflight") else None,
                      *accepted, now()))  # fmt: skip
        if capture is not None:
            db().execute("UPDATE capture_sessions SET status = 'recorded', recording_id = ? WHERE id = ?",
                         (recording_id, capture["id"]))  # fmt: skip
        db().execute("""INSERT INTO recording_contracts(recording_id, contract_version, recorder, recorder_version,
                        twinlab_commit, received_at) VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(recording_id) DO UPDATE SET contract_version = excluded.contract_version,
                        recorder = excluded.recorder, recorder_version = excluded.recorder_version,
                        twinlab_commit = excluded.twinlab_commit, received_at = excluded.received_at""",
                     (recording_id, sender["contract_version"], sender["recorder"],
                      sender["recorder_version"], sender["twinlab_commit"], now()))  # fmt: skip
        db().execute("""INSERT INTO machine_contracts(machine_id, contract_version, recorder, updated_at) VALUES (?, ?, ?, ?)
                        ON CONFLICT(machine_id) DO UPDATE SET contract_version = excluded.contract_version,
                        recorder = excluded.recorder, updated_at = excluded.updated_at""",
                     (machine["id"], sender["contract_version"],
                      f"{sender['recorder']} {sender['recorder_version']}", now()))  # fmt: skip
    # the recording's page in the web app (the same address the server-rendered page had)
    url = str(request.base_url).rstrip("/") + "/recordings/" + quote(recording_id, safe="")
    return {"recording_id": recording_id, "mapped": mapped, "discovered": discovered, "url": url}


# -- what a recording and an application show ----------------------------------------------------


def recording_refs(recording_id):
    ref = db().execute("""
        SELECT session_refs.*, machines.name AS machine, automations.name AS automation
        FROM session_refs LEFT JOIN machines ON machines.id = session_refs.machine_id
        LEFT JOIN automations ON automations.id = session_refs.automation_id
        WHERE recording_id = ?
    """, (recording_id,)).fetchone()
    if ref is None:
        return None
    apps = db().execute("""
        SELECT recording_apps.*, apps.name, apps.status FROM recording_apps
        JOIN apps ON apps.id = recording_apps.app_id WHERE recording_id = ? ORDER BY apps.name
    """, (recording_id,)).fetchall()
    payload = json.loads(ref["payload"])

    def name_of(key):  # a web origin's steps may have gone to several applications by path
        if key == "presence":
            return "Other applications"
        found = [a["name"] for a in apps if a["key"] == key or a["key"].startswith(key + "/")]
        return ", ".join(dict.fromkeys(found)) or key

    timeline = [dict(run, name=name_of(run["app"])) for run in payload["timeline"]]
    sender = db().execute("SELECT * FROM recording_contracts WHERE recording_id = ?",
                          (recording_id,)).fetchone()  # fmt: skip
    status = recording_status(db(), recording_id)
    return {"ref": ref, "session": payload["session"], "timeline": timeline, "contract": sender,
            "status": status,
            "ranges": compl.ranges(status["completeness"], payload["session"].get("started"),
                                   payload["session"].get("ended")),
            "apps": [_without_screens(dict(a, steps=json.loads(a["flow"]))) for a in apps]}  # fmt: skip


def app_recordings(app_id):
    rows = db().execute("""
        SELECT recording_apps.*, recordings.name AS recording_name, session_refs.received_at,
               machines.name AS machine
        FROM recording_apps JOIN recordings ON recordings.id = recording_apps.recording_id
        LEFT JOIN session_refs ON session_refs.recording_id = recording_apps.recording_id
        LEFT JOIN machines ON machines.id = session_refs.machine_id
        WHERE recording_apps.app_id = ? ORDER BY session_refs.received_at DESC NULLS LAST
    """, (app_id,)).fetchall()
    return [_without_screens(dict(r, steps=json.loads(r["flow"]))) for r in rows]


def _without_screens(row):
    """A recording_apps row for the pages: its screen structure goes to the build pack, not here."""
    row.pop("screens", None)
    return row


def app_automations(app_id):
    return db().execute("""
        SELECT automations.* FROM automations JOIN automation_apps ON automation_apps.automation_id = automations.id
        WHERE automation_apps.app_id = ? ORDER BY automations.name
    """, (app_id,)).fetchall()


@routes.get(V1 + "/recordings", auth.signed_in, response_model=m.RecordingList)
def recordings():
    """Restricted evidence: the recordings the caller added or that are linked to an application
    the caller owns."""
    rows = db().execute("""
        SELECT recordings.id, recordings.name, session_refs.received_at, machines.name AS machine,
               automations.name AS automation,
               (SELECT string_agg(apps.name || ' (' || recording_apps.level || ')', ', ')
                FROM recording_apps JOIN apps ON apps.id = recording_apps.app_id
                WHERE recording_apps.recording_id = recordings.id) AS apps
        FROM recordings LEFT JOIN session_refs ON session_refs.recording_id = recordings.id
        LEFT JOIN machines ON machines.id = session_refs.machine_id
        LEFT JOIN automations ON automations.id = session_refs.automation_id
        ORDER BY session_refs.received_at DESC NULLS LAST, recordings.name
    """).fetchall()
    from twinlab2.routes_portal import page_args, paged

    limit, offset = page_args()
    words = (g.request.query_params.get("q") or "").casefold().split()
    mine = [r for r in rows if all(w in r["name"].casefold() for w in words) and reviewer_of_recording(r["id"])]
    page, where = paged(mine, limit, offset)
    # `apps` (one text) is deprecated for `apps_list` (api/openapi.yaml: Deprecations)
    current().response_headers["Deprecation"] = "true"
    return {"page": where,
            "recordings": [{"id": r["id"], "name": r["name"], "machine": r["machine"], "received_at": r["received_at"],
                            "automation": r["automation"], "apps": r["apps"], "apps_list": recorded_apps(r["id"]),
                            "status": shapes.recording_status(recording_status(db(), r["id"]))} for r in page]}


def recorded_apps(recording_id):
    """[{app_id, name, mode}] of the applications a recording has, by name."""
    return [{"app_id": a["id"], "name": a["name"], "mode": a["level"]} for a in db().execute(
        "SELECT apps.id, apps.name, recording_apps.level FROM recording_apps JOIN apps ON apps.id = recording_apps.app_id "
        "WHERE recording_apps.recording_id = ? ORDER BY apps.name, apps.id", (recording_id,)).fetchall()]


@routes.post(V1 + "/recordings/{recording_id}/completeness/accept", auth.signed_in, response_model=m.RecordingDetail)
def accept_recording(recording_id: str):
    """The owner accepts a partial or failed recording as it is: it no longer holds up the
    journey of the applications it recorded. A resend with other completeness undoes it.
    Restricted evidence: its uploader and the owners of the applications it is linked to."""
    from twinlab2.routes_portal import recording_detail

    get_recording(recording_id)
    ensure_reviewer(recording_id, "Only the recording's uploader and the owners of its applications can accept it.")
    status = recording_status(db(), recording_id)
    if status["completeness"] is None:
        fail(404, "This recording has no completeness report to accept.")
    with db():
        db().execute("UPDATE recording_completeness SET accepted_by = ?, accepted_at = ? WHERE recording_id = ?",
                     (current_user(), now(), recording_id))  # fmt: skip
        linked = [r[0] for r in db().execute(
            "SELECT app_id FROM evidence WHERE recording_id = ? UNION SELECT app_id FROM recording_apps "
            "WHERE recording_id = ?", (recording_id, recording_id))]
        for app_id in linked or [None]:
            record("recording.accept", "recording", recording_id, app_id=app_id,
                   overall=status.get("overall") if isinstance(status, dict) else None)
    return recording_detail(recording_id)


# -- an application's identities --------------------------------------------------------------------


@routes.post(V1 + "/apps/{app_id:int}/identities", require_role("owner"), response_model=m.AppSetup, status_code=201,
             idempotent="stored")
def add_identity(app_id: int, data: m.IdentityInput):
    from twinlab2.routes_portal import app_setup

    get_app(app_id)
    key = normalise_key(data.key)
    if key is None:
        fail(400, "Enter an executable name (CaseDesk.exe), a web address, a mainframe host "
                  "(tn3270://host:port), or desktop:Company/Product.")
    other = db().execute("SELECT app_id FROM app_identities WHERE key = ?", (key,)).fetchone()
    if other is not None and other["app_id"] != app_id:
        fail(409, "Another application already has this identity: merge them instead.")
    if key.startswith("web:"):
        mode = db().execute("SELECT url_match FROM apps WHERE id = ?", (app_id,)).fetchone()[0]
        if web_clash(app_id, key, mode) is not None:
            fail(409, "Another application already matches this host and path: add a longer path, "
                      "or merge them if they are the same application.")
    surface = key.split(":", 1)[0] if key.startswith(("web:", "mainframe:")) else "desktop"
    with db():
        db().execute("INSERT INTO app_identities(key, app_id, surface, facts) VALUES (?, ?, ?, '{}') ON CONFLICT DO NOTHING",
                     (key, app_id, surface))
        db().execute("UPDATE apps SET surface = ? WHERE id = ? AND surface = ''", (surface, app_id))
        readiness_cache.forget(db(), app_id)
    return app_setup(app_id)


@routes.put(V1 + "/apps/{app_id:int}/url-match", require_role("owner"), response_model=m.AppSetup)
def set_url_match(app_id: int, data: m.UrlMatchInput):
    """The owner's override of how the application's web addresses match."""
    from twinlab2.routes_portal import app_setup, change_app

    get_app(app_id)
    mode = data.url_match
    for row in identities(app_id):
        if row["key"].startswith("web:") and web_clash(app_id, row["key"], mode) is not None:
            fail(409, "Another application already matches this whole host.")
    with db():
        change_app(app_id, data.model_dump(exclude_unset=True), app_setup)
        db().execute("UPDATE apps SET url_match = ? WHERE id = ?", (mode, app_id))
    return app_setup(app_id)


# -- merging two applications ----------------------------------------------------------------------
# Moved to the surviving application: identities, recorded flows, evidence links,
# automations, scenarios and historical build briefs with their packages. An evidence link both
# applications have to the same recording is combined (shown in the preview first); nothing
# is dropped. Rows in any other table that refers to the application block the merge.

# app_roles: the target's assignments stay; the source's are dropped (merging never widens
# who may see the target). Merging needs the owner role on both applications.
MOVED = {"app_identities", "recording_apps", "evidence", "automation_apps", "briefs", "scenarios", "app_roles"}


def combine_links(keep, gone, gone_name):
    """One evidence link from two links to the same recording. A reviewed selection is never
    widened by an unreviewed one: those events become suggestions for the owner instead."""
    def sel(row):
        return set(json.loads(row["selection"]))

    def sug(row):
        return set(json.loads(row["suggested"]))

    if keep["reviewed"] and gone["reviewed"] or not keep["reviewed"] and not gone["reviewed"]:
        selection = sel(keep) | sel(gone)
        suggested = sug(keep) | sug(gone)
    else:
        reviewed, other = (keep, gone) if keep["reviewed"] else (gone, keep)
        selection = sel(reviewed)
        suggested = sug(keep) | sug(gone) | sel(other)
    notes = [n for n in (keep["notes"].strip(), gone["notes"].strip()) if n]
    if len(notes) == 2 and notes[0] != notes[1]:
        notes = [notes[0], f"Merged from {gone_name}: {notes[1]}"]
    return {"selection": sorted(selection), "suggested": sorted(suggested - selection),
            "notes": "\n\n".join(dict.fromkeys(notes)),
            "reviewed": 1 if keep["reviewed"] or gone["reviewed"] else 0}


def merge_plan(app_id, into):
    source = db().execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone()
    target = db().execute("SELECT * FROM apps WHERE id = ?", (into,)).fetchone() if into is not None else None
    if source is None:
        fail(404, "There is no such application.")
    if target is None or into == app_id:
        fail(400, "Choose the application to merge into.")
    blockers = []
    for table in sorted(db().tables()):
        if table in MOVED:
            continue
        for fk in db().foreign_keys(table):
            if fk["table"] == "apps":
                count = db().execute(f'SELECT COUNT(*) FROM "{table}" WHERE "{fk["from"]}" = ?',
                                     (app_id,)).fetchone()[0]  # fmt: skip
                if count:
                    blockers.append(f"{count} {table.replace('_', ' ')}")
    target_links = {r["recording_id"]: r for r in db().execute(
        "SELECT * FROM evidence WHERE app_id = ?", (into,))}  # fmt: skip
    moving, combined = [], []
    for row in db().execute("""SELECT evidence.*, recordings.name FROM evidence
                               JOIN recordings ON recordings.id = evidence.recording_id
                               WHERE app_id = ? ORDER BY recordings.name""", (app_id,)).fetchall():  # fmt: skip
        keep = target_links.get(row["recording_id"])
        if keep is None:
            moving.append({"recording_id": row["recording_id"], "name": row["name"],
                           "selected": len(json.loads(row["selection"]))})  # fmt: skip
        else:
            order = {e["event_id"]: e["seq"] for e in json.loads(db().execute(
                "SELECT events FROM recordings WHERE id = ?", (row["recording_id"],)).fetchone()[0])}
            link = combine_links(keep, row, source["name"])
            combined.append({"recording_id": row["recording_id"], "name": row["name"],
                             # what the page shows: the events' display numbers, in order
                             "shown": sorted(order.get(i, 0) for i in link["selection"]),
                             "shown_suggested": sorted(order.get(i, 0) for i in link["suggested"]),
                             "source": json.loads(row["selection"]), "target": json.loads(keep["selection"]),
                             "source_notes": row["notes"], "target_notes": keep["notes"],
                             **link})  # fmt: skip
    flows = db().execute("""SELECT recording_apps.recording_id, recording_apps.key, recordings.name,
                                   EXISTS (SELECT 1 FROM recording_apps t WHERE t.app_id = ?
                                           AND t.recording_id = recording_apps.recording_id
                                           AND t.key = recording_apps.key) AS shared
                            FROM recording_apps JOIN recordings ON recordings.id = recording_apps.recording_id
                            WHERE app_id = ? ORDER BY recordings.name""", (into, app_id)).fetchall()  # fmt: skip
    plan = {
        "source": {"id": source["id"], "name": source["name"], "url_match": source["url_match"]},
        "target": {"id": target["id"], "name": target["name"], "url_match": target["url_match"]},
        "identities": [r["key"] for r in identities(app_id)],
        "flows": [{"recording_id": r["recording_id"], "name": r["name"], "key": r["key"],
                   "shared": bool(r["shared"])} for r in flows],  # fmt: skip
        "links": moving, "combined": combined,
        "automations": [r["name"] for r in app_automations(app_id)],
        "briefs": [{"revision": r["revision"], "package": bool(r["package"])} for r in db().execute(
            """SELECT revision, EXISTS (SELECT 1 FROM brief_packages p WHERE p.revision = briefs.revision)
               AS package FROM briefs WHERE app_id = ? ORDER BY revision""", (app_id,))],  # fmt: skip
        "scenarios": [r["name"] for r in db().execute(
            "SELECT name FROM scenarios WHERE app_id = ? ORDER BY position, id", (app_id,))],
        "blockers": blockers,
    }
    plan["fingerprint"] = sha(json.dumps(plan, sort_keys=True))[:16]
    return plan


@routes.get(V1 + "/apps/{app_id:int}/merge-plan", require_role("owner"), response_model=m.MergePlan)
def merge_preview(app_id: int, into: int | None = None):
    """Owner of both applications; nothing changes."""
    if into is not None:
        auth.ensure("owner", app_id=into)
    return merge_plan(app_id, into)


@routes.post(V1 + "/apps/{app_id:int}/merge", require_role("owner"), response_model=m.AppRef)
def merge_app(app_id: int, data: m.MergeInput):
    into = data.into
    auth.ensure("owner", app_id=into)  # the owner role on both applications
    plan = merge_plan(app_id, into)
    if data.confirm != plan["fingerprint"]:
        # not confirmed, or the applications changed since the preview: preview again
        fail(409, "The applications changed since the preview: review the merge again.", "plan_changed")
    if plan["blockers"]:
        fail(400, "This application cannot be merged: " + ", ".join(plan["blockers"]) + ".")
    with db():
        readiness_cache.forget(db(), app_id, into)
        db().execute("UPDATE app_identities SET app_id = ? WHERE app_id = ?", (into, app_id))
        for row in db().execute("SELECT * FROM recording_apps WHERE app_id = ?", (app_id,)).fetchall():
            keep = db().execute("SELECT flow, screens FROM recording_apps WHERE recording_id = ? AND app_id = ? AND key = ?",
                                (row["recording_id"], into, row["key"])).fetchone()  # fmt: skip
            if keep is None:
                db().execute("UPDATE recording_apps SET app_id = ? WHERE recording_id = ? AND app_id = ? AND key = ?",
                             (into, row["recording_id"], app_id, row["key"]))  # fmt: skip
                continue
            steps = {st["event_id"]: st for st in json.loads(row["flow"])}
            steps.update({st["event_id"]: st for st in json.loads(keep["flow"])})
            merged = sorted(steps.values(), key=lambda st: (st.get("seq", st["n"]), st["event_id"]))
            screens = structure.merge(json.loads(keep["screens"]), json.loads(row["screens"]))
            db().execute("UPDATE recording_apps SET flow = ?, screens = ? WHERE recording_id = ? AND app_id = ? AND key = ?",
                         (json.dumps(merged), json.dumps(screens), row["recording_id"], into, row["key"]))
            db().execute("DELETE FROM recording_apps WHERE recording_id = ? AND app_id = ? AND key = ?",
                         (row["recording_id"], app_id, row["key"]))  # fmt: skip
        for link in plan["combined"]:
            db().execute("""UPDATE evidence SET selection = ?, suggested = ?, notes = ?, reviewed = ?
                            WHERE app_id = ? AND recording_id = ?""",
                         (json.dumps(link["selection"]), json.dumps(link["suggested"]), link["notes"],
                          link["reviewed"], into, link["recording_id"]))  # fmt: skip
            db().execute("DELETE FROM evidence WHERE app_id = ? AND recording_id = ?", (app_id, link["recording_id"]))
        db().execute("UPDATE evidence SET app_id = ? WHERE app_id = ?", (into, app_id))
        db().execute("INSERT INTO automation_apps(automation_id, app_id) SELECT automation_id, ? FROM automation_apps "
                     "WHERE app_id = ? ON CONFLICT DO NOTHING", (into, app_id))  # fmt: skip
        db().execute("DELETE FROM automation_apps WHERE app_id = ?", (app_id,))
        db().execute("UPDATE briefs SET app_id = ? WHERE app_id = ?", (into, app_id))
        # scenarios follow the target's own, in their order
        db().execute("""UPDATE scenarios SET app_id = ?, position = position +
                        (SELECT COALESCE(MAX(position), 0) FROM scenarios WHERE app_id = ?) WHERE app_id = ?""",
                     (into, into, app_id))  # fmt: skip
        db().execute("DELETE FROM app_roles WHERE app_id = ?", (app_id,))
        db().execute("DELETE FROM evidence_removals WHERE app_id = ?", (app_id,))
        details = {k: plan[k] for k in ("identities", "links", "combined", "automations", "briefs", "scenarios")}
        for logged in (into, app_id):
            record("app.merge", "app", app_id, app_id=logged, source=plan["source"], target=plan["target"],
                   fingerprint=plan["fingerprint"], **details)
        db().execute("DELETE FROM apps WHERE id = ?", (app_id,))
    return {"id": plan["target"]["id"], "name": plan["target"]["name"]}


# -- automations ------------------------------------------------------------------------------------


@routes.get(V1 + "/automations", auth.signed_in, response_model=m.AutomationPage)
def automations():
    rows = db().execute("""
        SELECT automations.*, (SELECT string_agg(apps.name, ', ') FROM automation_apps
            JOIN apps ON apps.id = automation_apps.app_id WHERE automation_id = automations.id) AS apps
        FROM automations ORDER BY name
    """).fetchall()
    return {"automations": [shapes.automation(r) for r in rows],
            "apps": [shapes.ref(a) for a in db().execute("SELECT * FROM apps ORDER BY name").fetchall()
                     if auth.can("owner", app_id=a["id"])]}  # fmt: skip


def save_automation(name, kind, notes, app_ids):
    with db():
        row = db().execute("SELECT id FROM automations WHERE name = ?", (name,)).fetchone()
        if row is None:
            automation_id = db().execute("INSERT INTO automations(name, kind, notes) VALUES (?, ?, ?) RETURNING id",
                                         (name, kind, notes)).fetchone()[0]  # fmt: skip
        else:
            automation_id = row["id"]
            db().execute("UPDATE automations SET kind = ?, notes = ? WHERE id = ?", (kind, notes, automation_id))
        for app_id in app_ids:
            db().execute("INSERT INTO automation_apps(automation_id, app_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
                         (automation_id, app_id))
    return automation_id


def automation_detail(automation_id):
    item = db().execute("""
        SELECT automations.*, (SELECT string_agg(apps.name, ', ') FROM automation_apps
            JOIN apps ON apps.id = automation_apps.app_id WHERE automation_id = automations.id) AS apps
        FROM automations WHERE id = ?""", (automation_id,)).fetchone()
    if item is None:
        fail(404, "There is no such automation.")
    apps = db().execute("""SELECT apps.* FROM apps JOIN automation_apps ON automation_apps.app_id = apps.id
                           WHERE automation_id = ? ORDER BY name""", (automation_id,)).fetchall()  # fmt: skip
    recs = db().execute("""SELECT recordings.id, recordings.name, session_refs.received_at FROM session_refs
                           JOIN recordings ON recordings.id = session_refs.recording_id
                           WHERE session_refs.automation_id = ? ORDER BY received_at DESC NULLS LAST""",
                        (automation_id,)).fetchall()  # fmt: skip
    return {"automation": shapes.automation(item),
            "apps": [shapes.ref(a) for a in apps if auth.can("viewer", app_id=a["id"])],
            "recordings": [{"id": r["id"], "name": r["name"], "received_at": r["received_at"]}
                           for r in recs if reviewer_of_recording(r["id"])]}


@routes.post(V1 + "/automations", auth.signed_in, response_model=m.AutomationDetail, status_code=201,
             idempotent="stored")
def add_automation(data: m.NewAutomation):
    name = data.name.strip()[:120]
    if not name:
        fail(400, "Name the automation.")
    ids = list(data.apps or [])
    known = {r["id"] for r in db().execute("SELECT id FROM apps")}
    if not set(ids) <= known:
        fail(400, "Unknown application.")
    owned(ids)
    automation_id = save_automation(name, (data.kind or "").strip()[:40], (data.notes or "").strip()[:2000], ids)
    return automation_detail(automation_id)


@routes.post(V1 + "/automations/import", auth.signed_in, response_model=m.AutomationDetail, status_code=201)
def import_automation(file: UploadFile | None = File(None)):
    """A Twin Lab automation.yaml: its name, type, and the applications it `touches`, matched
    to catalog applications by name (case-insensitive). Unmatched names are listed in notes."""
    if file is None:
        fail(400, "Choose an automation.yaml.")
    try:
        doc = yaml.safe_load(file.file.read(256 * 1024).decode("utf-8")) or {}
    except (yaml.YAMLError, UnicodeDecodeError):
        fail(400, "Not a YAML file.")
    if not isinstance(doc, dict) or not isinstance(doc.get("id") or doc.get("name"), str):
        fail(400, "The file has no id or name.")
    names = [str(t.get("app")) for t in doc.get("touches") or [] if isinstance(t, dict) and t.get("app")]
    # only applications the user owns are linked: an import never reaches other applications
    by_name = {r["name"].lower(): r["id"] for r in db().execute("SELECT id, name FROM apps")
               if auth.can("owner", app_id=r["id"])}
    ids = [by_name[n.lower()] for n in names if n.lower() in by_name]
    missing = [n for n in names if n.lower() not in by_name]
    notes = "Imported from automation.yaml." + (f" Applications not in the catalog: {', '.join(missing)}." if missing else "")
    automation_id = save_automation(str(doc.get("name") or doc.get("id"))[:120], str(doc.get("type") or "")[:40],
                                    notes, ids)  # fmt: skip
    return automation_detail(automation_id)


@routes.get(V1 + "/automations/{automation_id:int}", auth.signed_in, response_model=m.AutomationDetail)
def automation(automation_id: int):
    return automation_detail(automation_id)

