"""The /api/v1 routes of the portal's own pages: sign-in, the catalog and an application (roles,
history, setup), restricted evidence (recorded flows, attachments, scenarios), recordings and
their assignment to applications, builder packages, and releases. api/openapi.yaml is their
contract; api_models.py (generated from it) types every request and response.

The rules live in the domain modules (portal: releases, tests and publishing; packages:
freezing; buildpack; scenarios; verification; audit) and in auth.py (roles); these routes read
the request, call them, and answer in the contract's shapes.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import uuid
from urllib.parse import quote, urlsplit

from fastapi import File, Form, Response, UploadFile
from fastapi.responses import RedirectResponse, StreamingResponse
from starlette.datastructures import MultiDict

from twinlab2 import api_models as m
from twinlab2 import artifacts
from twinlab2 import audit
from twinlab2 import demo_target
from twinlab2 import auth
from twinlab2 import buildpack
from twinlab2 import builds
from twinlab2 import completeness
from twinlab2 import event_ids
from twinlab2 import journey
from twinlab2 import pack_validator
from twinlab2 import packages
from twinlab2 import portal
from twinlab2 import readiness_cache
from twinlab2 import recording_export
from twinlab2 import redact
from twinlab2 import registry
from twinlab2 import revisions
from twinlab2 import scenarios
from twinlab2 import shapes
from twinlab2 import uploads
from twinlab2 import verification as commit_checks
from twinlab2 import verifications
from twinlab2 import idempotency
from twinlab2.auth import require_role
from twinlab2.portal import (ATTACHMENT_TYPES, RESULTS, VERSION_PATH, PublishConflict, acquire_publish,
                             brief_scenarios, deployment_label, deployment_reason, ordered, publish,
                             record_test, recording_projection, release_publish, upload_view, verification)
from twinlab2.rest import Problem, Routes, db, fail, form_token, g
from twinlab2.routes_registry import app_automations, app_recordings, identities, recording_refs
from twinlab2.views import config, ensure_reviewer, get_app, get_recording, record, reviewer_of_recording, visible

V1 = "/api/v1"
routes = Routes()


def payload(model) -> dict:
    """The fields a request body sent (a validated model), as a dict."""
    return model.model_dump(exclude_unset=True, by_alias=True)


def text(data, key, limit=None, default=""):
    value = data.get(key, default)
    value = default if value is None else value
    if not isinstance(value, str):
        fail(400, f"“{key}” must be text.", errors=[{"field": key, "message": "must be text"}])
    value = value.strip()
    return value[:limit] if limit else value


def integer(data, key, default=None):
    value = data.get(key, default)
    return default if value is None else value


def disposition(filename: str) -> str:
    """`attachment; filename=…` (RFC 6266): a plain name as it is, any other (non-ASCII, such as
    `請求-システム-4-rev1-….zip`) as an ASCII fallback plus `filename*` in UTF-8."""
    if re.fullmatch(r"[A-Za-z0-9._-]+", filename):
        return f"attachment; filename={filename}"
    fallback = filename.encode("ascii", "ignore").decode().replace('"', "").replace("\\", "") or "download"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


def download(content: bytes, mimetype: str, filename: str):
    return Response(content, media_type=mimetype, headers={
        "Content-Disposition": disposition(filename), "Cache-Control": "no-store"})


def links(app_id):
    return db().execute("""
        SELECT recordings.*, evidence.selection, evidence.notes, evidence.reviewed, evidence.suggested
        FROM evidence JOIN recordings ON recordings.id = evidence.recording_id
        WHERE evidence.app_id = ? ORDER BY recordings.id
    """, (app_id,)).fetchall()


def attachments(app_id):
    return db().execute(
        "SELECT * FROM attachments WHERE app_id = ? ORDER BY id", (app_id,),
    ).fetchall()


def releases(app_id):
    return db().execute(
        "SELECT id, version, revision, commit_sha, archive_sha, state FROM releases WHERE app_id = ? ORDER BY id DESC",
        (app_id,),
    ).fetchall()


def release_summaries(app_id):
    labels = registry.release_labels(db(), app_id)
    return [shapes.release_summary(r, labels[r["id"]]) for r in releases(app_id)]


def tested_releases():
    """Per app, its latest Tested or Published release, the results of its test, and the
    deployment to open (a verified one first)."""
    rows = db().execute("""
        SELECT releases.id, releases.app_id, releases.version, releases.state, releases.commit_sha,
               release_tests.results, release_tests.platform
        FROM releases JOIN release_tests ON release_tests.release_id = releases.id
        WHERE releases.id = (SELECT MAX(id) FROM releases AS later
                             WHERE later.app_id = releases.app_id AND later.state IN ('tested', 'published'))
    """).fetchall()
    found = {}
    for row in rows:
        label = registry.release_labels(db(), row["app_id"])[row["id"]]
        results = [item["result"] for item in json.loads(row["results"])]
        deployment = db().execute(
            "SELECT * FROM deployments WHERE release_id = ? ORDER BY verification = 'verified' DESC, environment",
            (row["id"],),
        ).fetchone()
        found[row["app_id"]] = {
            "id": row["id"], "version": row["version"], "label": label, "commit_sha": row["commit_sha"],
            "state": row["state"], "platform": row["platform"] or "",
            "counts": {result: results.count(result) for result in RESULTS},
            "deployment": shapes.deployment(deployment, deployment_label, deployment_reason) if deployment else None,
        }
    return found


def release_states(app_id=None):
    """{app_id: the states of its releases}, of one application or of all."""
    sql, args = "SELECT app_id, state FROM releases", ()
    if app_id is not None:
        sql, args = sql + " WHERE app_id = ?", (app_id,)
    found = {}
    for row in db().execute(sql, args).fetchall():
        found.setdefault(row["app_id"], set()).add(row["state"])
    return found


def app_summary(row, tested=None, states=None):
    states = states if states is not None else release_states(row["id"])
    return {"id": row["id"], "name": row["name"], "status": row["status"],
            "status_label": shapes.status_label(row["status"]),
            "family": journey.family(row["status"], states.get(row["id"], ())), "owner": row["owner"] or "",
            "purpose": row["purpose"] or "", "my_role": auth.role_of(db(), row["id"], g.user),
            "journey": shapes.journey(journey.for_app(db(), row)),
            "tested_release": (tested if tested is not None else tested_releases()).get(row["id"])}


def app_detail(app_id):
    item = get_app(app_id)
    builder = auth.can("builder", app_id=app_id)
    return dict(app_summary(item), revision=item["revision"], git_url=item["git_url"] or "", url_match=item["url_match"] or "path",
                surface=item["surface"] or "", releases=release_summaries(app_id),
                packages=[shapes.package_summary(r) for r in packages.revisions(db(), app_id)] if builder else [],
                # what an owner may choose: "discovered" is the state before a decision, never a choice
                statuses=[shapes.choice(s, shapes.status_label(s)) for s in journey.OWNER_CHOICES])

# -- session -------------------------------------------------------------------------------


def session_view():
    user, admin = auth.resolve()
    roles = []
    if user is not None:
        roles = [{"app_id": r["app_id"], "app_name": r["name"], "role": r["role"]} for r in db().execute(
            "SELECT app_roles.app_id, apps.name, app_roles.role FROM app_roles JOIN apps ON apps.id = app_roles.app_id "
            "WHERE app_roles.user_name = ? ORDER BY apps.name, apps.id", (user,))]
    return {"signed_in": user is not None, "user": user, "is_admin": user is not None and bool(admin),
            "auth_mode": auth.auth_mode(), "access_mode": auth.access_mode(),
            "login_available": auth.login_available(),
            "csrf_token": form_token(), "roles": roles,
            "can_create_apps": user is not None and auth.may_create_apps(user, bool(admin)),
            "home_role": auth.home_role(user, bool(admin)) if user is not None else "none"}


@routes.get(V1 + "/session", auth.public, response_model=m.Session)
def get_session():
    return session_view()


@routes.post(V1 + "/session", auth.public, response_model=m.Session)
def sign_in(body: m.SignIn):
    if not auth.login_available():
        fail(404, "There is no local sign-in here.")
    data = payload(body)
    name = text(data, "user", 80)
    via = auth.login_user(name, data.get("password") if isinstance(data.get("password"), str) else "") if name else None
    if via is None:
        fail(401, "Wrong name or password.", "wrong_credentials")
    session = g.session
    with db():
        if session.get("sid"):
            auth.revoke_session(db(), session["sid"])
        sid = auth.start_session(db(), name, via, g.settings.session_hours)
    session.clear()  # a new form token too (session_view makes it): one read before sign-in is no use after
    session["user"], session["sid"] = name, sid
    return session_view()


@routes.delete(V1 + "/session", auth.public, status_code=204)
def sign_out():
    session = g.session
    if session.get("sid"):  # revoked on the server: a copy of the cookie no longer signs in
        with db():
            auth.revoke_session(db(), session["sid"])
    session.clear()  # and the form token with it: GET /api/v1/session hands out a new one
    return Response(status_code=204)


# -- applications --------------------------------------------------------------------------


def page_args(default=50, maximum=200):
    """`limit` and `offset` of a paginated list (api/openapi.yaml: Pagination), or 400."""
    query = g.request.query_params
    out = []
    for name, fallback, low, high in (("limit", default, 1, maximum), ("offset", 0, 0, None)):
        raw = query.get(name)
        try:
            value = fallback if raw in (None, "") else int(raw)
        except ValueError:
            value = None
        if value is None or value < low or (high is not None and value > high):
            fail(400, f"“{name}” must be a whole number from {low}" + (f" to {high}." if high else " up."),
                 errors=[{"field": name, "message": "out of range"}])
        out.append(value)
    return out[0], out[1]


def paged(items, limit, offset):
    """One page of `items` and its `page` object."""
    total = len(items)
    return items[offset:offset + limit], {"total": total, "limit": limit, "offset": offset,
                                          "next_offset": offset + limit if offset + limit < total else None}


APP_SORTS = {"name": lambda a: a["name"].casefold(), "owner": lambda a: (a["owner"].casefold(), a["name"].casefold()),
             "status": lambda a: (a["status"], a["name"].casefold()),
             "stage": lambda a: (list(journey.JOURNEY).index(a["journey"]["stage"])
                                 if a["journey"]["stage"] in journey.JOURNEY else len(journey.JOURNEY),
                                 a["name"].casefold())}


@routes.get(V1 + "/apps", auth.signed_in, response_model=m.Catalog)
def list_apps():
    query = g.request.query_params
    limit, offset = page_args()
    sort = query.get("sort") or ""
    if sort and sort.lstrip("-") not in APP_SORTS:
        fail(400, "Sort by name, owner, status or stage.", errors=[{"field": "sort", "message": "unknown sort"}])
    rows = visible(db().execute("SELECT * FROM apps ORDER BY status = 'not_replicated', name").fetchall())
    tested, states = tested_releases(), release_states()
    apps = [app_summary(row, tested, states) for row in rows]
    owners = sorted({o.strip() for a in apps for o in a["owner"].split(",") if o.strip()}, key=str.casefold)
    stages = [s for s in journey.JOURNEY if any(a["journey"]["stage"] == s for a in apps)]
    stages += sorted({a["journey"]["stage"] for a in apps} - set(stages))
    words = (query.get("q") or "").casefold().split()
    if words:
        apps = [a for a in apps if all(w in f"{a['name']} {a['purpose']} {a['owner']}".casefold() for w in words)]
    if query.get("owner"):
        apps = [a for a in apps if query["owner"] in [o.strip() for o in a["owner"].split(",")]]
    if query.get("status"):
        apps = [a for a in apps if a["status"] == query["status"]]
    if query.get("stage"):
        apps = [a for a in apps if a["journey"]["stage"] == query["stage"]]
    if sort:
        apps.sort(key=APP_SORTS[sort.lstrip("-")], reverse=sort.startswith("-"))
        if sort.lstrip("-") == "stage":  # out of scope (no journey stage) last, either way
            apps.sort(key=lambda a: a["journey"]["stage"] not in journey.JOURNEY)
    # grouped by status family, the sort within each; a collapsed family is counted, not paged
    order = list(journey.FAMILY_LABELS)
    apps.sort(key=lambda a: order.index(a["family"]))
    collapsed = set(query.getlist("collapsed"))
    if collapsed - set(order):
        fail(400, "Collapse decide, build, tested, published or not_replicated.",
             errors=[{"field": "collapsed", "message": "unknown family"}])
    counts = {f: sum(a["family"] == f for a in apps) for f in order}
    apps = [a for a in apps if a["family"] not in collapsed]
    starts = {}
    for index, a in enumerate(apps):
        starts.setdefault(a["family"], index)
    groups = [{"family": f, "label": label, "count": counts[f], "collapsed": f in collapsed,
               "offset": starts.get(f)} for f, label in journey.FAMILY_LABELS.items()]
    page, where = paged(apps, limit, offset)
    recordings = []
    for r in db().execute("SELECT id, name FROM recordings ORDER BY name").fetchall():
        if len(recordings) == 50:
            break
        if reviewer_of_recording(r["id"]):
            recordings.append({"id": r["id"], "name": r["name"]})
    return {"apps": page, "page": where, "groups": groups, "filters": {"owners": owners, "stages": stages},
            "recordings": recordings, "journey_titles": list(journey.JOURNEY)}


def change_app(app_id, data, current=None):
    """Count a change of the application's own row (name, purpose, setup): refused with 409
    `conflict` and the current version when the change names a revision that is no longer the
    current one (revisions.py). Call it first, inside the change's transaction."""
    want = revisions.expected(data)
    sql, args = "UPDATE apps SET revision = revision + 1 WHERE id = ?", [app_id]
    if want is not None:
        sql, args = sql + " AND revision = ?", args + [want]
    if db().execute(sql, tuple(args)).rowcount != 1:
        raise revisions.conflict((current or app_detail)(app_id), "this application")
    readiness_cache.forget(db(), app_id)


