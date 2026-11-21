"""Verify as a job: a `verification` resource, worked by the worker through the job queue.

`POST /api/v1/releases/{id}/verify` and `POST /api/v1/apps/{id}/verify-commit` answer 202 with a
verification (`GET /api/v1/verifications/{id}`) instead of running the replay inside the HTTP
request: a replay takes minutes, longer than an OpenShift route or a proxy keeps a request open.

States: `queued` (waiting for a worker) → `running` → `passed` (the release is Tested), `failed`
(the checks ran and did not all pass: the release stays Packaged, the report says why) or `error`
(the checks could not run: a bug, the worker kept dying, the commit could not be packaged).
While it runs, `progress` holds the stage and one entry per scenario (pending, running, then the
scenario's result); the report it stores is linked when it ends, with what "Tested" covers
(`scope`) and the per-dimension results (`dimensions`: behavioural, structural, visual,
deployment).

Requests are idempotent: a `request_key` names one verification (a repeated request with it gets
the same one back, not a second run), and without a key a request while a verification of the
same release (or commit) is queued or running gets that one. One job per verification
(`dedupe_key = verify:<id>`). When the worker dies mid-run, the job's lease runs out and the next
worker starts the verification again from the start (`attempts` counts it; the replay of a
deployment is not resumable halfway, because it changed the deployment's data); after
`jobqueue.MAX_ATTEMPTS` the verification ends in `error`.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid

from twinlab2 import jobqueue, registry

JOB = "verify"
STATES = ("queued", "running", "passed", "failed", "error")
ACTIVE = ("queued", "running")
FINAL = ("passed", "failed", "error")
log = logging.getLogger("twinlab2.verifications")

# The table is created by migrations.py (14: async verification).


class Unknown(LookupError):
    pass


def _row(connection, verification_id):
    row = connection.execute("SELECT * FROM verifications WHERE id = ?", (verification_id,)).fetchone()
    return None if row is None else dict(row)


def get(connection, verification_id):
    return _row(connection, verification_id)


def latest_for_release(connection, release_id):
    row = connection.execute("SELECT * FROM verifications WHERE release_id = ? ORDER BY id DESC LIMIT 1",
                             (release_id,)).fetchone()
    return None if row is None else dict(row)


def _active(connection, kind, app_id, release_id, commit):
    if release_id is not None:
        row = connection.execute(
            "SELECT * FROM verifications WHERE release_id = ? AND state IN ('queued', 'running') "
            "ORDER BY id DESC LIMIT 1", (release_id,)).fetchone()
    else:
        row = connection.execute(
            "SELECT * FROM verifications WHERE app_id = ? AND kind = ? AND commit_sha = ? "
            "AND state IN ('queued', 'running') ORDER BY id DESC LIMIT 1", (app_id, kind, commit)).fetchone()
    return None if row is None else dict(row)


def request(connection, kind, app_id, params, user, request_key=None, release_id=None, commit=None):
    """(verification, created). `kind` is `release` (params: targets) or `commit` (params: repo,
    sha, pr_number, ref, build_id, revision, targets). The verification and its job are inserted
    in one transaction; a repeated request (same key, or none while one is active) changes nothing."""
    key = (request_key or "").strip()[:100]
    if key:
        row = connection.execute("SELECT * FROM verifications WHERE request_key = ?", (key,)).fetchone()
        if row is not None:
            return dict(row), False
    existing = _active(connection, kind, app_id, release_id, commit)
    if existing is not None:
        return existing, False
    key = key or f"auto:{uuid.uuid4().hex}"
    stamp = registry.now()
    try:
        cursor = connection.execute(
            "INSERT INTO verifications (app_id, release_id, kind, commit_sha, request_key, state, params, progress, "
            "reasons, requested_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, '[]', ?, ?, ?) "
            "RETURNING id",
            (app_id, release_id, kind, commit, key, json.dumps(params, sort_keys=True),
             json.dumps({"stage": "queued", "scenarios": []}), user, stamp, stamp))
        verification_id = cursor.fetchone()[0]
        job_id = jobqueue.enqueue(connection, JOB, {"verification_id": verification_id},
                                  dedupe_key=f"verify:{verification_id}", commit=False)
        connection.execute("UPDATE verifications SET job_id = ? WHERE id = ?", (job_id, verification_id))
        connection.commit()
    except Exception as exc:
        connection.rollback()
        from twinlab2 import db as database

        if isinstance(exc, database.IntegrityError):  # the same key, inserted meanwhile
            row = connection.execute("SELECT * FROM verifications WHERE request_key = ?", (key,)).fetchone()
            if row is not None:
                return dict(row), False
        raise
    return _row(connection, verification_id), True


def _update(connection, verification_id, guard="", **fields):
    fields["updated_at"] = registry.now()
    for name in ("progress", "reasons", "scope", "dimensions"):
        if name in fields and not isinstance(fields[name], str):
            fields[name] = json.dumps(fields[name], sort_keys=True)
    cursor = connection.execute(
        f"UPDATE verifications SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ? {guard}",
        [*fields.values(), verification_id])
    connection.commit()
    return cursor.rowcount == 1


class Progress:
    """What a running verification reports: its stage and each scenario's state. Written to the
    verification row as it changes (its own commits; nothing else is in a transaction then)."""

    def __init__(self, connection, verification_id):
        self.connection, self.id = connection, verification_id
        self.state = {"stage": "starting", "scenarios": []}

    def _save(self):
        _update(self.connection, self.id, progress=self.state)

    def stage(self, name):
        self.state["stage"] = name
        self._save()

    def scenarios(self, items):
        self.state["scenarios"] = [{"id": s["id"], "name": s["name"], "state": "pending"} for s in items]
        self._save()

    def scenario(self, scenario_id, state, result=None, why=None, surface=None):
        for item in self.state["scenarios"]:
            if item["id"] == scenario_id:
                item["state"] = state
                if result is not None:
                    item["result"] = result
                if why:
                    item["why"] = str(why)[:300]
                if surface:
                    item["surface"] = surface
        self._save()

    def done(self):
        return sum(1 for s in self.state["scenarios"] if s["state"] not in ("pending", "running"))


def view(row, report_url=None):
    """The API shape of a verification row (`Verification` in api/openapi.yaml)."""
    progress = json.loads(row["progress"] or "{}")
    items = progress.get("scenarios") or []
    report = None
    if row.get("report_id"):
        report = {"id": row["report_id"], "sha256": row["report_sha"],
                  "url": report_url(row["release_id"], row["report_id"]) if report_url else None}
    return {
        "id": row["id"], "app_id": row["app_id"], "release_id": row["release_id"], "kind": row["kind"],
        "state": row["state"], "commit_sha": row["commit_sha"], "requested_by": row["requested_by"],
        "request_key": row["request_key"], "attempts": row["attempts"] or 0,
        "created_at": row["created_at"], "started_at": row["started_at"], "finished_at": row["finished_at"],
        "progress": {"stage": progress.get("stage") or row["state"], "total": len(items),
                     "done": sum(1 for s in items if s.get("state") not in ("pending", "running")),
                     "scenarios": [{k: s.get(k) for k in ("id", "name", "state", "result", "why", "surface")}
                                   for s in items]},
        "tested": None if row["tested"] is None else bool(row["tested"]),
        "reasons": json.loads(row["reasons"] or "[]"),
        "report": report,
        "scope": json.loads(row["scope"]) if row["scope"] else None,
        "dimensions": json.loads(row["dimensions"]) if row["dimensions"] else None,
        "error": row["error"],
    }


# --- the job ---------------------------------------------------------------------------------------


class Context:
    """What a verification job uses: GitHub (checks, archives) and Twin Lab's checks runner.
    The worker's builds.Context is extended with these; tests give fakes."""

    def __init__(self, github=None, runner=None):
        self.verify_github, self.verify_runner = github, runner


