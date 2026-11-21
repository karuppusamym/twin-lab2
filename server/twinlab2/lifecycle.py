"""The data lifecycle: retention, owner-requested deletion, purges, tombstones and audit export.

Deleting stored bytes never deletes the record that they existed:

- an `artifacts` row whose object was removed keeps its SHA-256, size and kind and gets
  `deleted_at`, `deleted_reason` and the `tombstone_id` that explains it. Opening it raises
  `artifacts.ArtifactDeleted`, which the API answers with 410 `gone`;
- a **tombstone** (`tombstones`) records what was deleted, when, by whom and why (`retention`,
  `owner_request`, `purge`): the object's type and id, its application and recording, its
  SHA-256 and size, and details. Tombstones are append-only. Release and application pages list
  the tombstones of their application, so a release still shows what was deleted from it.

Retention (`policy`, `apply_retention`): a number of days per kind, `None` keeping it forever.

| kind | what goes once older than its days |
| --- | --- |
| `raw_recordings` | the uploaded recording bundles (`uploads` → `restricted_evidence` artifacts); the recording's steps stay |
| `evidence` | uploaded evidence files (`attachments` and their artifacts) |
| `frozen_packages` | the bytes of frozen builder packages, except each application's latest revision and any revision a release was made from |
| `build_logs` | build conversation messages (`build_messages`) |
| `audit` | audit history rows (`audit_log`), through the prune window of migration 16 |

The policy is stored in `settings` (`retention_policy`), with defaults from
`TWINLAB2_RETENTION_<KIND>_DAYS`. A run is a `retention` job (worker.py): a dry run counts what
would go; a real run deletes it, writes tombstones, and records the run (`retention_runs`) and an
audit row (`retention.apply`). `schedule: daily` re-queues the job every 24 hours.
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import logging
import os

from twinlab2 import artifacts
from twinlab2 import audit
from twinlab2 import jobqueue

log = logging.getLogger("twinlab2.lifecycle")

KINDS = ("raw_recordings", "evidence", "frozen_packages", "build_logs", "audit")
JOB = "retention"
SCHEDULE_KEY = "retention.scheduled"
POLICY_KEY = "retention_policy"
MIN_DAYS = {"audit": 30}  # the shortest an administrator may set
REASONS = ("retention", "owner_request", "purge")


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# -- the policy -----------------------------------------------------------------------------------


def default_policy(environ=None) -> dict:
    env = os.environ if environ is None else environ
    out = {}
    for kind in KINDS:
        value = (env.get(f"TWINLAB2_RETENTION_{kind.upper()}_DAYS") or "").strip()
        out[kind] = int(value) if value.isdigit() and int(value) > 0 else None
    schedule = (env.get("TWINLAB2_RETENTION_SCHEDULE") or "off").strip().lower()
    out["schedule"] = schedule if schedule in ("off", "daily") else "off"
    return out


def policy(db) -> dict:
    """The retention policy in force: the stored one, else the environment's defaults."""
    out = default_policy()
    row = db.execute("SELECT value, updated_by, updated_at FROM settings WHERE key = ?", (POLICY_KEY,)).fetchone()
    if row is not None:
        try:
            stored = json.loads(row["value"])
        except ValueError:
            stored = {}
        out.update({k: stored[k] for k in (*KINDS, "schedule") if k in stored})
        out["updated_by"], out["updated_at"] = row["updated_by"], row["updated_at"]
    else:
        out["updated_by"], out["updated_at"] = None, None
    return out


def check_policy(data) -> dict:
    """The policy in `data`, checked; ValueError names the first bad field."""
    if not isinstance(data, dict):
        raise ValueError("Send the policy as an object.")
    out = {}
    for kind in KINDS:
        value = data.get(kind)
        if value is None:
            out[kind] = None
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < MIN_DAYS.get(kind, 1) or value > 36500:
            raise ValueError(f"{kind}: a number of days from {MIN_DAYS.get(kind, 1)} to 36500, or null to keep forever.")
        out[kind] = value
    schedule = data.get("schedule", "off")
    if schedule not in ("off", "daily"):
        raise ValueError("schedule: off or daily.")
    out["schedule"] = schedule
    return out