@routes.post(V1 + "/apps", auth.signed_in, response_model=m.AppRef, status_code=201,
             idempotent="stored")
def create_app_(body: m.NewApp):
    if not auth.may_create_apps():
        fail(403, {"owners": "Adding an application needs the owner role on an application, or a portal admin.",
                   "admins": "Only a portal admin can add an application here."}.get(
                       g.settings.app_creators, "You cannot add an application."), "role")
    data = payload(body)
    name = text(data, "name", 200)
    owner = text(data, "owner", 120)
    purpose = text(data, "purpose", 500)
    git_url = text(data, "git_url", 500)
    if not name:
        fail(400, "Enter a demo app name.", errors=[{"field": "name", "message": "required"}])
    with db():
        app_id = db().execute(
            "INSERT INTO apps(name, git_url, requirements, owner, purpose) VALUES (?, ?, ?, ?, ?) RETURNING id",
            (name, git_url, "", "", purpose),
        ).fetchone()[0]
        record("app.create", "app", app_id, app_id=app_id, name=name)
        # whoever creates an application owns it; a named owner is assigned too
        for person in dict.fromkeys([g.user, owner]):
            if person and auth.valid_name(person):
                auth.assign(db(), app_id, person, "owner", g.user, registry.now())
                record("role.assign", "role", f"{app_id}/{person}", app_id=app_id, user=person,
                       role="owner", previous=None)
    return {"id": app_id, "name": name}


@routes.get(V1 + "/apps/{app_id:int}", require_role("viewer"), response_model=m.AppDetail)
def get_app_(app_id: int):
    return app_detail(app_id)


@routes.patch(V1 + "/apps/{app_id:int}", require_role("owner"), response_model=m.AppDetail)
def update_app(app_id: int, body: m.AppUpdate):
    get_app(app_id)
    data = payload(body)
    status = data.get("status")
    if status is not None and status not in registry.STATUSES:
        fail(400, "Unknown status.", errors=[{"field": "status", "message": "unknown status"}])
    with db():
        change_app(app_id, data)
        # only the fields sent change
        for column, limit in (("purpose", 500), ("git_url", 500)):
            if column in data:
                db().execute(f"UPDATE apps SET {column} = ? WHERE id = ?", (text(data, column, limit), app_id))
        name = text(data, "name", 120) if "name" in data else ""
        if "name" in data and not name:
            fail(400, "An application needs a name.", errors=[{"field": "name", "message": "required"}])
        if name:
            db().execute("UPDATE apps SET name = ? WHERE id = ?", (name, app_id))
        if status is not None:
            db().execute("UPDATE apps SET status = ? WHERE id = ?", (status, app_id))
        if status is not None or name:
            record("app.status", "app", app_id, app_id=app_id, status=status, name=name or None)
        # the owner field is an assignment: the named user becomes an owner
        person = text(data, "owner", 120)
        if person:
            grant(app_id, person, "owner")
    return app_detail(app_id)


def grant(app_id, person, role):
    try:
        previous = auth.assign(db(), app_id, person, role, g.user, registry.now())
    except ValueError as error:
        fail(400, str(error))
    if previous != role:
        record("role.assign", "role", f"{app_id}/{person}", app_id=app_id, user=person, role=role,
               previous=previous)


def owner_or_admin(kwargs):
    return None if g.is_admin else kwargs["app_id"]


def role_list(app_id):
    return {"roles": [{"user_name": r["user_name"], "role": r["role"], "assigned_by": r["granted_by"],
                       "assigned_at": r["granted_at"]} for r in auth.roles_of(db(), app_id)],
            "role_names": list(auth.ROLES)}


@routes.get(V1 + "/apps/{app_id:int}/roles", require_role("viewer"), response_model=m.RoleList)
def list_roles(app_id: int):
    get_app(app_id)
    return role_list(app_id)


@routes.put(V1 + "/apps/{app_id:int}/roles/{user_name}", require_role("owner", auth.ADMIN, resolve=owner_or_admin), response_model=m.RoleList)
def assign_role(app_id: int, user_name: str, body: m.RoleUpdate):
    """Assign a role on the application (its owners, and portal admins)."""
    get_app(app_id)
    role = payload(body).get("role")
    person = user_name.strip()[:120]
    if role not in auth.ROLES or not auth.valid_name(person):
        fail(400, "Give a user name and choose owner, builder or viewer.")
    with db():
        grant(app_id, person, role)
    return role_list(app_id)


