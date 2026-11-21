"""The operations API (portal admins): retention, purges, tombstones, the audit export and what the
Operations page shows; and an owner's request to delete a recording. lifecycle.py does the work."""

from __future__ import annotations

import os

from fastapi.responses import StreamingResponse

from twinlab2 import api_models as m
from twinlab2 import artifacts
from twinlab2 import auth
from twinlab2 import jobqueue
from twinlab2 import lifecycle
from twinlab2 import observability
from twinlab2.auth import require_role
from twinlab2.rest import Routes, db, fail, g
from twinlab2.views import get_app, record

V1 = "/api/v1"
routes = Routes()
ADMIN = require_role(auth.ADMIN, resolve=None)


def _payload(model) -> dict:
    return model.model_dump(exclude_unset=True, by_alias=True)


@routes.get(V1 + "/admin/operations", ADMIN, response_model=m.Operations)
def operations():
    """Storage, encryption, metrics, tracing, the retention policy, what it would delete now and
    the latest runs."""
    pol = lifecycle.policy(db())
    return {"storage": artifacts.store().describe(),
            "metrics": {"port": observability.metrics_port(),
                        "token": bool(os.environ.get("TWINLAB2_METRICS_TOKEN", "").strip())},
            "tracing": observability.tracing_enabled(),
            "log_format": "json" if os.environ.get("TWINLAB2_LOG_FORMAT", "text").strip().lower() == "json" else "text",
            "policy": pol, "plan": lifecycle.plan(db(), pol), "runs": lifecycle.runs(db(), 10),
            "queue": jobqueue.counts(db())}


@routes.put(V1 + "/admin/retention", ADMIN, response_model=m.RetentionPolicy)
def save_retention(body: m.RetentionPolicyInput):
    data = _payload(body)
    current = lifecycle.policy(db())
    merged = {k: data.get(k, current.get(k)) for k in (*lifecycle.KINDS, "schedule")}
    try:
        with db():
            lifecycle.save_policy(db(), merged, g.user)
    except ValueError as error:
        fail(400, str(error))
    return lifecycle.policy(db())


@routes.post(V1 + "/admin/retention/runs", ADMIN, response_model=m.RetentionRun, status_code=202)
def start_retention(body: m.RetentionRunInput):
    dry_run = _payload(body).get("dry_run", True)
    with db():
        run = lifecycle.request_run(db(), g.user, bool(dry_run))
        record("retention.request", "retention_run", run["id"], dry_run=bool(dry_run))
    return run


@routes.get(V1 + "/admin/retention/runs/{run_id:int}", ADMIN, response_model=m.RetentionRun)
def retention_run(run_id: int):
    run = lifecycle.run_of(db(), run_id)
    if run is None:
        fail(404, "There is no such retention run.")
    return run


def _when(value, name):
    value = (value or "").strip()
    if not value:
        return None
    from datetime import datetime

    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        fail(400, f"“{name}” must be an ISO date or time, such as 2026-11-01.",
             errors=[{"field": name, "message": "not an ISO date or time"}])
    return value


@routes.get(V1 + "/admin/audit-export", ADMIN)
def export_audit(since: str | None = None, until: str | None = None, app_id: int | None = None,
                 format: str = "csv"):  # noqa: A002 - the query parameter's name
    if format not in ("csv", "json"):
        fail(400, "“format” is csv or json.", errors=[{"field": "format", "message": "csv or json"}])
    filters = {"since": _when(since, "since"), "until": _when(until, "until"), "app_id": app_id}
    with db():
        record("audit.export", "audit_log", "-", format=format, **filters)
    # the rows are read on a connection of their own while the response streams
    from twinlab2 import db as database

    target = g.request.app.state.target
    media = "text/csv; charset=utf-8" if format == "csv" else "application/x-ndjson"
    name = f"twinlab2-audit-{(filters['since'] or 'start')[:10]}-{(filters['until'] or 'now')[:10]}." + (
        "csv" if format == "csv" else "jsonl")

    def body():
        connection = database.connect(target)
        try:
            for chunk in lifecycle.export_audit(connection, format, **filters):
                yield chunk.encode("utf-8")
            connection.commit()
        finally:
            connection.close()

    return StreamingResponse(body(), media_type=media,
                             headers={"Content-Disposition": f"attachment; filename={name}", "Cache-Control": "no-store"})


@routes.get(V1 + "/admin/tombstones", ADMIN, response_model=m.TombstoneList)
def list_tombstones(app_id: int | None = None, recording_id: str | None = None):
    return {"tombstones": lifecycle.tombstones(db(), app_id=app_id, recording_id=recording_id)}


def _sha(sha256):
    sha256 = sha256.lower()
    if not artifacts.SHA256.match(sha256):
        fail(400, "A SHA-256 is 64 hexadecimal characters.", errors=[{"field": "sha256", "message": "not a SHA-256"}])
    return sha256


@routes.get(V1 + "/admin/files/{sha256}", ADMIN, response_model=m.FileLocations)
def find_file(sha256: str):
    found = lifecycle.find_sha256(db(), _sha(sha256))
    db().commit()
    return {"sha256": found["sha256"],
            "artifacts": [{k: a[k] for k in ("id", "kind", "app_id", "size", "created_at")} for a in found["artifacts"]],
            "inside": found["inside"]}


@routes.post(V1 + "/admin/files/{sha256}/purge", ADMIN, response_model=m.PurgeResult)
def purge_file(sha256: str, body: m.PurgeInput):
    note = (_payload(body).get("note") or "").strip()
    if len(note) < 3:
        fail(400, "Say why the file is purged (kept in the audit history).",
             errors=[{"field": "note", "message": "at least 3 characters"}])
    with db():
        return lifecycle.purge_sha256(db(), _sha(sha256), g.user, note)


@routes.get(V1 + "/apps/{app_id:int}/tombstones", require_role("viewer", auth.ADMIN), response_model=m.TombstoneList)
def app_tombstones(app_id: int):
    get_app(app_id)
    return {"tombstones": lifecycle.tombstones(db(), app_id=app_id)}


def recording_owners(kwargs):
    """The applications a recording covers (an owner of every one may delete it); None (admins
    only) when it covers none or does not exist."""
    return lifecycle.recording_apps(db(), kwargs["recording_id"]) or None


@routes.post(V1 + "/recordings/{recording_id}/deletion", require_role("owner", auth.ADMIN, resolve=recording_owners),
             response_model=m.RecordingDeletion)
def delete_recording(recording_id: str, body: m.RecordingDeletionInput):
    data = _payload(body)
    reason = (data.get("reason") or "").strip()
    if len(reason) < 3:
        fail(400, "Say why the recording is deleted (kept in its tombstone).",
             errors=[{"field": "reason", "message": "at least 3 characters"}])
    try:
        with db():
            return lifecycle.delete_recording(db(), recording_id, g.user, reason,
                                              purge_packages=bool(data.get("purge_packages")))
    except LookupError:
        fail(404, "There is no such recording.")
