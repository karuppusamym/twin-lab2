"""What several route modules need: the request's application and recording, restricted
evidence (who may open a recording), the audit write, and the portal's test hooks (`config`)."""

from __future__ import annotations

from twinlab2 import audit
from twinlab2 import auth
from twinlab2.rest import db, fail, g


def config() -> dict:
    """The running portal's settings and test hooks (`app.config`: GITHUB, TWINLAB_RUNNER, ...)."""
    return g.request.app.config


def record(action, object_type, object_id, app_id=None, **details):
    """An audit entry by the signed-in user (audit.py: append-only)."""
    audit.record(db(), g.user, action, object_type, object_id, app_id=app_id, **details)


def visible(rows, key="id"):
    """The rows of applications the user may see (any role; an admin sees all)."""
    ids = auth.visible_app_ids(db(), g.user, g.is_admin)
    return [row for row in rows if ids is None or row[key] in ids]


def get_app(app_id):
    row = db().execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone()
    if row is None:
        fail(404, "There is no such application.")
    return row


def get_recording(recording_id):
    row = db().execute("SELECT * FROM recordings WHERE id = ?", (recording_id,)).fetchone()
    if row is None:
        fail(404, "There is no such recording.")
    return row


def reviewer_of_recording(recording_id):
    """Restricted evidence: who added the recording (imported it, or paired the machine that
    sent or uploaded it), and the owners of the applications it is linked to."""
    return db().execute("""
        SELECT 1 FROM recording_uploads WHERE recording_id = ? AND added_by = ?
        UNION SELECT 1 FROM app_roles JOIN evidence ON evidence.app_id = app_roles.app_id
              WHERE evidence.recording_id = ? AND app_roles.user_name = ? AND app_roles.role = 'owner'
        UNION SELECT 1 FROM app_roles JOIN recording_apps ON recording_apps.app_id = app_roles.app_id
              WHERE recording_apps.recording_id = ? AND app_roles.user_name = ? AND app_roles.role = 'owner'
        UNION SELECT 1 FROM uploads JOIN machines ON machines.id = uploads.machine_id
              WHERE uploads.recording_id = ? AND machines.paired_by = ?
    """, (recording_id, g.user) * 4).fetchone() is not None


RESTRICTED = ("Recordings are restricted evidence: only their uploader and the owners of the applications "
              "they are linked to can open them.")


def ensure_reviewer(recording_id, detail=RESTRICTED):
    if not reviewer_of_recording(recording_id):
        fail(403, detail, "restricted")