@routes.delete(V1 + "/apps/{app_id:int}/roles/{user_name}", require_role("owner", auth.ADMIN, resolve=owner_or_admin), response_model=m.RoleList)
def remove_role(app_id: int, user_name: str):
    get_app(app_id)
    person = user_name.strip()[:120]
    with db():
        try:
            previous = auth.unassign(db(), app_id, person)
        except ValueError as error:
            fail(400, str(error))
        if previous:
            record("role.remove", "role", f"{app_id}/{person}", app_id=app_id, user=person, previous=previous)
    return role_list(app_id)


@routes.get(V1 + "/apps/{app_id:int}/history", require_role("owner", auth.ADMIN, resolve=owner_or_admin), response_model=m.History)
def app_history(app_id: int):
    """The application's audit history (its owners, and portal admins)."""
    item = get_app(app_id)
    return {"app": shapes.ref(item),
            "entries": [{key: row[key] for key in ("id", "at", "actor", "action", "object_type", "object_id",
                                                   "details")} | {"object_id": str(row["object_id"])}
                        for row in audit.history(db(), app_id)]}


def user_list():
    rows = db().execute("SELECT name, password_hash != '' AS has_password, is_admin, created_by, created_at "
                        "FROM users ORDER BY name").fetchall()
    return {"users": [{"name": r["name"], "has_password": bool(r["has_password"]), "is_admin": bool(r["is_admin"]),
                       "created_by": r["created_by"] or "", "created_at": r["created_at"] or ""} for r in rows]}


@routes.get(V1 + "/admin/users", require_role(auth.ADMIN, resolve=None), response_model=m.UserList)
def list_users():
    """The local user store (dev sign-in) and portal admins."""
    return user_list()


@routes.put(V1 + "/admin/users/{user_name}", require_role(auth.ADMIN, resolve=None), response_model=m.UserList)
def save_user(user_name: str, body: m.UserInput):
    data = payload(body)
    name = user_name.strip()[:120]
    password = data.get("password") or ""
    if not isinstance(password, str) or not isinstance(data.get("is_admin"), bool):
        fail(400, "Send is_admin (true or false) and, optionally, a password.")
    make_admin = data["is_admin"]
    try:
        with db():
            auth.save_user(db(), name, password=password or None, is_admin=make_admin, by=g.user,
                           now=registry.now())
            record("user.save", "user", name, user=name, is_admin=make_admin, password_changed=bool(password))
    except ValueError as error:
        fail(400, str(error))
    return user_list()

# -- an application's setup ----------------------------------------------------------------


def app_setup(app_id):
    item = get_app(app_id)
    return {
        "app": {key: (item[key] or "") for key in ("name", "git_url", "required_checks", "target_platform",
                                                   "windows_check_pattern", "lab_url", "launch")}
               | {"id": item["id"], "revision": item["revision"], "url_match": item["url_match"] or "path",
                  "data_backend": item["data_backend"] if item["data_backend"] in demo_target.ENGINES else ""},
        "identities": [{"key": r["key"], "surface": r["surface"] or ""} for r in identities(app_id)],
        "platforms": [shapes.choice(value, label) for value, label in commit_checks.PLATFORMS.items()],
        "github_repo": commit_checks.github_repo(item["git_url"]) or None,
        "github_token_configured": bool(commit_checks.GitHub().token),
        "default_windows_pattern": commit_checks.WINDOWS_PATTERN,
        "releases": release_summaries(app_id),
        "data_backends": [shapes.choice(value, label) for value, label in demo_target.BACKENDS.items()],
        "data_backend": dict(demo_target.backend(db(), item),
                             tables=demo_target.tables(text for _row, text in demo_target.ddl_texts(db(), app_id))),
        "deployment": deployment_of(item),
    }


def deployment_of(item):
    """How the pack deploys the demo: OpenShift files for a web or mainframe demo, else local."""
    if demo_target.kind(db(), item) == "desktop":
        return {"kind": "local", "files": []}
    name = demo_target.resource_name(item)
    return {"kind": "openshift", "files": [f"deploy/ocp/{name}/README.md", f"deploy/ocp/{name}/openshift.yaml"]}


@routes.put(V1 + "/apps/{app_id:int}/data-backend", require_role("owner"), response_model=m.AppSetup)
def set_data_backend(app_id: int, body: m.DataBackendInput):
    get_app(app_id)
    value = payload(body).get("data_backend", "")
    if value not in demo_target.BACKENDS:
        fail(400, "Choose SQLite, PostgreSQL, SQL Server, or predict it from the data model.")
    with db():
        change_app(app_id, payload(body), app_setup)
        db().execute("UPDATE apps SET data_backend = ? WHERE id = ?", (value, app_id))
        record("app.data_backend", "app", app_id, app_id=app_id, data_backend=value or "predicted")
    return app_setup(app_id)


@routes.get(V1 + "/apps/{app_id:int}/setup", require_role("owner"), response_model=m.AppSetup)
def get_app_setup(app_id: int):
    return app_setup(app_id)


@routes.get(V1 + "/setup", auth.signed_in, response_model=m.SetupPage)
def setup():
    rows = visible(db().execute("SELECT id, name FROM apps ORDER BY name").fetchall())
    # where the artifacts are kept (a local path, an S3 bucket and prefix) is for portal admins only
    return {"apps": [shapes.ref(r) for r in rows if auth.can("owner", app_id=r["id"])],
            "storage": artifacts.store().describe() if g.is_admin else None}

# -- restricted evidence: an application's recorded flows, links, attachments, scenarios ----


def selected_events(app_id):
    """The events selected for the app, as `recording#event_id` with their pack references and
    the recording's name (shown to the owner here; a pack names a recording only by id)."""
    linked = buildpack.evidence(db(), app_id)
    names = {r["id"]: r["name"] for r in db().execute(
        "SELECT recordings.id, recordings.name FROM recordings JOIN evidence ON evidence.recording_id = recordings.id "
        "WHERE evidence.app_id = ?", (app_id,))}
    return {scenarios.event_ref(rec["recording_id"], event["event_id"]):
            dict(event, recording=names.get(rec["recording_id"], rec["recording_id"]))
            for rec in linked for event in rec["events"]}


def attachment_view(row):
    return {key: row[key] for key in ("id", "title", "kind", "extension", "share", "added_by")}


@routes.get(V1 + "/apps/{app_id:int}/evidence", require_role("owner"), response_model=m.AppEvidence)
def app_evidence(app_id: int):
    get_app(app_id)
    flows = [{"recording_id": r["recording_id"], "recording_name": r["recording_name"], "key": r["key"],
              "level": r["level"], "machine": r["machine"], "received_at": r["received_at"],
              "steps": [shapes.flow_step(s) for s in r["steps"]]} for r in app_recordings(app_id)]
    return {
        "recorded_flows": flows,
        "links": [{"recording_id": r["id"], "name": r["name"], "selected_count": len(json.loads(r["selection"])),
                   "suggested_count": len(json.loads(r["suggested"])), "reviewed": bool(r["reviewed"]),
                   "notes": r["notes"] or ""} for r in links(app_id)],
        "attachments": [attachment_view(r) for r in attachments(app_id)],
        "automations": [shapes.ref(r) for r in app_automations(app_id)],
        "attachment_kinds": {kind: sorted(extensions) for kind, extensions in ATTACHMENT_TYPES.items()},
        "share_states": [shapes.choice(value, label) for value, label in redact.SHARE_STATES.items()],
        "merge_candidates": [shapes.ref(r) for r in visible(db().execute(
            "SELECT id, name FROM apps WHERE id != ? ORDER BY name", (app_id,)).fetchall())],
    }


def scenario_view(row):
    out = {key: (row.get(key) or "") for key in scenarios.FIELDS}
    out.update(id=row["id"], revision=row["revision"], events=list(row["events"]),
               bindings=list(row.get("bindings") or []))
    return out


def current_scenario(app_id, scenario_id):
    found = next((s for s in scenarios.load(db(), app_id) if s["id"] == scenario_id), None)
    if found is None:
        fail(404, "There is no such scenario.")
    return scenario_view(found)


def scenario_form(data):
    """A ScenarioInput as the form scenarios.from_form reads."""
    events = data.get("events") or []
    if not isinstance(events, list) or not all(isinstance(e, str) for e in events):
        fail(400, "“events” must be a list of event references.")
    pairs = [(key, data[key]) for key in scenarios.FIELDS if isinstance(data.get(key), str)]
    return MultiDict(pairs + [("event", e) for e in events])


def save_scenario(app_id, body, scenario_id=None):
    get_app(app_id)
    data = payload(body)
    want = revisions.expected(data) if scenario_id is not None else None
    try:
        values = scenarios.from_form(scenario_form(data), set(selected_events(app_id)), data.get("bindings"))
    except ValueError as error:
        fail(400, str(error))
    try:
        scenario_id = scenarios.save(db(), app_id, values, scenario_id, expected_revision=want)
    except LookupError:
        fail(404, "There is no such scenario.")
    except scenarios.Stale:
        raise revisions.conflict(current_scenario(app_id, scenario_id), "this scenario") from None
    return current_scenario(app_id, scenario_id)