def save_policy(db, data, user) -> dict:
    """Store the policy (no commit) and queue or drop the daily run; audited."""
    checked = check_policy(data)
    db.execute("INSERT INTO settings (key, value, updated_by, updated_at) VALUES (?, ?, ?, ?) "
               "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_by = excluded.updated_by, "
               "updated_at = excluded.updated_at", (POLICY_KEY, json.dumps(checked), user, now()))
    audit.record(db, user, "retention.policy", "settings", POLICY_KEY, **checked)
    if checked["schedule"] == "daily":
        jobqueue.enqueue(db, JOB, {"dry_run": False, "scheduled": True, "requested_by": user},
                         delay=60, dedupe_key=SCHEDULE_KEY, commit=False)
    return checked


# -- tombstones and object deletion ----------------------------------------------------------------


def tombstone(db, *, actor, reason, object_type, object_id, app_id=None, recording_id=None, sha256=None,
              size=None, **details) -> int:
    if reason not in REASONS:
        raise ValueError(reason)
    row = db.execute(
        "INSERT INTO tombstones(at, actor, reason, object_type, object_id, app_id, recording_id, sha256, size, details) "
        "VALUES (?,?,?,?,?,?,?,?,?,?) RETURNING id",
        (now(), actor or "unknown", reason, object_type, str(object_id), app_id, recording_id, sha256, size,
         json.dumps(details, sort_keys=True, default=str))).fetchone()
    return row[0]


def tombstones(db, app_id=None, recording_id=None, limit=500) -> list[dict]:
    sql, args = "SELECT * FROM tombstones", []
    where = []
    if app_id is not None:
        where.append("app_id = ?")
        args.append(app_id)
    if recording_id is not None:
        where.append("recording_id = ?")
        args.append(recording_id)
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows = db.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
    return [{"id": r["id"], "at": r["at"], "actor": r["actor"], "reason": r["reason"],
             "object_type": r["object_type"], "object_id": r["object_id"], "app_id": r["app_id"],
             "recording_id": r["recording_id"], "sha256": r["sha256"], "size": r["size"],
             "details": json.loads(r["details"] or "{}")} for r in rows]


