"""A durable job queue in the catalog database, worked by `python worker.py`.

A job is claimed with a lease (`lease_until`, `lease_owner`); the worker extends the lease
(heartbeat) while it works. A job whose lease ran out without being finished belongs to a worker
that died: the next claim takes it over and `attempts` counts it. A job that keeps being
reclaimed (MAX_ATTEMPTS in a row) fails instead of looping. `reschedule` puts a job back in the
queue for later (a poll), which resets the count.

SQL is kept portable (no SKIP LOCKED, no INSERT OR, no PRAGMA): a claim is a guarded
UPDATE whose WHERE repeats the claimable condition, so of two workers racing for one job only
one update matches. Timestamps are fixed-width UTC text, compared as strings.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import socket
import uuid

# The tables (jobs, workers) are created by migrations.py (3: build jobs).
STATES = ("queued", "running", "done", "failed")
MAX_ATTEMPTS = 5
LEASE_SECONDS = 60.0


def stamp(when=None):
    when = when or datetime.now(timezone.utc)
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def later(seconds):
    return stamp(datetime.now(timezone.utc) + timedelta(seconds=seconds))


def migrate(db):
    """The queue's tables (migration 3) and every other pending migration."""
    import migrations
    migrations.run(db)


def worker_id():
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def enqueue(db, kind, payload, delay=0.0, dedupe_key=None, commit=True):
    """A new queued job's id. With `dedupe_key`, an existing job of that key is re-queued (if it
    is not running) instead, and its id returned."""
    now = stamp()
    if dedupe_key:
        row = db.execute("SELECT id, state FROM jobs WHERE dedupe_key = ?", (dedupe_key,)).fetchone()
        if row is not None:
            if row[1] in ("done", "failed"):
                db.execute("UPDATE jobs SET state = 'queued', attempts = 0, run_after = ?, payload = ?, error = NULL, "
                           "lease_until = NULL, lease_owner = NULL, updated_at = ?, finished_at = NULL WHERE id = ?",
                           (later(delay), json.dumps(payload), now, row[0]))
            if commit:
                db.commit()
            return row[0]
    job_id = uuid.uuid4().hex
    db.execute("INSERT INTO jobs (id, type, state, attempts, payload, dedupe_key, run_after, created_at, updated_at) "
               "VALUES (?, ?, 'queued', 0, ?, ?, ?, ?, ?)",
               (job_id, kind, json.dumps(payload), dedupe_key, later(delay), now, now))
    if commit:
        db.commit()
    return job_id


_CLAIMABLE = "((state = 'queued' AND run_after <= ?) OR (state = 'running' AND lease_until < ?))"


def claim(db, owner, lease=LEASE_SECONDS, types=None):
    """The next due job, now leased to `owner`, as a dict; None when nothing is due. The worker
    gives up on a job whose `attempts` passed MAX_ATTEMPTS."""
    for _ in range(10):
        now = stamp()
        sql = f"SELECT id, attempts, state FROM jobs WHERE {_CLAIMABLE}"
        args = [now, now]
        if types:
            sql += f" AND type IN ({', '.join('?' for _ in types)})"
            args += list(types)
        row = db.execute(sql + " ORDER BY run_after, created_at LIMIT 1", args).fetchone()
        if row is None:
            return None
        cursor = db.execute(
            f"UPDATE jobs SET state = 'running', lease_owner = ?, lease_until = ?, attempts = attempts + 1, "
            f"updated_at = ? WHERE id = ? AND {_CLAIMABLE}", (owner, later(lease), now, row[0], now, now))
        db.commit()
        if cursor.rowcount == 1:
            return get(db, row[0])
    return None


def get(db, job_id):
    cursor = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
    row = cursor.fetchone()
    if row is None:
        return None
    job = dict(zip([d[0] for d in cursor.description], row))
    job["payload"] = json.loads(job["payload"] or "{}")
    return job


def _owned(db, sql, args, job_id, owner):
    cursor = db.execute(sql + " WHERE id = ? AND lease_owner = ? AND state = 'running'", (*args, job_id, owner))
    db.commit()
    return cursor.rowcount == 1


def heartbeat(db, job_id, owner, lease=LEASE_SECONDS):
    """Extend the lease. False when the job is no longer this worker's (it was reclaimed)."""
    return _owned(db, "UPDATE jobs SET lease_until = ?, updated_at = ?", (later(lease), stamp()), job_id, owner)


def finish(db, job_id, owner, result=None):
    now = stamp()
    return _owned(db, "UPDATE jobs SET state = 'done', result = ?, error = NULL, lease_until = NULL, "
                  "updated_at = ?, finished_at = ?", (json.dumps(result), now, now), job_id, owner)


def fail(db, job_id, owner, error):
    now = stamp()
    return _owned(db, "UPDATE jobs SET state = 'failed', error = ?, lease_until = NULL, updated_at = ?, "
                  "finished_at = ?", (str(error)[:2000], now, now), job_id, owner)


def reschedule(db, job_id, owner, delay, error=None, keep_attempts=False):
    """Back to the queue in `delay` seconds. A planned wait resets `attempts`; an error that
    should still count towards MAX_ATTEMPTS passes keep_attempts=True."""
    attempts = "attempts" if keep_attempts else "0"
    return _owned(db, f"UPDATE jobs SET state = 'queued', attempts = {attempts}, run_after = ?, error = ?, "
                  "lease_owner = NULL, lease_until = NULL, updated_at = ?",
                  (later(delay), None if error is None else str(error)[:2000], stamp()), job_id, owner)


def counts(db):
    return {row[0]: row[1] for row in db.execute("SELECT state, COUNT(*) FROM jobs GROUP BY state")}


def recent(db, limit=20):
    return [dict(zip(("id", "type", "state", "attempts", "lease_until", "lease_owner", "error", "run_after",
                      "updated_at"), row))
            for row in db.execute("SELECT id, type, state, attempts, lease_until, lease_owner, error, run_after, "
                                  "updated_at FROM jobs ORDER BY updated_at DESC LIMIT ?", (limit,))]


def worker_seen(db, wid, current_job=None, started=False, stopped=False):
    now = stamp()
    host, pid = socket.gethostname(), os.getpid()
    if started:
        db.execute("INSERT INTO workers (id, host, pid, started_at, seen_at) VALUES (?, ?, ?, ?, ?)",
                   (wid, host, pid, now, now))
    elif stopped:
        db.execute("UPDATE workers SET seen_at = ?, current_job = NULL, stopped_at = ? WHERE id = ?", (now, now, wid))
    else:
        db.execute("UPDATE workers SET seen_at = ?, current_job = ? WHERE id = ?", (now, current_job, wid))
    db.commit()


def workers(db, limit=10):
    return [dict(zip(("id", "host", "pid", "started_at", "seen_at", "current_job", "stopped_at"), row))
            for row in db.execute("SELECT id, host, pid, started_at, seen_at, current_job, stopped_at FROM workers "
                                  "ORDER BY seen_at DESC LIMIT ?", (limit,))]