@routes.get(V1 + "/apps/{app_id:int}/scenarios", require_role("owner"), response_model=m.ScenarioList)
def list_scenarios(app_id: int):
    get_app(app_id)
    return {
        "scenarios": [scenario_view(s) for s in scenarios.load(db(), app_id)],
        "fields": [{"key": key, "label": label, "max_length": limit}
                   for key, (label, limit) in scenarios.FIELDS.items()],
        "event_choices": [{"ref": key, "seq": e["seq"], "action": e["action"], "label": e.get("label"),
                           "screen": e.get("screen"), "recording": e["recording"]}
                          for key, e in selected_events(app_id).items()],
    }


@routes.post(V1 + "/apps/{app_id:int}/scenarios", require_role("owner"), response_model=m.Scenario, status_code=201,
             idempotent="stored")
def create_scenario(app_id: int, body: m.ScenarioInput):
    return save_scenario(app_id, body)


@routes.put(V1 + "/apps/{app_id:int}/scenarios/{scenario_id:int}", require_role("owner"), response_model=m.Scenario)
def update_scenario(app_id: int, scenario_id: int, body: m.ScenarioInput):
    return save_scenario(app_id, body, scenario_id)


@routes.delete(V1 + "/apps/{app_id:int}/scenarios/{scenario_id:int}", require_role("owner"), status_code=204)
def remove_scenario(app_id: int, scenario_id: int):
    get_app(app_id)
    try:
        scenarios.remove(db(), app_id, scenario_id, expected_revision=revisions.expected())
    except scenarios.Stale:
        raise revisions.conflict(current_scenario(app_id, scenario_id), "this scenario") from None
    return Response(status_code=204)