def delete_artifact(db, artifact_id, tombstone_id, reason, target=None) -> int:
    """Remove the stored object of an artifact row and mark the row deleted (no commit). Returns
    the bytes freed (0 when it was already gone)."""
    row = db.execute("SELECT deleted_at FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
    if row is None or row["deleted_at"]:
        return 0
    ref = artifacts.load(db, artifact_id, include_deleted=True)
    target = target or artifacts.store()
    if ref.backend == target.backend:
        target.delete(ref)
    else:
        log.warning("artifact %s is in the %s backend, not this portal's %s: only marked deleted",
                    artifact_id, ref.backend, target.backend)
    db.execute("UPDATE artifacts SET deleted_at = ?, deleted_reason = ?, tombstone_id = ? WHERE id = ?",
               (now(), reason, tombstone_id, artifact_id))
    return ref.size


# -- retention ------------------------------------------------------------------------------------


def cutoff(days) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()


def candidates(db, kind, days) -> list[dict]:
    """What retention would delete of `kind` (older than `days`)."""
    before = cutoff(days)
    if kind == "raw_recordings":
        rows = db.execute(
            "SELECT uploads.id AS upload_id, uploads.recording_id, a.id AS artifact_id, a.sha256, a.size, a.created_at "
            "FROM uploads JOIN artifacts a ON a.id = uploads.artifact_id "
            "WHERE a.deleted_at IS NULL AND a.created_at < ? ORDER BY a.id", (before,)).fetchall()
        return [dict(r) for r in rows]
    if kind == "evidence":
        rows = db.execute(
            "SELECT t.id AS attachment_id, t.app_id, t.title, a.id AS artifact_id, a.sha256, a.size, a.created_at "
            "FROM attachments t JOIN artifacts a ON a.id = t.artifact_id "
            "WHERE a.deleted_at IS NULL AND a.created_at < ? ORDER BY t.id", (before,)).fetchall()
        return [dict(r) for r in rows]
    if kind == "frozen_packages":
        keep = set()
        for row in db.execute("SELECT app_id, MAX(rev) AS rev FROM builder_packages GROUP BY app_id"):
            found = db.execute("SELECT revision FROM builder_packages WHERE app_id = ? AND rev = ?",
                               (row["app_id"], row["rev"])).fetchone()
            keep.add(found["revision"])
        keep |= {r["revision"] for r in db.execute("SELECT DISTINCT revision FROM releases")}
        rows = db.execute(
            "SELECT bp.app_id, bp.rev, bp.revision, a.id AS artifact_id, a.sha256, a.size, a.created_at "
            "FROM builder_packages bp JOIN brief_packages p ON p.revision = bp.revision "
            "JOIN artifacts a ON a.id = p.artifact_id WHERE a.deleted_at IS NULL AND a.created_at < ? "
            "ORDER BY bp.id", (before,)).fetchall()
        return [dict(r) for r in rows if r["revision"] not in keep]
    if kind == "build_logs":
        row = db.execute("SELECT COUNT(*), COALESCE(SUM(LENGTH(body)), 0) FROM build_messages WHERE created_at < ?",
                         (before,)).fetchone()
        return [{"messages": row[0], "size": row[1], "before": before}] if row[0] else []
    if kind == "audit":
        row = db.execute("SELECT COUNT(*), MIN(at), MAX(at) FROM audit_log WHERE at < ?", (before,)).fetchone()
        return [{"rows": row[0], "first": row[1], "last": row[2], "before": before}] if row[0] else []
    raise ValueError(kind)


def plan(db, pol=None) -> dict:
    """{kind: {"days", "count", "bytes"}}: what a run of the policy would delete now."""
    pol = pol or policy(db)
    out = {}
    for kind in KINDS:
        days = pol.get(kind)
        if days is None:
            out[kind] = {"days": None, "count": 0, "bytes": 0}
            continue
        found = candidates(db, kind, days)
        count = sum(c.get("messages", c.get("rows", 1)) for c in found)
        out[kind] = {"days": days, "count": count, "bytes": sum(int(c.get("size") or 0) for c in found)}
    return out


def apply_retention(db, actor, dry_run=True, run_id=None, target=None) -> dict:
    """Apply the policy (or only count, with dry_run). Each kind commits on its own; returns the
    result per kind, also stored on the run and in the audit row."""
    pol = policy(db)
    result = plan(db, pol)
    db.commit()
    if dry_run:
        return result
    for kind in KINDS:
        days = pol.get(kind)
        if days is None:
            continue
        found = candidates(db, kind, days)
        if not found:
            continue
        with db:
            freed = 0
            if kind == "raw_recordings":
                for c in found:
                    stone = tombstone(db, actor=actor, reason="retention", object_type="upload",
                                      object_id=c["upload_id"], recording_id=c["recording_id"], sha256=c["sha256"],
                                      size=c["size"], kind=kind, days=days, run=run_id)
                    freed += delete_artifact(db, c["artifact_id"], stone, "retention", target)
            elif kind == "evidence":
                for c in found:
                    stone = tombstone(db, actor=actor, reason="retention", object_type="attachment",
                                      object_id=c["attachment_id"], app_id=c["app_id"], sha256=c["sha256"],
                                      size=c["size"], title=c["title"], kind=kind, days=days, run=run_id)
                    freed += delete_artifact(db, c["artifact_id"], stone, "retention", target)
                    db.execute("DELETE FROM attachments WHERE id = ?", (c["attachment_id"],))
            elif kind == "frozen_packages":
                for c in found:
                    stone = tombstone(db, actor=actor, reason="retention", object_type="frozen_package",
                                      object_id=f"{c['app_id']}:{c['rev']}", app_id=c["app_id"], sha256=c["sha256"],
                                      size=c["size"], rev=c["rev"], revision=c["revision"], kind=kind, days=days,
                                      run=run_id)
                    freed += delete_artifact(db, c["artifact_id"], stone, "retention", target)
                    db.execute("UPDATE brief_packages SET archive = ? WHERE revision = ?", (b"", c["revision"]))
            elif kind == "build_logs":
                before = found[0]["before"]
                tombstone(db, actor=actor, reason="retention", object_type="build_messages", object_id=f"before:{before}",
                          size=found[0]["size"], messages=found[0]["messages"], days=days, run=run_id)
                db.execute("DELETE FROM build_messages WHERE created_at < ?", (before,))
                freed = found[0]["size"]
            elif kind == "audit":
                before = found[0]["before"]
                tombstone(db, actor=actor, reason="retention", object_type="audit_log", object_id=f"before:{before}",
                          rows=found[0]["rows"], first=found[0]["first"], last=found[0]["last"], days=days, run=run_id)
                prune_audit(db, before)
            result[kind]["freed_bytes"] = freed
    with db:
        audit.record(db, actor, "retention.apply", "retention_run", run_id or "-",
                     **{k: {"count": v["count"], "bytes": v["bytes"]} for k, v in result.items()})
    return result


def prune_audit(db, before):
    """Delete audit rows older than `before` through the prune window (migration 16): the
    append-only guard lets a DELETE through only for rows older than a window row, which exists
    only inside this transaction."""
    db.execute("INSERT INTO audit_prune_window(cutoff) VALUES (?)", (before,))
    db.execute("DELETE FROM audit_log WHERE at < ?", (before,))
    db.execute("DELETE FROM audit_prune_window WHERE cutoff = ?", (before,))


# -- retention runs (the job) -------------------------------------------------------------------------


def request_run(db, user, dry_run) -> dict:
    """Queue a retention run (no commit); returns its row."""
    at = now()
    row = db.execute("INSERT INTO retention_runs(requested_by, dry_run, state, policy, created_at) "
                     "VALUES (?, ?, 'queued', ?, ?) RETURNING id",
                     (user, 1 if dry_run else 0, json.dumps({k: v for k, v in policy(db).items()
                                                            if k in (*KINDS, "schedule")}), at)).fetchone()
    job_id = jobqueue.enqueue(db, JOB, {"run_id": row[0], "dry_run": bool(dry_run), "requested_by": user}, commit=False)
    db.execute("UPDATE retention_runs SET job_id = ? WHERE id = ?", (job_id, row[0]))
    return run_of(db, row[0])


def run_of(db, run_id):
    row = db.execute("SELECT * FROM retention_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        return None
    return {"id": row["id"], "requested_by": row["requested_by"], "dry_run": bool(row["dry_run"]),
            "state": row["state"], "policy": json.loads(row["policy"] or "{}"),
            "result": json.loads(row["result"]) if row["result"] else None, "error": row["error"],
            "created_at": row["created_at"], "finished_at": row["finished_at"]}


def runs(db, limit=20):
    return [run_of(db, r["id"]) for r in db.execute("SELECT id FROM retention_runs ORDER BY id DESC LIMIT ?", (limit,))]


def handle(db, job, ctx=None):
    """The worker's `retention` job: a requested run, or the daily one (re-queued for tomorrow)."""
    from twinlab2.builds import Outcome, again

    payload = job.get("payload") or {}
    actor = payload.get("requested_by") or "retention"
    run_id = payload.get("run_id")
    if payload.get("scheduled"):
        if policy(db)["schedule"] != "daily":
            return Outcome(result={"skipped": "schedule is off"})
        run_id = db.execute("INSERT INTO retention_runs(requested_by, dry_run, state, policy, job_id, created_at) "
                            "VALUES (?, 0, 'running', ?, ?, ?) RETURNING id",
                            ("schedule", json.dumps(policy(db)), job["id"], now())).fetchone()[0]
        db.commit()
    else:
        db.execute("UPDATE retention_runs SET state = 'running' WHERE id = ?", (run_id,))
        db.commit()
    try:
        result = apply_retention(db, "schedule" if payload.get("scheduled") else actor,
                                 dry_run=bool(payload.get("dry_run")), run_id=run_id)
    except Exception as error:  # noqa: BLE001 - recorded on the run, then the job retries
        db.rollback()
        db.execute("UPDATE retention_runs SET state = 'failed', error = ?, finished_at = ? WHERE id = ?",
                   (f"{type(error).__name__}: {error}"[:2000], now(), run_id))
        db.commit()
        raise
    db.execute("UPDATE retention_runs SET state = 'done', result = ?, finished_at = ? WHERE id = ?",
               (json.dumps(result), now(), run_id))
    db.commit()
    if payload.get("scheduled"):
        return again(24 * 3600)
    return Outcome(result={"run_id": run_id})


# -- owner-requested deletion of a recording ------------------------------------------------------------


def recording_apps(db, recording_id) -> list[int]:
    ids = {r[0] for r in db.execute("SELECT app_id FROM recording_apps WHERE recording_id = ?", (recording_id,))}
    ids |= {r[0] for r in db.execute("SELECT app_id FROM evidence WHERE recording_id = ?", (recording_id,))}
    return sorted(ids)


def packages_with_recording(db, recording_id, app_ids) -> list[dict]:
    """Frozen packages (not yet deleted) of `app_ids` whose evidence.json names the recording."""
    from twinlab2 import buildpack

    out = []
    for app_id in app_ids:
        rows = db.execute("SELECT bp.rev, bp.revision, p.artifact_id FROM builder_packages bp "
                          "JOIN brief_packages p ON p.revision = bp.revision WHERE bp.app_id = ? ORDER BY bp.rev",
                          (app_id,)).fetchall()
        for row in rows:
            if row["artifact_id"] is None or _deleted(db, row["artifact_id"]):
                continue
            try:
                files = buildpack.read(artifacts.read(artifacts.load(db, row["artifact_id"])))
                evidence = json.loads(files.get("evidence.json", b"{}"))
            except Exception:  # noqa: BLE001 - an unreadable package is reported by verify, not here
                continue
            if any(e.get("recording_id") == recording_id for e in evidence.get("evidence", [])):
                out.append({"app_id": app_id, "rev": row["rev"], "revision": row["revision"],
                            "artifact_id": row["artifact_id"]})
    return out


def _deleted(db, artifact_id) -> bool:
    row = db.execute("SELECT deleted_at FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
    return row is None or bool(row["deleted_at"])


def delete_recording(db, recording_id, actor, reason_text, purge_packages=False, target=None) -> dict:
    """Delete a recording and what was derived from it (no commit): its steps, application
    assignments, evidence links, contract and completeness rows, its uploaded bundles' bytes, and
    with `purge_packages` the frozen packages that include it. Tombstones record each."""
    row = db.execute("SELECT id, name, events FROM recordings WHERE id = ?", (recording_id,)).fetchone()
    if row is None:
        raise LookupError(recording_id)
    app_ids = recording_apps(db, recording_id)
    steps = len(json.loads(row["events"] or "[]"))
    stone = tombstone(db, actor=actor, reason="owner_request", object_type="recording", object_id=recording_id,
                      recording_id=recording_id, app_id=app_ids[0] if len(app_ids) == 1 else None,
                      name=row["name"], apps=app_ids, steps=steps, note=reason_text[:500])
    for app_id in app_ids if len(app_ids) > 1 else []:  # one tombstone per application, for its pages
        tombstone(db, actor=actor, reason="owner_request", object_type="recording", object_id=recording_id,
                  recording_id=recording_id, app_id=app_id, name=row["name"], steps=steps, note=reason_text[:500],
                  of=stone)
    freed = 0
    uploads = db.execute("SELECT id, artifact_id FROM uploads WHERE recording_id = ?", (recording_id,)).fetchall()
    for upload in uploads:
        if upload["artifact_id"] is not None:
            ref = artifacts.load(db, upload["artifact_id"], include_deleted=True)
            ustone = tombstone(db, actor=actor, reason="owner_request", object_type="upload", object_id=upload["id"],
                               recording_id=recording_id, sha256=ref.sha256, size=ref.size, of=stone)
            freed += delete_artifact(db, upload["artifact_id"], ustone, "owner_request", target)
    db.execute("UPDATE uploads SET state = 'rejected', last_error = 'deleted at the owner''s request' "
               "WHERE recording_id = ? AND state != 'confirmed'", (recording_id,))
    purged = []
    if purge_packages:
        for package in packages_with_recording(db, recording_id, app_ids):
            ref = artifacts.load(db, package["artifact_id"], include_deleted=True)
            pstone = tombstone(db, actor=actor, reason="owner_request", object_type="frozen_package",
                               object_id=f"{package['app_id']}:{package['rev']}", app_id=package["app_id"],
                               recording_id=recording_id, sha256=ref.sha256, size=ref.size, rev=package["rev"],
                               revision=package["revision"], of=stone)
            freed += delete_artifact(db, package["artifact_id"], pstone, "owner_request", target)
            db.execute("UPDATE brief_packages SET archive = ? WHERE revision = ?", (b"", package["revision"]))
            purged.append({"app_id": package["app_id"], "rev": package["rev"]})
    for table in ("evidence", "recording_apps", "recording_contracts", "recording_completeness",
                  "recording_uploads", "session_refs", "evidence_removals"):
        db.execute(f"DELETE FROM {table} WHERE recording_id = ?", (recording_id,))
    db.execute("UPDATE capture_sessions SET recording_id = NULL WHERE recording_id = ?", (recording_id,))
    db.execute("DELETE FROM recordings WHERE id = ?", (recording_id,))
    for app_id in app_ids or [None]:
        audit.record(db, actor, "recording.delete", "recording", recording_id, app_id=app_id, tombstone=stone,
                     purged_packages=purged, uploads=len(uploads), reason=reason_text[:500])
    return {"recording_id": recording_id, "tombstone_id": stone, "apps": app_ids, "uploads_deleted": len(uploads),
            "packages_deleted": purged, "freed_bytes": freed}


# -- purge one file everywhere (a leaked picture) -----------------------------------------------------------


def find_sha256(db, sha256, target=None) -> dict:
    """Where a file with this SHA-256 is: artifact rows holding it as a whole, and stored ZIPs
    (frozen packages, release downloads, reports, bundles) that have a member with it."""
    sha256 = sha256.lower()
    whole = [dict(r) for r in db.execute(
        "SELECT id, kind, app_id, object_key, version_id, size, created_at FROM artifacts "
        "WHERE sha256 = ? AND deleted_at IS NULL ORDER BY id", (sha256,))]
    inside = []
    for row in db.execute("SELECT id, kind, app_id, size FROM artifacts WHERE deleted_at IS NULL AND sha256 != ? "
                          "AND content_type IN ('application/zip', 'application/octet-stream') ORDER BY id",
                          (sha256,)).fetchall():
        members = zip_members_with(db, row["id"], sha256, target)
        if members:
            inside.append({**dict(row), "members": members})
    return {"sha256": sha256, "artifacts": whole, "inside": inside}


def zip_members_with(db, artifact_id, sha256, target=None) -> list[str]:
    import zipfile

    try:
        data = artifacts.read(artifacts.load(db, artifact_id), target=target)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return [info.filename for info in archive.infolist() if not info.is_dir()
                    and hashlib.sha256(archive.read(info)).hexdigest() == sha256]
    except Exception:  # noqa: BLE001 - not a ZIP, or unreadable: nothing found in it
        return []


def purge_sha256(db, sha256, actor, note, target=None) -> dict:
    """Delete every stored copy of the file with this SHA-256 (no commit): the artifacts that are
    that file, the attachments rows that point at them, and every stored ZIP that contains it
    (whole: a ZIP cannot be partly purged). On S3 every version of the content-addressed key goes,
    including versions the catalog never recorded. Tombstones record each; audited as `purge`."""
    found = find_sha256(db, sha256, target)
    target = target or artifacts.store()
    stone = tombstone(db, actor=actor, reason="purge", object_type="file", object_id=found["sha256"],
                      sha256=found["sha256"], note=note[:500], artifacts=[a["id"] for a in found["artifacts"]],
                      containers=[a["id"] for a in found["inside"]])
    freed, keys = 0, set()
    for item in found["artifacts"]:
        keys.add(item["object_key"])
        freed += delete_artifact(db, item["id"], stone, "purge", target)
        for att in db.execute("SELECT id, app_id, title FROM attachments WHERE artifact_id = ?", (item["id"],)).fetchall():
            tombstone(db, actor=actor, reason="purge", object_type="attachment", object_id=att["id"],
                      app_id=att["app_id"], sha256=found["sha256"], size=item["size"], title=att["title"], of=stone)
            db.execute("DELETE FROM attachments WHERE id = ?", (att["id"],))
    for item in found["inside"]:
        ref = artifacts.load(db, item["id"], include_deleted=True)
        keys.add(ref.key)
        cstone = tombstone(db, actor=actor, reason="purge", object_type=item["kind"], object_id=item["id"],
                           app_id=item["app_id"], sha256=ref.sha256, size=ref.size, members=item["members"], of=stone)
        freed += delete_artifact(db, item["id"], cstone, "purge", target)
        db.execute("UPDATE brief_packages SET archive = ? WHERE artifact_id = ?", (b"", item["id"]))
    leftovers = 0
    purge_key = getattr(target, "purge_key", None)
    for key in sorted(keys):
        if purge_key is not None and key.rsplit("/", 1)[-1] == found["sha256"]:
            leftovers += purge_key(key)  # every version of the file's own content-addressed key
    audit.record(db, actor, "purge", "file", found["sha256"], tombstone=stone,
                 artifacts=len(found["artifacts"]), containers=len(found["inside"]), note=note[:500])
    return {"sha256": found["sha256"], "tombstone_id": stone, "artifacts_deleted": len(found["artifacts"]),
            "containers_deleted": [{"id": i["id"], "kind": i["kind"], "members": i["members"]} for i in found["inside"]],
            "other_versions_deleted": leftovers, "freed_bytes": freed}


# -- audit export -------------------------------------------------------------------------------------

AUDIT_COLUMNS = ("id", "at", "actor", "app_id", "action", "object_type", "object_id", "details")


def audit_rows(db, since=None, until=None, app_id=None, batch=1000):
    """Audit rows in id order, `since` <= at < `until` (ISO dates or times), in batches."""
    where, args = [], []
    if since:
        where.append("at >= ?")
        args.append(since)
    if until:
        where.append("at < ?")
        args.append(until)
    if app_id is not None:
        where.append("app_id = ?")
        args.append(app_id)
    last = 0
    while True:
        sql = "SELECT * FROM audit_log WHERE id > ?" + "".join(" AND " + w for w in where) + " ORDER BY id LIMIT ?"
        rows = db.execute(sql, (last, *args, batch)).fetchall()
        if not rows:
            return
        for row in rows:
            yield {c: row[c] for c in AUDIT_COLUMNS}
        last = rows[-1]["id"]


def export_audit(db, fmt="csv", **filters):
    """The audit rows as CSV (one header line) or JSON lines, in chunks of text."""
    if fmt == "json":
        for row in audit_rows(db, **filters):
            yield json.dumps(dict(row, details=json.loads(row["details"] or "{}")), ensure_ascii=False) + "\n"
        return
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(AUDIT_COLUMNS)
    for n, row in enumerate(audit_rows(db, **filters), 1):
        writer.writerow([_csv_safe(row[c]) for c in AUDIT_COLUMNS])
        if n % 500 == 0:
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate()
    yield buffer.getvalue()


def _csv_safe(value):
    """A cell a spreadsheet will not run as a formula (=, +, -, @ at the start)."""
    if value is None:
        return ""
    text = str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text