def handle(connection, job, ctx):
    """Run one verification job. Returns a builds.Outcome (done)."""
    from twinlab2 import builds, portal
    from twinlab2 import verification as commit_checks
    from twinlab2 import independent

    verification_id = (job.get("payload") or {}).get("verification_id")
    row = _row(connection, verification_id)
    if row is None:
        return builds.Outcome(result={"missing": verification_id})
    if row["state"] in FINAL:
        return builds.Outcome(result={"already": row["state"]})
    params = json.loads(row["params"] or "{}")
    restarted = row["state"] == "running"
    _update(connection, row["id"], state="running", attempts=job.get("attempts") or 1,
            started_at=row["started_at"] or registry.now(), error=None)
    progress = Progress(connection, row["id"])
    if restarted:
        progress.state["restarted"] = True
    github = getattr(ctx, "verify_github", None) or commit_checks.GitHub()
    runner = getattr(ctx, "verify_runner", None) or independent.TwinLabChecks()
    try:
        release_id = row["release_id"]
        if row["kind"] == "commit":  # an existing release of the commit is reused (and its PR or ref kept)
            progress.stage("packaging the commit")
            release_id = portal.package_for_verification(
                connection, row["app_id"], params["repo"], params["sha"], github, params.get("pr_number"),
                params.get("ref") or "", build_id=params.get("build_id"), revision=params.get("revision"))
            connection.commit()
            _update(connection, row["id"], release_id=release_id)
        outcome = portal.verify_release(connection, release_id, github, runner, user=row["requested_by"],
                                        progress=progress, targets=params.get("targets") or {})
        connection.commit()
    except (ValueError, LookupError) as error:
        connection.rollback()
        _finish(connection, row["id"], "error", reasons=[str(error)], error=str(error))
        return builds.Outcome(result={"verification": row["id"], "state": "error"})
    report = outcome["report"]
    state = "passed" if outcome["tested"] else "failed"
    _finish(connection, row["id"], state, tested=outcome["tested"], reasons=outcome["reasons"],
            report_id=outcome.get("report_id"), report_sha=outcome.get("report_sha"),
            scope=report.get("scope"), dimensions=report.get("dimensions"))
    from twinlab2 import audit

    audit.record(connection, row["requested_by"], "release.verified", "release", release_id, app_id=row["app_id"],
                 verification=row["id"], state=state, report_sha=outcome.get("report_sha"))
    connection.commit()
    return builds.Outcome(result={"verification": row["id"], "state": state})