@routes.post(V1 + "/apps/{app_id:int}/attachments", require_role("owner"), response_model=m.Attachment, status_code=201)
def add_attachment(app_id: int, file: UploadFile | None = File(None), title: str = Form(""),
                   kind: str = Form(""), share: str = Form("restricted")):
    """Upload restricted evidence. Uploading never shares it: the owner marks it reviewed or
    synthetic (here or later) before it can enter a builder package."""
    get_app(app_id)
    upload = file
    title = title.strip()[:200]
    share = share or "restricted"
    if not upload or not title:
        fail(400, "Choose a file and give it a title.")
    if share not in redact.SHARE_STATES:
        fail(400, "Choose restricted, reviewed or synthetic.")
    extension = Path(upload.filename or "").suffix.lower()
    if extension not in ATTACHMENT_TYPES.get(kind, set()):
        fail(400, "The file extension is unsupported for this evidence type.")
    content = upload.file.read()
    if not content or len(content) > 4 * 1024 * 1024:
        fail(400, "Evidence files must be between 1 byte and 4 MiB.")
    ref = artifacts.put(content, kind="restricted_evidence", app_id=app_id,
                        content_type=artifacts.content_type_for(extension))
    with db():
        ref = artifacts.save(db(), ref)
        attachment_id = db().execute(
            "INSERT INTO attachments(app_id,title,kind,extension,content,artifact_id,share,added_by) "
            "VALUES (?,?,?,?,?,?,?,?) RETURNING id",
            (app_id, title, kind, extension, b"", ref.id, share, g.user),
        ).fetchone()[0]
        readiness_cache.forget(db(), app_id)
        record("evidence.upload", "attachment", attachment_id, app_id=app_id, title=title, kind=kind,
               sha256=hashlib.sha256(content).hexdigest(), share=share)
    row = db().execute("SELECT * FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
    return attachment_view(row)


@routes.patch(V1 + "/apps/{app_id:int}/attachments/{attachment_id:int}", require_role("owner"), response_model=m.Attachment)
def share_attachment(app_id: int, attachment_id: int, body: m.ShareInput):
    """The owner's review of an attachment: restricted, reviewed or synthetic."""
    share = payload(body).get("share")
    if share not in redact.SHARE_STATES:
        fail(400, "Choose restricted, reviewed or synthetic.")
    with db():
        row = db().execute("SELECT share FROM attachments WHERE app_id = ? AND id = ?",
                           (app_id, attachment_id)).fetchone()
        if row is None:
            fail(404, "There is no such attachment.")
        if row["share"] != share:
            db().execute("UPDATE attachments SET share = ? WHERE app_id = ? AND id = ?",
                         (share, app_id, attachment_id))
            readiness_cache.forget(db(), app_id)
            record("evidence.share", "attachment", attachment_id, app_id=app_id, share=share,
                   previous=row["share"])
    return attachment_view(db().execute("SELECT * FROM attachments WHERE id = ?", (attachment_id,)).fetchone())


@routes.delete(V1 + "/apps/{app_id:int}/attachments/{attachment_id:int}", require_role("owner"), status_code=204)
def remove_attachment(app_id: int, attachment_id: int):
    get_app(app_id)
    from twinlab2 import lifecycle

    with db():
        row = db().execute("SELECT title, artifact_id FROM attachments WHERE app_id = ? AND id = ?",
                           (app_id, attachment_id)).fetchone()
        removed = db().execute("DELETE FROM attachments WHERE app_id = ? AND id = ?",
                               (app_id, attachment_id)).rowcount
        if removed:
            readiness_cache.forget(db(), app_id)
            record("evidence.remove", "attachment", attachment_id, app_id=app_id)
            # the stored bytes go too, unless another attachment holds the same object
            if row["artifact_id"] is not None and db().execute(
                    "SELECT 1 FROM attachments WHERE artifact_id = ?", (row["artifact_id"],)).fetchone() is None:
                ref = artifacts.load(db(), row["artifact_id"], include_deleted=True)
                stone = lifecycle.tombstone(db(), actor=g.user, reason="owner_request", object_type="attachment",
                                            object_id=attachment_id, app_id=app_id, sha256=ref.sha256, size=ref.size,
                                            title=row["title"])
                lifecycle.delete_artifact(db(), row["artifact_id"], stone, "owner_request")
    return Response(status_code=204)

# -- recordings ----------------------------------------------------------------------------


@routes.post(V1 + "/recordings", auth.signed_in, response_model=m.RecordingRef, status_code=201)
def import_recording(name: str = Form(""), log: UploadFile | None = File(None)):
    name = name.strip()[:200]
    if not name or log is None:
        fail(400, "Enter a recording name and select steps.jsonl.")
    try:
        digest, events = recording_projection(log.file.read())
    except UnicodeDecodeError:
        fail(400, "The event log is not UTF-8 JSON lines.")
    except ValueError as error:
        fail(400, str(error))
    with db():
        inserted = db().execute(
            "INSERT INTO recordings(id, name, events) VALUES (?, ?, ?) ON CONFLICT DO NOTHING RETURNING id",
            (digest, name, json.dumps(events)),
        ).fetchone()
        if inserted is None:
            # a recovered upload of the same session: events are the same by event_id
            old = {event["event_id"]: event for event in json.loads(get_recording(digest)["events"])}
            for event in events:
                existing = old.setdefault(event["event_id"], event)
                if any(existing[key] != event[key] for key in ("action", "source")) or (
                    existing.get("time") and event.get("time") and existing["time"] != event["time"]
                ):
                    fail(409, "A recovered event conflicts with the existing catalog event.")
                existing.update(event)
            db().execute("UPDATE recordings SET events = ? WHERE id = ?", (
                json.dumps(ordered(old.values())), digest,
            ))
        db().execute("INSERT INTO recording_uploads(recording_id, added_by, added_at) VALUES (?, ?, ?) "
                     "ON CONFLICT DO NOTHING", (digest, g.user, registry.now()))
        readiness_cache.forget_recording(db(), digest)
        record("recording.import", "recording", digest, name=name, events=len(events))
    return {"id": digest, "name": get_recording(digest)["name"]}


def recorded_app(row):
    """One application a recording holds, with its own steps and, for its builders, its own
    latest builder-approved package."""
    latest = packages.latest(db(), row["app_id"]) if auth.can("builder", app_id=row["app_id"]) else None
    return {"app_id": row["app_id"], "name": row["name"], "status": row["status"],
            "status_label": shapes.status_label(row["status"]), "level": row["level"], "key": row["key"],
            "steps": [shapes.flow_step(s) for s in row["steps"]],
            "my_role": auth.role_of(db(), row["app_id"], g.user),
            "latest_package": shapes.package_summary(latest) if latest else None}


def refs_view(recording_id):
    refs = recording_refs(recording_id)
    if refs is None:
        return None
    session_ = refs["session"]
    contract_row = refs["contract"]
    return {
        "machine": refs["ref"]["machine"], "automation": refs["ref"]["automation"],
        "session": {"id": str(session_.get("id") or ""), "started": session_.get("started"),
                    "ended": session_.get("ended"), "state": session_.get("state")},
        "contract": {"recorder": contract_row["recorder"], "recorder_version": contract_row["recorder_version"],
                     "contract_version": contract_row["contract_version"],
                     "twinlab_commit": contract_row["twinlab_commit"]} if contract_row else None,
        "timeline": [{"app": run["app"], "name": run["name"], "steps": run["steps"]} for run in refs["timeline"]],
        "ranges": [{"label": lane["label"], "status": lane["status"], "gaps": lane["gaps"]} for lane in refs["ranges"]],
        "apps": [recorded_app(row) for row in refs["apps"]],
    }


def recording_detail(recording_id):
    item = get_recording(recording_id)
    events = json.loads(item["events"])
    status = registry.recording_status(db(), recording_id)
    data = status["completeness"]
    by_id = {e["event_id"]: e for e in events}
    preview = [dict(p, events=[shapes.event(dict(by_id.get(e["event_id"], {}), **e), data, completeness.in_gap)
                               for e in p["events"]]) for p in buildpack.preview(db(), recording_id)]
    upload = upload_progress(recording_id)
    return {
        "id": item["id"], "name": item["name"], "status": shapes.recording_status(status),
        "upload": {key: upload[key] for key in ("text", "percent", "complete")} if upload else None,
        "preview": preview, "refs": refs_view(recording_id),
        "events": [shapes.event(e, data, completeness.in_gap) for e in events[:DETAIL_EVENTS]],
        "events_total": len(events),
        "assignable_apps": [shapes.ref(row) for row in db().execute("SELECT id, name FROM apps ORDER BY name")
                            .fetchall() if auth.can("owner", app_id=row["id"])],
    }


DETAIL_EVENTS = 200  # the rest: GET .../events


@routes.get(V1 + "/recordings/{recording_id}", auth.signed_in, response_model=m.RecordingDetail)
def recording(recording_id: str):
    """Restricted evidence: its uploader and the owners of linked applications only."""
    get_recording(recording_id)
    ensure_reviewer(recording_id)
    return recording_detail(recording_id)


@routes.get(V1 + "/recordings/{recording_id}/packages/download", auth.signed_in)
def download_recording_packages(recording_id: str):
    """One ZIP with the latest builder-approved package of every application of the recording
    the caller builds (recording_export), for a reviewer of the recording. Each included
    package is audited as a download, as the single-package download is."""
    get_recording(recording_id)
    ensure_reviewer(recording_id)
    try:
        result = recording_export.export(db(), recording_id, g.user)
    except LookupError:
        fail(404, "There is no such recording.")
    except ValueError as error:
        fail(500, str(error))
    with db():
        for app_id, rev in result.included:
            row = packages.get(db(), app_id, rev)
            record("package.download", "builder_package", f"{app_id}/rev{rev}", app_id=app_id, rev=rev,
                   sha256=row["sha256"], recording_id=recording_id, export_sha256=result.sha256)
    return download(result.data, "application/zip", result.filename)


EVENT_TEXT = ("action", "key", "control", "label", "screen", "source", "app", "time")


@routes.get(V1 + "/recordings/{recording_id}/events", auth.signed_in, response_model=m.RecordingEvents)
def recording_events(recording_id: str):
    """A reviewer of the recording: its events, a page at a time, searchable."""
    item = get_recording(recording_id)
    ensure_reviewer(recording_id)
    query = g.request.query_params
    limit, offset = page_args(default=100, maximum=500)
    events = json.loads(item["events"])
    # a recorder's events carry their control, label and screen in each application's flow
    steps = {}
    for row in db().execute("SELECT recording_apps.flow, apps.name FROM recording_apps JOIN apps ON apps.id = "
                            "recording_apps.app_id WHERE recording_apps.recording_id = ? ORDER BY apps.name, "
                            "recording_apps.key", (recording_id,)):
        for step in json.loads(row["flow"]):
            steps.setdefault(step.get("event_id"), dict(step, app=row["name"]))
    events = [dict({k: steps[e["event_id"]].get(k) for k in ("control", "label", "screen", "app")
                    if steps.get(e["event_id"], {}).get(k) is not None}, **e) for e in events]
    sources = {}
    for e in events:
        source = e.get("source") or "Unknown source"
        sources[source] = sources.get(source, 0) + 1
    if query.get("source"):
        events = [e for e in events if (e.get("source") or "Unknown source") == query["source"]]
    # the screens of the chosen source (or all), in the order the recording first reached them
    screens = {}
    for e in events:
        screen = e.get("screen") or ""
        screens[screen] = screens.get(screen, 0) + 1
    if "screen" in query:
        events = [e for e in events if (e.get("screen") or "") == query["screen"]]
    words = (query.get("q") or "").casefold().split()
    if words:
        events = [e for e in events
                  if all(w in " ".join(str(e.get(k) or "") for k in EVENT_TEXT).casefold() for w in words)]
    data = registry.recording_status(db(), recording_id)["completeness"]
    page, where = paged(events, limit, offset)
    out = {"events": [dict(shapes.event(e, data, completeness.in_gap), **({"app": e["app"]} if e.get("app") else {}))
                      for e in page], "page": where,
           "sources": [{"source": k, "count": v} for k, v in sources.items()],
           "screens": [{"screen": k or None, "count": v} for k, v in screens.items()]}
    if query.get("ids", "").lower() in ("1", "true"):
        out["ids"] = [e["event_id"] for e in events]
    return out


def assignment(recording_id, app_id):
    saved = db().execute("SELECT * FROM evidence WHERE app_id = ? AND recording_id = ?",
                         (app_id, recording_id)).fetchone()
    removal = None if saved else db().execute(
        "SELECT revision FROM evidence_removals WHERE app_id = ? AND recording_id = ?", (app_id, recording_id)).fetchone()
    return {"app_id": app_id, "recording_id": recording_id, "saved": saved is not None,
            "revision": saved["revision"] if saved else removal["revision"] if removal else 0,
            "removed": removal is not None,
            "reviewed": bool(saved and saved["reviewed"]),
            "selected": json.loads(saved["selection"]) if saved else [],
            "suggested": json.loads(saved["suggested"]) if saved else [],
            "notes": (saved["notes"] or "") if saved else ""}


def assignment_revision(recording_id, app_id, want):
    """The assignment's current revision; 409 `conflict` when `want` names another one."""
    current = assignment(recording_id, app_id)
    if want is not None and want != current["revision"]:
        raise revisions.conflict(current, "this assignment")
    return current


@routes.get(V1 + "/recordings/{recording_id}/assignments/{app_id:int}", require_role("owner"), response_model=m.Assignment)
def get_assignment(recording_id: str, app_id: int):
    get_app(app_id)
    get_recording(recording_id)
    ensure_reviewer(recording_id)
    return assignment(recording_id, app_id)


@routes.put(V1 + "/recordings/{recording_id}/assignments/{app_id:int}", require_role("owner"), response_model=m.Assignment)
def save_assignment(recording_id: str, app_id: int, body: m.AssignmentInput):
    get_app(app_id)
    item = get_recording(recording_id)
    ensure_reviewer(recording_id)
    data = payload(body)
    values = data.get("events")
    if not isinstance(values, list):
        fail(400, "Choose one or more events from this recording.")
    # events are chosen by event_id: their seq is a display order and may change
    selected = {event_ids.clean(value) if isinstance(value, str) else None for value in values}
    if None in selected:
        fail(400, "Invalid event.")
    if not selected or not selected <= {event["event_id"] for event in json.loads(item["events"])}:
        fail(400, "Choose one or more events from this recording, or remove the assignment.")
    notes = text(data, "notes")
    want = revisions.expected(data)
    with db():
        current = assignment_revision(recording_id, app_id, want)
        value = (json.dumps(sorted(selected)), notes)
        if current["saved"]:
            # the owner's review: a resend never changes this selection (registry.link_evidence);
            # the revision in the WHERE makes the check and the write one step
            changed = db().execute("""
                UPDATE evidence SET selection = ?, notes = ?, reviewed = 1, suggested = '[]', revision = revision + 1
                WHERE app_id = ? AND recording_id = ? AND revision = ?
            """, (*value, app_id, recording_id, current["revision"])).rowcount
            if changed != 1:
                raise revisions.conflict(assignment(recording_id, app_id), "this assignment")
        else:
            db().execute("DELETE FROM evidence_removals WHERE app_id = ? AND recording_id = ?", (app_id, recording_id))
            db().execute("""
                INSERT INTO evidence(app_id, recording_id, selection, notes, reviewed, suggested, revision)
                VALUES (?, ?, ?, ?, 1, '[]', ?)
            """, (app_id, recording_id, *value, current["revision"] + 1))
        readiness_cache.forget(db(), app_id)
        record("evidence.review", "recording", recording_id, app_id=app_id,
               selected=sorted(selected), notes_changed=bool(notes))
    return assignment(recording_id, app_id)


@routes.delete(V1 + "/recordings/{recording_id}/assignments/{app_id:int}", require_role("owner"), response_model=m.Assignment)
def remove_assignment(recording_id: str, app_id: int):
    """The owner's explicit removal: the application no longer uses this recording's events or
    notes. Its next draft package leaves the recording out; a resend does not select them again
    (registry.link_evidence); frozen packages are unchanged."""
    get_app(app_id)
    get_recording(recording_id)
    ensure_reviewer(recording_id)
    with db():
        current = assignment_revision(recording_id, app_id, revisions.expected())
        if current["saved"]:
            gone = db().execute("DELETE FROM evidence WHERE app_id = ? AND recording_id = ? AND revision = ?",
                                (app_id, recording_id, current["revision"])).rowcount
            if gone != 1:
                raise revisions.conflict(assignment(recording_id, app_id), "this assignment")
        db().execute("DELETE FROM evidence_removals WHERE app_id = ? AND recording_id = ?", (app_id, recording_id))
        db().execute("INSERT INTO evidence_removals(app_id, recording_id, revision, removed_by, removed_at) "
                     "VALUES (?, ?, ?, ?, ?)", (app_id, recording_id, current["revision"] + 1, g.user, registry.now()))
        readiness_cache.forget(db(), app_id)
        record("evidence.remove", "recording", recording_id, app_id=app_id, removed=current["selected"],
               suggested=current["suggested"], notes_removed=bool(current["notes"]))
    return assignment(recording_id, app_id)


def upload_progress(recording_id):
    """The upload's progress (what GET /api/recordings/<id>/upload answers), or None when there
    is none to show."""
    try:
        found = upload_view(uploads.recording_upload(recording_id))
    except Problem:
        return None
    return found


# -- builder packages ----------------------------------------------------------------------


def file_list(files, text=True):
    return [{"path": path, "size": len(content), "sha256": hashlib.sha256(content).hexdigest(),
             **({"text": content.decode("utf-8") if buildpack.is_text(path) else None} if text else {})}
            for path, content in files.items()]


def current_draft(app_id, override_reason=""):
    get_app(app_id)
    if not scenarios.load(db(), app_id):
        fail(400, "Add a business scenario before freezing a builder package.")
    return buildpack.draft(db(), app_id, VERSION_PATH, known_gaps=packages.override_reason(override_reason) or None)


def frozen(app_id, rev):
    row = packages.get(db(), app_id, rev)
    if row is None:
        fail(404, "There is no such package revision.")
    return row


def report_entries(report):
    return [{"item": entry["item"], "outcome": entry["outcome"], "reasons": list(entry.get("reasons") or []),
             **{k: entry[k] for k in ("kind", "path") if k in entry}} for entry in report]


def package_view(app_id, rev):
    row = frozen(app_id, rev)
    files = buildpack.read(packages.open_package(db(), row["storage_ref"]))
    validation, known = packages.completeness_of(row)
    return dict(shapes.package_summary(row), app=shapes.ref(get_app(app_id)), summary=row["summary"] or "",
                diff=row["diff"] or "", files=file_list(files, text=False), completeness=validation,
                known_gaps=known,
                report=report_entries(json.loads(row["report"])) if auth.can("owner", app_id=app_id) else None)


def send_package(app_id, row):
    """A frozen revision: a short-lived presigned URL when the store is S3 (its object carries
    the approved SHA-256), else the bytes, checked against the approved SHA-256."""
    item = get_app(app_id)
    stored = packages.stored_row(db(), row["storage_ref"])
    ref = artifacts.ref_of(db(), stored)
    if ref is not None and ref.sha256 != row["sha256"] or ref is None and \
            hashlib.sha256(packages.open_package(db(), row["storage_ref"])).hexdigest() != row["sha256"]:
        fail(500, "The stored package does not match its approved SHA-256.")
    with db():
        record("package.download", "builder_package", f"{app_id}/rev{row['rev']}", app_id=app_id,
               rev=row["rev"], sha256=row["sha256"])
    return send_stored(stored, "archive", packages.download_name(item, row))


@routes.get(V1 + "/apps/{app_id:int}/packages", require_role("builder"), response_model=m.PackageList)
def list_packages(app_id: int):
    return {"app": shapes.ref(get_app(app_id)),
            "packages": [shapes.package_summary(r) for r in packages.revisions(db(), app_id)]}


@routes.get(V1 + "/apps/{app_id:int}/build-pack", require_role("builder"))
def build_pack(app_id: int):
    """The latest builder-approved package (a frozen revision)."""
    get_app(app_id)
    row = packages.latest(db(), app_id)
    if row is None:
        fail(400, "No builder package is frozen yet: the owner previews and approves one first.")
    return send_package(app_id, row)


@routes.get(V1 + "/apps/{app_id:int}/package-draft", require_role("owner"), response_model=m.PackageDraft)
def preview_pack(app_id: int, override_reason: str = ""):
    """The draft package exactly as freezing would store it: every file, its size and SHA-256,
    text files in full, the pre-freeze report (what was excluded or redacted, and why) and its
    completeness. With `override_reason` (and critical gaps), the draft that carries the reason
    as "Known gaps": the one a freeze with that reason stores."""
    d = current_draft(app_id, override_reason[:2000])
    latest = packages.latest(db(), app_id)
    return {"app": shapes.ref(get_app(app_id)), "revision": d.revision, "sha256": d.sha256,
            "size": len(d.package), "files": file_list(d.files), "report": report_entries(d.report),
            "summary": d.summary(), "latest": shapes.package_summary(latest) if latest else None,
            "unchanged": latest is not None and latest["sha256"] == d.sha256,
            "next_rev": (latest["rev"] if latest else 0) + 1, "completeness": d.validation,
            "known_gaps": json.loads(d.manifest).get("known_gaps")}


@routes.post(V1 + "/apps/{app_id:int}/packages", require_role("owner"), response_model=m.PackageRevision, status_code=201,
             idempotent="stored")
def freeze_package(app_id: int, body: m.FreezeInput):
    """The owner's explicit approval: freeze the previewed draft as the next revision."""
    get_app(app_id)
    data = payload(body)
    if data.get("approve") is not True:
        fail(400, "Confirm that you approve this package for the builder.")
    if not scenarios.load(db(), app_id):
        fail(400, "Add a business scenario before freezing a builder package.")
    sha = data.get("sha256") if isinstance(data.get("sha256"), str) else ""
    try:
        with db():
            rev = packages.freeze(db(), app_id, g.user, sha, text(data, "summary", 2000), VERSION_PATH,
                                  override=text(data, "override_reason", 2000))
    except packages.Unchanged as error:
        fail(409, str(error), "unchanged")
    except packages.Incomplete as error:
        fail(409, str(error), "incomplete_package",
             errors=[{"field": gap["item"], "message": " ".join([gap["title"] + ".", *gap["details"][:3]])}
                     for gap in pack_validator.critical(error.validation)])
    except ValueError as error:
        fail(409, str(error), "draft_changed")
    return package_view(app_id, rev)


@routes.get(V1 + "/apps/{app_id:int}/packages/{rev:int}", require_role("builder"), response_model=m.PackageRevision)
def package(app_id: int, rev: int):
    return package_view(app_id, rev)


@routes.get(V1 + "/apps/{app_id:int}/packages/{rev:int}/download", require_role("builder"))
def download_package(app_id: int, rev: int):
    return send_package(app_id, frozen(app_id, rev))


@routes.get(V1 + "/apps/{app_id:int}/briefs/{revision}/download", require_role("builder"))
def download_brief(app_id: int, revision: str):
    """A package by its content revision: a frozen one for builders; a brief stored before
    packages were frozen only for owners."""
    row = packages.by_revision(db(), app_id, revision)
    if row is not None:
        return send_package(app_id, row)
    auth.ensure("owner", app_id=app_id)
    item = get_app(app_id)
    slug = re.sub(r"[^a-z0-9]+", "-", item["name"].lower()).strip("-") or "demo"
    return send_stored(pack_row(app_id, revision), "archive", f"{slug}-{revision[:12]}.zip")


def pack_row(app_id, revision):
    row = db().execute("""
        SELECT brief_packages.archive, brief_packages.artifact_id FROM brief_packages
        JOIN briefs USING (revision) WHERE app_id = ? AND revision = ?
    """, (app_id, revision)).fetchone()
    if row is None:
        fail(404, "There is no such package.")
    return row


def send_stored(row, column, filename):
    """A download of a stored artifact: a short-lived presigned URL when the store is S3,
    else the bytes through the portal, checked against the ref's SHA-256."""
    ref = artifacts.ref_of(db(), row)
    if ref is not None and ref.backend == "s3":
        return RedirectResponse(artifacts.presign_get(ref, 300, filename), status_code=302)
    if ref is not None:  # streamed in chunks, checked as they pass (artifacts.stream)
        return StreamingResponse(artifacts.stream(ref), media_type="application/zip", headers={
            "Content-Disposition": disposition(filename), "Cache-Control": "no-store", "Content-Length": str(ref.size)})
    return download(bytes(row[column]), "application/zip", filename)  # an old blob, already in the row


def pack_view(app_id):
    """What a builder builds: the latest builder-approved (frozen) package, every file with its
    size and SHA-256, text in full. 400 before an owner has approved one."""
    row = packages.latest(db(), app_id)
    if row is None:
        fail(400, "No builder-approved package yet: an owner previews the package, checks its "
                      "pre-freeze report and approves it.")
    package = packages.open_package(db(), row["storage_ref"])
    return dict(shapes.package_summary(row), size=len(package), files=file_list(buildpack.read(package)))

# -- releases ------------------------------------------------------------------------------


def get_release(release_id):
    item = db().execute(
        "SELECT id, app_id, version, revision, commit_sha, archive_sha, state, published_at, repository, "
        "pr_number, source_ref FROM releases WHERE id = ?",
        (release_id,),
    ).fetchone()
    if item is None:
        fail(404, "There is no such release.")
    return item


def save_check(release, environment, url):
    status, observed = verification(url, release["commit_sha"])
    db().execute(
        "UPDATE deployments SET verification = ?, observed_commit = ?, checked_at = ? WHERE release_id = ? AND environment = ?",
        (status, observed, registry.now(), release["id"], environment),
    )


def release_app(kwargs):
    return get_release(kwargs["release_id"])["app_id"]


def verification_reports(release_id, reports):
    """The release's verification reports, latest first, each with its outcome and who asked for it
    (the verification job that wrote it; a report stored before jobs existed names no one)."""
    jobs = {row["report_id"]: row for row in db().execute(
        "SELECT report_id, tested, requested_by FROM verifications WHERE release_id = ? AND report_id IS NOT NULL",
        (release_id,))}
    out = []
    for n, r in enumerate(sorted(reports, key=lambda r: r["id"], reverse=True)):
        job = jobs.get(r["id"])
        tested = None if job is None or job["tested"] is None else bool(job["tested"])
        if tested is None:
            content = commit_checks.get_report(db(), release_id, r["id"])
            try:
                tested = bool(json.loads(content).get("tested")) if content else None
            except ValueError:
                tested = None
        out.append({"id": r["id"], "created_at": r["created_at"], "sha256": r["sha256"], "latest": n == 0,
                    "number": len(reports) - n, "tested": tested,
                    "requested_by": job["requested_by"] if job is not None else None})
    return out


def check_summary(release_id):
    gate = commit_checks.summary(db(), release_id)
    return {
        "repository": gate.get("repository") or None, "pr_number": gate.get("pr_number"),
        "source_ref": gate.get("source_ref") or None, "platform": gate.get("platform") or "",
        "required": [{"pattern": r["pattern"], "state": r["state"], "checks": r.get("checks", [])}
                     for r in gate["required"]],
        "windows": gate["windows"], "checked_at": gate.get("checked_at"),
        "checks": [{"name": c["name"], "kind": c["kind"], "conclusion": c["conclusion"], "url": c.get("url") or None,
                    "completed_at": c.get("completed_at")} for c in gate.get("checks", [])],
        "reasons": list(gate["reasons"]),
        "reports": verification_reports(release_id, [r for r in gate.get("reports", [])
                                                     if r.get("kind", "verification_report") == "verification_report"]),
        "ok": gate["ok"],
    }


def test_scope(test):
    """The stored scope of a release's evidence; evidence stored before scopes existed gets one
    from its own columns (the role it was recorded with is not known)."""
    scope = json.loads(test["scope"] or "{}") if "scope" in test.keys() else {}
    if scope:
        return scope
    return portal.verified_scope(test["commit_sha"], test["platform"], test["tested_by"], None, "manual",
                                 json.loads(test["results"]), None, test["tested_at"])


def release_detail(release_id, notice=None):
    item = get_release(release_id)
    app_id = item["app_id"]
    deployments = db().execute(
        "SELECT * FROM deployments WHERE release_id = ? ORDER BY environment", (release_id,),
    ).fetchall()
    test = db().execute("SELECT * FROM release_tests WHERE release_id = ?", (release_id,)).fetchone()
    brief = db().execute("SELECT manifest FROM briefs WHERE revision = ?", (item["revision"],)).fetchone()
    return {
        "release": dict(shapes.release_summary(item, registry.release_labels(db(), app_id)[item["id"]]), published_at=item["published_at"],
                        repository=item["repository"] or None, pr_number=item["pr_number"],
                        source_ref=item["source_ref"] or None),
        "app": shapes.ref(get_app(app_id)), "my_role": auth.role_of(db(), app_id, g.user),
        "can_download": item["state"] == "published" or auth.can("builder", app_id=app_id),
        "deployments": [shapes.deployment(d, deployment_label, deployment_reason) for d in deployments],
        "test": {"commit_sha": test["commit_sha"], "platform": test["platform"], "launch": test["launch"],
                 "reset_result": test["reset_result"], "reset_notes": test["reset_notes"] or "",
                 "tested_by": test["tested_by"], "tested_at": test["tested_at"],
                 "results": json.loads(test["results"]), "scope": test_scope(test)} if test else None,
        "scenarios": brief_scenarios(json.loads(brief["manifest"])) if brief else [],
        "results": list(RESULTS), "checks": check_summary(release_id), "version_path": VERSION_PATH,
        "verification": latest_verification(release_id),
        "notice": notice,
    }


def latest_verification(release_id):
    row = verifications.latest_for_release(db(), release_id)
    return verification_view(row) if row is not None else None


@routes.get(V1 + "/releases/{release_id:int}", require_role("viewer", resolve=release_app), response_model=m.ReleaseDetail)
def release(release_id: int):
    return release_detail(release_id)


@routes.put(V1 + "/releases/{release_id:int}/deployments", require_role("owner", resolve=release_app), response_model=m.ReleaseDetail)
def save_deployment(release_id: int, body: m.DeploymentInput):
    item = get_release(release_id)
    data = payload(body)
    environment = text(data, "environment", 100)
    url = text(data, "url", 2000)
    try:
        parsed = urlsplit(url)
        hostname, username = parsed.hostname, parsed.username
    except ValueError:
        fail(400, "Enter a valid deployment URL.")
    if not environment or parsed.scheme not in {"http", "https"} or not hostname or username is not None:
        fail(400, "Provide an environment and an HTTP or HTTPS URL without credentials.")
    secret_ref = text(data, "secret_ref", 256)
    if secret_ref and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/#@-]{0,255}", secret_ref):
        fail(400, "Enter a secret reference such as vault:demo/billing.")
    with db():
        db().execute("""
            INSERT INTO deployments(release_id, environment, url, username, secret_ref) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(release_id, environment) DO UPDATE SET
            url = excluded.url, username = excluded.username, secret_ref = excluded.secret_ref
        """, (release_id, environment, url, text(data, "username", 200), secret_ref))
        save_check(item, environment, url)
        record("deployment.register", "release", release_id, app_id=item["app_id"],
               environment=environment, url=url)
    return release_detail(release_id)


@routes.post(V1 + "/releases/{release_id:int}/deployments/check", require_role("builder", resolve=release_app), response_model=m.ReleaseDetail)
def check_deployment(release_id: int, body: m.DeploymentCheck):
    item = get_release(release_id)
    environment = text(payload(body), "environment", 100)
    row = db().execute("SELECT url FROM deployments WHERE release_id = ? AND environment = ?",
                       (release_id, environment)).fetchone()
    if row is None:
        fail(404, "This release has no deployment in that environment.")
    with db():
        save_check(item, environment, row["url"])
    return release_detail(release_id)


@routes.post(V1 + "/releases/{release_id:int}/tests", require_role("owner", resolve=release_app), response_model=m.ReleaseDetail)
def test_release(release_id: int, body: m.TestInput):
    item = get_release(release_id)
    data = payload(body)
    results = data.get("results")
    if not isinstance(results, dict) or not all(isinstance(v, str) for v in results.values()):
        fail(400, "Send a result (pass, fail or not-run) per scenario.")
    with db():  # the fetched checks are kept even when the test is refused
        gate = commit_checks.refresh(db(), release_id, github())
    try:
        with db():
            record_test(db(), release_id, text(data, "commit"), text(data, "platform"),
                        text(data, "launch"), data.get("reset_result"),
                        text(data, "reset_notes"), results, g.user, gate=gate)
            record("release.test", "release", release_id, app_id=item["app_id"], version=item["version"],
                   commit=item["commit_sha"], results=results, reset=data.get("reset_result"))
    except ValueError as error:
        fail(400, str(error))
    return release_detail(release_id, notice="Recorded: the release is Tested.")


def github():
    return config().get("GITHUB") or commit_checks.GitHub()


@routes.post(V1 + "/releases/{release_id:int}/checks/refresh", require_role("owner", resolve=release_app), response_model=m.ReleaseDetail)
def refresh_checks(release_id: int):
    get_release(release_id)
    with db():
        commit_checks.refresh(db(), release_id, github())
    return release_detail(release_id)


def report_link(release_id, report_id):
    return f"{V1}/releases/{release_id}/reports/{report_id}"


def verification_view(row):
    return verifications.view(row, report_link)


def verification_app(kwargs):
    row = verifications.get(db(), kwargs["verification_id"])
    if row is None:
        fail(404, "There is no such verification.")
    return row["app_id"]


def targets_of(data):
    """The request's verify targets: a mainframe host (host:port) or a desktop executable
    (relative to the Windows verifier's builds folder)."""
    raw = data.get("targets") if isinstance(data.get("targets"), dict) else {}
    out = {}
    if raw.get("mainframe"):
        value = str(raw["mainframe"]).strip()[:300]
        if not re.fullmatch(r"(tn3270s?://)?[A-Za-z0-9.\-]{1,253}:\d{1,5}/?", value):
            fail(400, "Name the mainframe target as host:port (tn3270://host:port).")
        out["mainframe"] = value
    if raw.get("desktop"):
        value = str(raw["desktop"]).strip()[:300]
        if ".." in value.replace("\\", "/").split("/") or re.match(r"^([A-Za-z]:|[\\/])", value):
            fail(400, "Name the desktop executable relative to the verifier's builds folder.")
        out["desktop"] = value
    return out


def queue_verification(kind, app_id, params, request_key, release_id=None, commit=None):
    """The verification of a request (a new one queued, or the one the request repeats). The
    demo portal and tests inject a fake Twin Lab runner (TWINLAB_RUNNER): their verifications
    are worked in a thread of this process."""
    row, created = verifications.request(db(), kind, app_id, params, g.user, request_key,
                                         release_id=release_id, commit=commit)
    if created:
        record("release.verify", "release", release_id, app_id=app_id, commit=commit, verification=row["id"],
               kind=kind)
        db().commit()
        runner = config().get("TWINLAB_RUNNER")
        if runner is not None or config().get("VERIFY_IN_PROCESS"):
            verifications.run_in_process(config()["DATABASE"], verify_context(runner))
    return row


def verify_context(runner=None):
    from twinlab2 import builds

    ctx = builds.Context()
    ctx.verify_github, ctx.verify_runner = github(), runner
    return ctx


@routes.post(V1 + "/releases/{release_id:int}/verify", require_role("owner", resolve=release_app),
             response_model=m.Verification, status_code=202, idempotent="native")
def verify_this_release(release_id: int, body: m.VerifyInput | None = None):
    """Queue the verification of a release; 202 with the verification to follow
    (GET /api/v1/verifications/{id}). A repeated request gets the same verification."""
    item = get_release(release_id)
    data = payload(body) if body is not None else {}
    if item["state"] == "published":
        fail(400, "A published release is final: package a new version to test again.")
    row = queue_verification("release", item["app_id"], {"targets": targets_of(data)},
                             idempotency.key_of(data.get("request_key")), release_id=release_id,
                             commit=item["commit_sha"])
    return verification_view(verifications.get(db(), row["id"]))


@routes.get(V1 + "/verifications/{verification_id:int}", require_role("viewer", resolve=verification_app),
            response_model=m.Verification)
def get_verification(verification_id: int):
    row = verifications.get(db(), verification_id)
    if row is None:
        fail(404, "There is no such verification.")
    return verification_view(row)


def session_of(app_id, build_id):
    """{build_id, revision} of the build session a verification is for (none: {})."""
    if not build_id:
        return {}
    build = builds.get(db(), build_id)
    if build is None or build["app_id"] != app_id:
        fail(400, "That build session is not one of this application's.")
    return {"build_id": build_id, "revision": build["revision"]}


def verify_commit_request(app_id, repo, sha, pr_number=None, ref="", build_id=None, revision=None, targets=None,
                          request_key=None):
    """Check a "Verify this build" request now (400 for what is missing) and queue it: the
    worker packages the commit and verifies it."""
    try:
        repo, sha, revision = portal.check_commit_request(db(), app_id, repo, sha, revision)
    except ValueError as error:
        fail(400, str(error))
    existing = db().execute("SELECT id FROM releases WHERE app_id = ? AND commit_sha = ? ORDER BY id DESC",
                            (app_id, sha)).fetchone()
    params = {"repo": repo, "sha": sha, "pr_number": pr_number, "ref": ref or "", "build_id": build_id,
              "revision": revision, "targets": targets or {}}
    return queue_verification("commit", app_id, params, request_key, commit=sha,
                              release_id=existing["id"] if existing is not None and build_id is None else None)


def verify_session(build, request_key=None):
    return verify_commit_request(build["app_id"], build["repo"], build["head_sha"], build["pr_number"],
                                 build["branch"] or "", build["id"], build["revision"], request_key=request_key)


@routes.post(V1 + "/apps/{app_id:int}/verify-commit", require_role("owner"), response_model=m.Verification,
             status_code=202, idempotent="native")
def verify_build(app_id: int, body: m.VerifyCommitInput):
    """The “Verify this build” hook: a build's repository, head commit and pull request. 202
    with the verification to follow; its release is known once the worker packaged the commit."""
    item = get_app(app_id)
    data = payload(body)
    session = session_of(app_id, integer(data, "build_id"))
    row = verify_commit_request(app_id, text(data, "repo", 200) or commit_checks.github_repo(item["git_url"]),
                                text(data, "sha", 64), integer(data, "pr_number"), text(data, "ref", 200),
                                session.get("build_id"), session.get("revision"), targets_of(data),
                                idempotency.key_of(data.get("request_key")))
    return verification_view(verifications.get(db(), row["id"]))


@routes.put(V1 + "/apps/{app_id:int}/verification-settings", require_role("owner"), response_model=m.AppSetup)
def verification_settings(app_id: int, body: m.VerificationSettings):
    get_app(app_id)
    data = payload(body)
    platform = data.get("target_platform", "")
    if platform not in commit_checks.PLATFORMS:
        fail(400, "Choose a target platform.")
    checks = "\n".join(line.strip()[:200] for line in text(data, "required_checks").splitlines()
                       if line.strip())[:4000]
    lab_url = text(data, "lab_url", 500)
    if lab_url and urlsplit(lab_url).scheme not in ("http", "https"):
        fail(400, "The lab agent URL must be HTTP or HTTPS.")
    with db():
        change_app(app_id, data, app_setup)
        db().execute("""UPDATE apps SET required_checks = ?, target_platform = ?, windows_check_pattern = ?,
                        lab_url = ?, launch = ? WHERE id = ?""",
                     (checks, platform, text(data, "windows_check_pattern", 200),
                      lab_url, text(data, "launch", 8000), app_id))
    return app_setup(app_id)


@routes.get(V1 + "/releases/{release_id:int}/reports/{report_id:int}", require_role("builder", resolve=release_app))
def verification_report(release_id: int, report_id: int):
    content = commit_checks.get_report(db(), release_id, report_id)
    if content is None:
        fail(404, "There is no such verification report.")
    return download(content, "application/json", f"verification-{release_id}-{report_id}.json")


@routes.post(V1 + "/releases/{release_id:int}/publish", require_role("owner", resolve=release_app), response_model=m.ReleaseDetail)
def publish_release(release_id: int):
    item = get_release(release_id)
    holder = f"{g.user}:{uuid.uuid4().hex[:8]}"
    try:
        acquire_publish(db(), item["app_id"], release_id, holder)
    except PublishConflict as error:
        fail(409, str(error), "publish_locked")
    try:
        with db():
            publish(db(), release_id)
            record("release.publish", "release", release_id, app_id=item["app_id"], version=item["version"],
                   commit=item["commit_sha"], archive_sha256=item["archive_sha"])
    except ValueError as error:
        fail(400, str(error))
    finally:
        release_publish(db(), item["app_id"], holder)
    return release_detail(release_id, notice=f"Published {item['version']}.")


@routes.get(V1 + "/releases/{release_id:int}/openshift.yaml", require_role("viewer", resolve=release_app))
def release_openshift(release_id: int, image_digest: str = ""):
    """The release manifest: pinned to the release's commit (the BuildConfig's git ref) and,
    given `image_digest`, to the built image by digest (demo_target.release_manifest). Viewers
    for a published release; builders and owners before."""
    item = db().execute("SELECT * FROM releases WHERE id = ?", (release_id,)).fetchone()
    if item is None:
        fail(404, "There is no such release.")
    if item["state"] != "published":
        auth.ensure("builder", app_id=item["app_id"])
    app = get_app(item["app_id"])
    surface = demo_target.kind(db(), app)
    if surface == "desktop":
        fail(404, "A desktop demo has no OpenShift deployment.")
    git_url = app["git_url"] or (f"https://github.com/{item['repository']}.git" if item["repository"] else "")
    try:
        text_ = demo_target.release_manifest(app, demo_target.backend(db(), app)["engine"], VERSION_PATH,
                                             {"commit": item["commit_sha"], "version": item["version"],
                                              "image_digest": image_digest.strip()}, git_url, surface)
    except ValueError as error:
        fail(400, str(error))
    return download(text_.encode("utf-8"), "application/yaml",
                    f"{demo_target.resource_name(app)}-release.yaml")


@routes.get(V1 + "/releases/{release_id:int}/download", require_role("viewer", resolve=release_app))
def download_release(release_id: int):
    """A published release for every viewer; one not yet published for builders and owners."""
    item = db().execute("SELECT * FROM releases WHERE id = ?", (release_id,)).fetchone()
    if item is None:
        fail(404, "There is no such release.")
    if item["state"] != "published":
        auth.ensure("builder", app_id=item["app_id"])
    return send_stored(item, "archive", f"demo-{item['app_id']}-{item['version']}.zip")

