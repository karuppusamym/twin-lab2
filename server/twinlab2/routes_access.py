"""Access requests: someone refused (403 `role`) on an application asks its owners for a role.

- `POST /api/v1/apps/{id}/access-requests` (any signed-in user): a role and a reason. It answers
  the same 202 whether or not the application exists, so asking reveals nothing a 403 hides. A
  second request while one is pending replaces its role and reason (one pending request per
  person and application).
- `GET /api/v1/access-requests`: the caller's own requests with their outcome (`mine`), and the
  pending requests the caller decides (`to_decide`: on applications they own; every one for a
  portal admin, who manages role assignments).
- `POST /api/v1/access-requests/{id}/decision`: approve (the role is assigned, as on the access
  table) or decline, with an optional note the requester sees.

Every request and decision is in the audit history: `access.request`, `access.approve`,
`access.decline` (and `role.assign` for the role an approval gives).
"""

from __future__ import annotations

from twinlab2 import api_models as m
from twinlab2 import auth
from twinlab2 import registry
from twinlab2.rest import Routes, db, fail, g
from twinlab2.views import record

V1 = "/api/v1"
routes = Routes()
PENDING, APPROVED, DECLINED = "pending", "approved", "declined"
SENT = "Your request was sent to the application's owners. You will see their answer on your home page."


def app_row(app_id):
    return db().execute("SELECT id, name FROM apps WHERE id = ?", (app_id,)).fetchone()


def shape(row, names=None):
    """An AccessRequest. `app_name` only where the caller may see the application (`names`)."""
    names = names if names is not None else {}
    return {"id": row["id"], "app_id": row["app_id"], "app_name": names.get(row["app_id"]),
            "user": row["user_name"], "role": row["role"], "reason": row["reason"], "state": row["state"],
            "created_at": row["created_at"], "decided_by": row["decided_by"], "decided_at": row["decided_at"],
            "note": row["note"] or ""}


def visible_names(app_ids):
    """{app_id: name} of the applications the caller may see among `app_ids`."""
    out = {}
    for app_id in set(app_ids):
        if auth.can("viewer", app_id=app_id):
            row = app_row(app_id)
            if row is not None:
                out[app_id] = row["name"]
    return out


def may_decide(app_id):
    return bool(g.is_admin) or auth.role_of(db(), app_id, g.user) == "owner"


@routes.post(V1 + "/apps/{app_id:int}/access-requests", auth.signed_in, response_model=m.AccessRequestSent,
             status_code=202)
def request_access(app_id: int, body: m.AccessRequestInput):
    role = body.role.value if hasattr(body.role, "value") else str(body.role)
    reason = (body.reason or "").strip()
    if not reason:
        fail(400, "Say why you need the role: the owners decide from it.",
             errors=[{"field": "reason", "message": "required"}])
    reason = reason[:500]
    held = auth.role_of(db(), app_id, g.user)
    if held is not None and auth.RANK[held] >= auth.RANK[role]:
        fail(400, f"You already hold the {held} role on this application.",
             errors=[{"field": "role", "message": "already held"}])
    if app_row(app_id) is None:  # the same answer as for an application that exists
        return {"state": PENDING, "role": role, "message": SENT}
    with db():
        pending = db().execute("SELECT id FROM access_requests WHERE app_id = ? AND user_name = ? AND state = ?",
                               (app_id, g.user, PENDING)).fetchone()
        if pending is not None:
            db().execute("UPDATE access_requests SET role = ?, reason = ? WHERE id = ?", (role, reason, pending["id"]))
            request_id = pending["id"]
        else:
            request_id = db().execute(
                "INSERT INTO access_requests(app_id, user_name, role, reason, state, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?) RETURNING id", (app_id, g.user, role, reason, PENDING, registry.now())
            ).fetchone()[0]
        record("access.request", "access_request", request_id, app_id=app_id, user=g.user, role=role,
               reason=reason, previous_held=held)
    return {"state": PENDING, "role": role, "message": SENT}


@routes.get(V1 + "/access-requests", auth.signed_in, response_model=m.AccessRequestList)
def list_access_requests():
    mine = db().execute("SELECT * FROM access_requests WHERE user_name = ? ORDER BY id DESC LIMIT 50",
                        (g.user,)).fetchall()
    pending = db().execute("SELECT * FROM access_requests WHERE state = ? ORDER BY id", (PENDING,)).fetchall()
    to_decide = [row for row in pending if may_decide(row["app_id"])]
    names = visible_names([r["app_id"] for r in [*mine, *to_decide]])
    return {"mine": [shape(r, names) for r in mine], "to_decide": [shape(r, names) for r in to_decide]}


@routes.post(V1 + "/access-requests/{request_id:int}/decision", auth.signed_in, response_model=m.AccessRequest)
def decide_access_request(request_id: int, body: m.AccessDecision):
    row = db().execute("SELECT * FROM access_requests WHERE id = ?", (request_id,)).fetchone()
    if row is None or not (may_decide(row["app_id"]) or row["user_name"] == g.user):
        fail(404, "There is no such access request.")
    if not may_decide(row["app_id"]):
        fail(403, "Only the application's owners, or a portal admin, decide its access requests.", "role")
    if row["state"] != PENDING:
        fail(409, f"This request was already {row['state']} by {row['decided_by']}.", "state_conflict")
    decision = body.decision.value if hasattr(body.decision, "value") else str(body.decision)
    note = (body.note or "").strip()[:500]
    role = row["role"]
    if body.role is not None:
        role = body.role.value if hasattr(body.role, "value") else str(body.role)
    state = APPROVED if decision == "approve" else DECLINED
    with db():
        if state == APPROVED:
            held = auth.role_of(db(), row["app_id"], row["user_name"])
            if held is None or auth.RANK[held] < auth.RANK[role]:  # never lowers a role held since
                try:
                    previous = auth.assign(db(), row["app_id"], row["user_name"], role, g.user, registry.now())
                except ValueError as error:
                    fail(400, str(error))
                record("role.assign", "role", f"{row['app_id']}/{row['user_name']}", app_id=row["app_id"],
                       user=row["user_name"], role=role, previous=previous, via="access request")
        changed = db().execute(
            "UPDATE access_requests SET state = ?, role = ?, decided_by = ?, decided_at = ?, note = ? "
            "WHERE id = ? AND state = ?", (state, role, g.user, registry.now(), note, request_id, PENDING)).rowcount
        if changed != 1:
            fail(409, "Someone decided this request just now: reload.", "state_conflict")
        record(f"access.{decision}", "access_request", request_id, app_id=row["app_id"], user=row["user_name"],
               role=role, requested=row["role"], note=note)
    updated = db().execute("SELECT * FROM access_requests WHERE id = ?", (request_id,)).fetchone()
    return shape(updated, visible_names([updated["app_id"]]))