def _finish(connection, verification_id, state, **fields):
    row = _row(connection, verification_id)
    progress = json.loads(row["progress"] or "{}")
    progress["stage"] = state
    for item in progress.get("scenarios") or []:
        if item.get("state") in ("pending", "running"):
            item["state"] = "not-run"
    if "tested" in fields and fields["tested"] is not None:
        fields["tested"] = 1 if fields["tested"] else 0
    _update(connection, verification_id, state=state, finished_at=registry.now(), progress=progress, **fields)


def give_up(connection, job, error):
    """The worker kept stopping while it ran this verification."""
    verification_id = (job.get("payload") or {}).get("verification_id")
    if verification_id is None:
        return
    row = _row(connection, verification_id)
    if row is not None and row["state"] not in FINAL:
        why = (f"The worker stopped {jobqueue.MAX_ATTEMPTS} times while running this verification ({error}). "
               "Verify again once the worker is healthy (Setup, Build worker).")
        _finish(connection, verification_id, "error", reasons=[why], error=why)


# --- in-process work (tests, the demo portal) ---------------------------------------------------------


_threads: list[threading.Thread] = []


def run_in_process(target, ctx):
    """Work the queued verification jobs in a thread of this process (the demo portal and tests
    inject a fake Twin Lab runner, which a separate worker process cannot use)."""
    from twinlab2 import worker

    thread = threading.Thread(target=worker.run, args=(target,), daemon=True,
                              kwargs={"once": True, "ctx": ctx, "types": (JOB,), "owner": f"in-process:{uuid.uuid4().hex[:8]}"})
    thread.start()
    _threads.append(thread)
    return thread


def join_in_process(timeout=60.0):
    """Wait for the in-process verification threads (tests, before their catalog is removed)."""
    while _threads:
        _threads.pop().join(timeout)
