"""The audit history: an append-only record of who did what, when, to which object.

`record(db, actor, action, object_type, object_id, app_id=..., **details)` adds one row in the
caller's transaction. Rows are never updated or deleted: the code only inserts, and on SQLite
triggers refuse UPDATE and DELETE (on PostgreSQL grant the portal's role INSERT and SELECT only,
or add the equivalent rule). `app_id` is a plain column, not a foreign key, so the history of an
application outlives a merge that removes it.

Actions written today (dotted, `area.verb`):

- evidence: `evidence.upload`, `evidence.review` (recording selection saved), `evidence.share`
  (an attachment marked restricted / reviewed / synthetic), `evidence.remove`,
  `recording.import`;
- packages: `package.freeze`, `package.download`;
- builds and releases: `build.start` (for the build-job routes), `release.test`,
  `release.publish`, `deployment.register`;
- access: `role.assign`, `role.remove`, `user.save`;
- catalog: `app.create`, `app.merge`, `app.status`.
"""

import json

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY, at TEXT NOT NULL, actor TEXT NOT NULL, app_id INTEGER,
    action TEXT NOT NULL, object_type TEXT NOT NULL, object_id TEXT NOT NULL, details TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_log_app ON audit_log(app_id, id);
"""

SQLITE_GUARDS = """
CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""


def migrate(db):
    db.executescript(SCHEMA)
    if is_sqlite(db):
        db.executescript(SQLITE_GUARDS)


def is_sqlite(db):
    return type(db).__module__.startswith("sqlite3")


def now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def record(db, actor, action, object_type, object_id, app_id=None, **details):
    db.execute(
        "INSERT INTO audit_log(at, actor, app_id, action, object_type, object_id, details) VALUES (?,?,?,?,?,?,?)",
        (now(), actor or "unknown", app_id, action, object_type, str(object_id),
         json.dumps(details, sort_keys=True, ensure_ascii=False, default=str)),
    )


def history(db, app_id, limit=500):
    rows = db.execute("SELECT * FROM audit_log WHERE app_id = ? ORDER BY id DESC LIMIT ?", (app_id, limit)).fetchall()
    return [dict(row, details=json.loads(row["details"])) for row in rows]
