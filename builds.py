"""Build with Devin: a tracked build of a frozen build pack in a connected GitHub repository.

A `builds` row binds what was built to what came out of it: the application, the frozen pack
revision and its archive SHA-256, the repository, the base branch resolved to a commit, the
attempt number, the ACU budget, the Devin session, and the pull request and its head commit.

States:

    queued -> starting -> running <-> waiting_for_answer -> ready_for_verification
                 |            ^
                 v            |                         (any non-final) -> failed | cancelled
          checking_started ---+

Devin finishing is *Ready for verification*, never Tested: the release checks decide that.

Starting a session costs money and Devin's v3 API has no idempotency key, so a create is never
repeated blindly:

- Each build has an `idempotency_tag`, sent as a session tag.
- Before the create the worker sets `create_token` with a guarded UPDATE: only one worker can
  hold it, and a worker that finds it set without a session id knows a create may have been sent
  (a timeout, a 5xx, or a worker that died mid-call). The build is then `checking_started`, and
  the worker looks for the tag. Found: that session is the build's. Not found after
  RECONCILE_CHECKS lookups: the token is cleared (guarded again) and the create is made once more.
- One active build per application: `active_app` is UNIQUE and set only while a build is not
  final, so a second "Build" click finds the first build instead of inserting.
"""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import uuid

import artifacts
import buildpack
import devin
import github
import jobqueue

PROJECT = Path(__file__).resolve().parent
SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_by TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS github_repos (
    repo TEXT PRIMARY KEY, default_branch TEXT NOT NULL, private INTEGER NOT NULL DEFAULT 0,
    connected_by TEXT NOT NULL, connected_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS frozen_packs (
    revision TEXT PRIMARY KEY REFERENCES briefs(revision), app_id INTEGER NOT NULL REFERENCES apps(id),
    sha256 TEXT NOT NULL, frozen_by TEXT NOT NULL, frozen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS builds (
    id INTEGER PRIMARY KEY, app_id INTEGER NOT NULL REFERENCES apps(id),
    revision TEXT NOT NULL REFERENCES frozen_packs(revision), pack_sha256 TEXT NOT NULL,
    repo TEXT NOT NULL, base_branch TEXT NOT NULL, base_sha TEXT NOT NULL, attempt INTEGER NOT NULL,
    previous_build INTEGER REFERENCES builds(id), previous_revision TEXT, change_summary TEXT NOT NULL,
    acu_budget INTEGER NOT NULL, state TEXT NOT NULL, active_app INTEGER UNIQUE,
    idempotency_tag TEXT NOT NULL UNIQUE, create_token TEXT, create_calls INTEGER NOT NULL DEFAULT 0,
    reconcile_checks INTEGER NOT NULL DEFAULT 0, attachment_url TEXT,
    devin_session_id TEXT, devin_url TEXT, devin_status TEXT, devin_status_detail TEXT, acus_consumed REAL,
    pr_number INTEGER, pr_url TEXT, head_sha TEXT, questions TEXT NOT NULL DEFAULT '[]',
    error TEXT, note TEXT, requested_by TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS build_messages (
    id INTEGER PRIMARY KEY, build_id INTEGER NOT NULL REFERENCES builds(id), author TEXT NOT NULL,
    body TEXT NOT NULL, state TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
)
"""
STATES = ("queued", "starting", "checking_started", "running", "waiting_for_answer",
          "ready_for_verification", "failed", "cancelled")
FINAL = ("ready_for_verification", "failed", "cancelled")
LABELS = {
    "queued": "Queued",
    "starting": "Starting",
    "checking_started": "Checking whether the build started",
    "running": "Running",
    "waiting_for_answer": "Waiting for your answer",
    "ready_for_verification": "Ready for verification",
    "failed": "Failed",
    "cancelled": "Cancelled",
}
SECRET_KEYS = ("DEVIN_API_KEY", "GITHUB_TOKEN")
SETTING_KEYS = ("DEVIN_API_KEY", "DEVIN_ORG_ID", "DEVIN_BASE_URL", "GITHUB_TOKEN", "GITHUB_API",
                "DEFAULT_ACU_BUDGET")
DEFAULT_ACU_BUDGET = 20
MAX_ACU_BUDGET = 1000
_FINAL_SQL = "(" + ", ".join(f"'{s}'" for s in FINAL) + ")"


class BuildError(ValueError):
    pass


def now():
    return jobqueue.stamp()


def migrate(db):
    jobqueue.migrate(db)
    for statement in SCHEMA.split(";"):
        if statement.strip():
            db.execute(statement)
    db.commit()


def connect(database):
    """A connection for the worker: rows by name, waiting on locks instead of failing."""
    db = sqlite3.connect(database, timeout=30)  # waits up to 30 s on a lock
    db.row_factory = sqlite3.Row
    artifacts.configure(artifacts.from_env(database))  # packs outside a request: beside this catalog
    return db


def is_integrity_error(exc):
    return isinstance(exc, sqlite3.IntegrityError) or type(exc).__name__ in ("IntegrityError", "UniqueViolation")


# --- settings and connections ----------------------------------------------------------------------

def stored_settings(db):
    return {row["key"]: row["value"] for row in db.execute("SELECT key, value FROM settings")}


def save_setting(db, key, value, user):
    if key not in SETTING_KEYS:
        raise KeyError(key)
    if value:
        db.execute("INSERT INTO settings (key, value, updated_by, updated_at) VALUES (?, ?, ?, ?) "
                   "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_by = excluded.updated_by, "
                   "updated_at = excluded.updated_at", (key, value, user, now()))
    else:
        db.execute("DELETE FROM settings WHERE key = ?", (key,))
    db.commit()


def devin_settings(db):
    return devin.settings(stored_settings(db))


def github_settings(db):
    return github.settings(stored_settings(db))


def default_budget(db):
    try:
        return int(stored_settings(db).get("DEFAULT_ACU_BUDGET") or DEFAULT_ACU_BUDGET)
    except ValueError:
        return DEFAULT_ACU_BUDGET


def connect_repo(db, repo, user, gh=None):
    """Check that the token can see `repo` and remember it with its default branch."""
    repo = github.normalize_repo(repo)
    info = (gh or github.GitHub(github_settings(db))).repository(repo)
    db.execute("INSERT INTO github_repos (repo, default_branch, private, connected_by, connected_at) "
               "VALUES (?, ?, ?, ?, ?) ON CONFLICT (repo) DO UPDATE SET default_branch = excluded.default_branch, "
               "private = excluded.private, connected_by = excluded.connected_by, connected_at = excluded.connected_at",
               (info["full_name"], info["default_branch"], int(info["private"]), user, now()))
    db.commit()
    return info


def repos(db):
    return db.execute("SELECT * FROM github_repos ORDER BY repo").fetchall()


# --- frozen packs ------------------------------------------------------------------------------------

def stored_archive(db, app_id, revision):
    row = db.execute("SELECT archive, artifact_id FROM brief_packages JOIN briefs USING (revision) "
                     "WHERE briefs.app_id = ? AND revision = ?", (app_id, revision)).fetchone()
    return None if row is None else artifacts.content(db, row, "archive")


def is_frozen(db, app_id, revision):
    return db.execute("SELECT * FROM frozen_packs WHERE app_id = ? AND revision = ?", (app_id, revision)).fetchone()


def freeze(db, app_id, revision, user):
    """Record a frozen pack: from now on builds of it send exactly these bytes. Only
    `packages.freeze` calls this, after the owner approved the package's SHA-256 on the preview
    (packages.py); `frozen_packs` is the index builds reference."""
    archive = stored_archive(db, app_id, revision)
    if archive is None:
        raise BuildError("That build pack revision is not stored for this application.")
    if is_frozen(db, app_id, revision) is None:
        db.execute("INSERT INTO frozen_packs (revision, app_id, sha256, frozen_by, frozen_at) VALUES (?, ?, ?, ?, ?)",
                   (revision, app_id, hashlib.sha256(archive).hexdigest(), user, now()))
        db.commit()
    return is_frozen(db, app_id, revision)


def frozen_archive(db, app_id, revision):
    """The frozen pack's bytes, checked against the digest recorded when it was frozen."""
    frozen = is_frozen(db, app_id, revision)
    if frozen is None:
        raise BuildError("Freeze the build pack before building: a build sends only a frozen pack.")
    archive = stored_archive(db, app_id, revision)
    if archive is None or hashlib.sha256(archive).hexdigest() != frozen["sha256"]:
        raise BuildError("The stored build pack no longer matches the digest it was frozen with.")
    return archive, frozen["sha256"]


def pack_changes(old, new):
    """['added X', 'changed Y', 'removed Z'] between two pack archives."""
    before, after = buildpack.read(old), buildpack.read(new)
    out = [f"added {p}" for p in after if p not in before]
    out += [f"changed {p}" for p in after if p in before and before[p] != after[p]]
    out += [f"removed {p}" for p in before if p not in after]
    return out


# --- builds ------------------------------------------------------------------------------------------

def get(db, build_id):
    return db.execute("SELECT * FROM builds WHERE id = ?", (build_id,)).fetchone()


def active(db, app_id):
    return db.execute("SELECT * FROM builds WHERE active_app = ?", (app_id,)).fetchone()


def for_app(db, app_id):
    return db.execute("SELECT * FROM builds WHERE app_id = ? ORDER BY id DESC", (app_id,)).fetchall()


def previous_of(db, app_id):
    """The build Devin last worked on for this app: what a later build is a change from."""
    return db.execute("SELECT * FROM builds WHERE app_id = ? AND devin_session_id IS NOT NULL "
                      "ORDER BY id DESC LIMIT 1", (app_id,)).fetchone()


def messages(db, build_id):
    return db.execute("SELECT * FROM build_messages WHERE build_id = ? ORDER BY id", (build_id,)).fetchall()


def _budget(value):
    try:
        budget = int(str(value).strip())
    except ValueError:
        raise BuildError("Give the ACU budget as a whole number.") from None
    if not 1 <= budget <= MAX_ACU_BUDGET:
        raise BuildError(f"Give an ACU budget between 1 and {MAX_ACU_BUDGET}.")
    return budget


def _insert(db, values):
    """Insert a build and its job in one transaction. (build, True) when inserted; (the active
    build, False) when the application already has one."""
    current = active(db, values["app_id"])
    if current is not None:
        return current, False
    columns = list(values)
    try:
        build_id = db.execute(f"INSERT INTO builds ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)}) "
                              "RETURNING id", [values[c] for c in columns]).fetchone()[0]
        jobqueue.enqueue(db, "build", {"build_id": build_id}, dedupe_key=f"build:{build_id}", commit=False)
        db.commit()
    except Exception as exc:
        db.rollback()
        if is_integrity_error(exc):
            current = active(db, values["app_id"])
            if current is not None:
                return current, False
        raise
    return get(db, build_id), True


def request(db, app_id, revision, repo, base_branch, change_summary, acu_budget, user, gh=None):
    """Queue a build of a frozen pack. (build, created): a duplicate request while the app has
    an active build returns that build with created=False and changes nothing."""
    current = active(db, app_id)
    if current is not None:
        return current, False
    archive, sha = frozen_archive(db, app_id, revision)
    repo = github.normalize_repo(repo)
    connected = db.execute("SELECT * FROM github_repos WHERE repo = ?", (repo,)).fetchone()
    if connected is None:
        raise BuildError("Connect that repository under Setup before building in it.")
    base_branch = (base_branch or "").strip() or connected["default_branch"]
    budget = _budget(acu_budget)
    change_summary = (change_summary or "").strip()[:8000]
    previous = previous_of(db, app_id)
    if previous is not None and previous["revision"] != revision and not change_summary:
        raise BuildError(f"Say what changed since the previous build (revision {previous['revision'][:12]}).")
    try:
        base_sha = (gh or github.GitHub(github_settings(db))).branch_commit(repo, base_branch)
    except github.GitHubError as exc:
        raise BuildError(str(exc)) from exc
    attempt = db.execute("SELECT COUNT(*) FROM builds WHERE app_id = ?", (app_id,)).fetchone()[0] + 1
    stamp = now()
    return _insert(db, {
        "app_id": app_id, "revision": revision, "pack_sha256": sha, "repo": repo, "base_branch": base_branch,
        "base_sha": base_sha, "attempt": attempt, "previous_build": previous["id"] if previous else None,
        "previous_revision": previous["revision"] if previous else None, "change_summary": change_summary,
        "acu_budget": budget, "state": "queued", "active_app": app_id,
        "idempotency_tag": f"twinlab2-build-{uuid.uuid4().hex}", "requested_by": user,
        "created_at": stamp, "updated_at": stamp})


def retry(db, build_id, user):
    """A new attempt of a failed or cancelled build: the same frozen pack, repository and base
    commit, and the same previous revision and change summary."""
    old = get(db, build_id)
    if old is None:
        raise LookupError("No such build.")
    if old["state"] not in ("failed", "cancelled"):
        raise BuildError("Only a failed or cancelled build can be retried.")
    frozen_archive(db, old["app_id"], old["revision"])
    attempt = db.execute("SELECT COUNT(*) FROM builds WHERE app_id = ?", (old["app_id"],)).fetchone()[0] + 1
    stamp = now()
    return _insert(db, {
        "app_id": old["app_id"], "revision": old["revision"], "pack_sha256": old["pack_sha256"], "repo": old["repo"],
        "base_branch": old["base_branch"], "base_sha": old["base_sha"], "attempt": attempt,
        "previous_build": old["previous_build"], "previous_revision": old["previous_revision"],
        "change_summary": old["change_summary"], "acu_budget": old["acu_budget"], "state": "queued",
        "active_app": old["app_id"], "idempotency_tag": f"twinlab2-build-{uuid.uuid4().hex}", "requested_by": user,
        "created_at": stamp, "updated_at": stamp})


def _finalise(db, build_id, state, error=None, note=None):
    """Move a build to a final state unless it already is in one. True when this call did it."""
    stamp = now()
    cursor = db.execute(f"UPDATE builds SET state = ?, active_app = NULL, error = COALESCE(?, error), "
                        f"note = COALESCE(?, note), finished_at = ?, updated_at = ? "
                        f"WHERE id = ? AND state NOT IN {_FINAL_SQL}", (state, error, note, stamp, stamp, build_id))
    db.commit()
    return cursor.rowcount == 1


def cancel(db, build_id, user):
    """Cancel now (the application can build again at once); the worker stops the Devin session,
    including one an uncertain create may have started."""
    build = get(db, build_id)
    if build is None:
        raise LookupError("No such build.")
    if not _finalise(db, build_id, "cancelled", note=f"Cancelled by {user}."):
        raise BuildError("This build has already finished.")
    jobqueue.enqueue(db, "build.cancel", {"build_id": build_id})
    return get(db, build_id)


def answer(db, build_id, text, user):
    build = get(db, build_id)
    if build is None:
        raise LookupError("No such build.")
    text = (text or "").strip()
    if not text:
        raise BuildError("Write an answer first.")
    if build["state"] not in ("waiting_for_answer", "running") or not build["devin_session_id"]:
        raise BuildError("Devin is not waiting for an answer on this build.")
    stamp = now()
    message_id = db.execute("INSERT INTO build_messages (build_id, author, body, state, created_at, updated_at) "
                            "VALUES (?, ?, ?, 'sending', ?, ?) RETURNING id",
                            (build_id, user, text[:8000], stamp, stamp)).fetchone()[0]
    jobqueue.enqueue(db, "build.message", {"build_id": build_id, "message_id": message_id}, commit=False)
    db.commit()


def prompt(db, build):
    app = db.execute("SELECT name FROM apps WHERE id = ?", (build["app_id"],)).fetchone()
    name = app["name"] if app else f"application {build['app_id']}"
    lines = [
        f"Build the TwinLab2 application \"{name}\" from the attached build pack.", "",
        f"- Build pack revision `{build['revision']}`, archive SHA-256 `{build['pack_sha256']}`. Its "
        "BUILD_TASK.md is the task; evidence.json has the same scenarios with the recorded events.",
        f"- Repository `{build['repo']}`: start from branch `{build['base_branch']}` at commit "
        f"`{build['base_sha']}` and open one pull request against `{build['base_branch']}`.",
        "- Ask when the pack leaves a decision open; record assumptions in BUILD-NOTES.md.",
        "- Do not call the result tested: TwinLab2 verifies the pull request's head commit independently.",
        f"- This is build attempt {build['attempt']} for this application; ACU budget {build['acu_budget']}.",
    ]
    if build["previous_revision"]:
        before = db.execute("SELECT pr_number, pr_url FROM builds WHERE id = ?", (build["previous_build"],)).fetchone()
        lines += ["", "## Changes since the previous build", "",
                  f"The previous build used pack revision `{build['previous_revision']}`"
                  + (f" and opened pull request #{before['pr_number']} ({before['pr_url']})." if before and before["pr_number"]
                     else ".")]
        if build["change_summary"]:
            lines += ["", build["change_summary"]]
        old = stored_archive(db, build["app_id"], build["previous_revision"])
        new = stored_archive(db, build["app_id"], build["revision"])
        if old and new and old != new:
            lines += ["", "Files that differ between the two packs:", ""]
            lines += [f"- {change}" for change in pack_changes(old, new)]
    return "\n".join(lines)


# --- the worker's side -------------------------------------------------------------------------------

class Context:
    """What the build handlers need. Tests replace the timings; the clients are made from the
    settings each time, so a key updated under Setup takes effect on the next job."""

    def __init__(self, poll_seconds=None, reconcile_delay=None, reconcile_checks=None, timeout=None):
        env = os.environ
        self.poll_seconds = float(poll_seconds if poll_seconds is not None else env.get("TWINLAB2_POLL_SECONDS", 30))
        self.reconcile_delay = float(reconcile_delay if reconcile_delay is not None
                                     else env.get("TWINLAB2_RECONCILE_SECONDS", 20))
        self.reconcile_checks = int(reconcile_checks if reconcile_checks is not None
                                    else env.get("TWINLAB2_RECONCILE_CHECKS", 2))
        self.timeout = float(timeout if timeout is not None else env.get("TWINLAB2_DEVIN_TIMEOUT", 30))

    def devin(self, db):
        return devin.make_client(devin_settings(db), timeout=self.timeout)

    def github(self, db):
        return github.GitHub(github_settings(db))


class Outcome:
    def __init__(self, done=True, delay=0.0, result=None, error=None, count=False):
        self.done, self.delay, self.result, self.error, self.count = done, delay, result, error, count


def again(delay, error=None, count=False):
    return Outcome(done=False, delay=delay, error=error, count=count)


def _update(db, build_id, guard="", **fields):
    fields["updated_at"] = now()
    cursor = db.execute(f"UPDATE builds SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ? {guard}",
                        [*fields.values(), build_id])
    db.commit()
    return cursor.rowcount == 1


def _bind(db, ctx, client, build, session):
    """The session is this build's. A build cancelled meanwhile has its session stopped."""
    sid = devin.client_module().session_id(str(session.get("session_id") or ""))
    _update(db, build["id"], "AND devin_session_id IS NULL", devin_session_id=sid, devin_url=session.get("url"),
            devin_status=session.get("status"), devin_status_detail=session.get("status_detail"))
    _update(db, build["id"], "AND state IN ('queued', 'starting', 'checking_started')", state="running", error=None)
    if get(db, build["id"])["state"] == "cancelled":
        try:
            client.terminate(sid)
        except devin.error_class() as exc:
            _update(db, build["id"], note=f"Cancelled, but the Devin session could not be stopped: {devin.explain(exc)}")
        return Outcome(result={"cancelled": sid})
    return again(0)


def handle(db, job, ctx):
    """Run one job. Returns an Outcome: finished, or back in the queue after `delay`."""
    payload = job["payload"]
    build = get(db, payload.get("build_id"))
    if build is None:
        return Outcome(result={"missing": payload.get("build_id")})
    try:
        client = ctx.devin(db)
    except (ValueError, devin.DevinUnavailable) as exc:
        if job["type"] == "build":
            _finalise(db, build["id"], "failed", error=str(exc))
            return Outcome(error=str(exc))
        return Outcome(error=str(exc))
    with closing(client):
        if job["type"] == "build":
            if build["state"] in FINAL:
                return Outcome(result={"state": build["state"]})
            if build["devin_session_id"]:
                return poll(db, ctx, client, build)
            return start(db, ctx, client, build)
        if job["type"] == "build.cancel":
            return stop(db, client, build)
        if job["type"] == "build.message":
            return send(db, client, build, payload["message_id"])
    return Outcome(error=f"unknown job type {job['type']}")


def start(db, ctx, client, build):
    DevinError = devin.error_class()
    if build["create_token"]:
        # A create may have been sent and its answer lost: look before creating again.
        _update(db, build["id"], f"AND state NOT IN {_FINAL_SQL}", state="checking_started")
        try:
            found = client.find_sessions(build["idempotency_tag"])
        except DevinError as exc:
            if exc.status in (401, 403):
                _finalise(db, build["id"], "failed", error=devin.explain(exc)
                          + " A session may have started: check the Devin web app before retrying.")
                return Outcome(error=str(exc))
            _update(db, build["id"], note=f"Could not check yet: {devin.explain(exc)}")
            return again(ctx.reconcile_delay)
        if found:
            found.sort(key=lambda s: str(s.get("created_at") or ""))
            if len(found) > 1:
                _update(db, build["id"], note=f"{len(found)} sessions carry this build's tag; following the first.")
            return _bind(db, ctx, client, build, found[0])
        if build["reconcile_checks"] + 1 < ctx.reconcile_checks:
            _update(db, build["id"], reconcile_checks=build["reconcile_checks"] + 1)
            return again(ctx.reconcile_delay)
        # Not found: no session has this tag, so creating one now cannot make a second. Clearing
        # the token is guarded, so only one worker goes on to create.
        released = db.execute("UPDATE builds SET create_token = NULL, reconcile_checks = 0, note = ?, updated_at = ? "
                              "WHERE id = ? AND create_token = ? AND devin_session_id IS NULL",
                              ("No Devin session had started; it was started again.", now(), build["id"],
                               build["create_token"])).rowcount
        db.commit()
        if released != 1:
            return again(1)
        build = get(db, build["id"])
        if build["state"] in FINAL:
            return Outcome(result={"state": build["state"]})

    if not build["attachment_url"]:
        try:
            archive, _ = frozen_archive(db, build["app_id"], build["revision"])
        except BuildError as exc:
            _finalise(db, build["id"], "failed", error=str(exc))
            return Outcome(error=str(exc))
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / f"twinlab2-build-pack-{build['revision'][:12]}.zip"
            path.write_bytes(archive)
            try:
                uploaded = client.upload(path)  # repeating an upload is harmless: no session, no cost
            except DevinError as exc:
                if exc.status in (401, 403) or 400 <= exc.status < 429:
                    _finalise(db, build["id"], "failed", error=devin.explain(exc))
                    return Outcome(error=str(exc))
                _update(db, build["id"], note=f"Uploading the pack: {devin.explain(exc)}")
                return again(max(5.0, ctx.reconcile_delay), error=str(exc), count=True)
        _update(db, build["id"], attachment_url=uploaded.get("url"))
        build = get(db, build["id"])

    token = uuid.uuid4().hex
    claimed = db.execute(f"UPDATE builds SET create_token = ?, state = 'starting', create_calls = create_calls + 1, "
                         f"updated_at = ? WHERE id = ? AND create_token IS NULL AND devin_session_id IS NULL "
                         f"AND state NOT IN {_FINAL_SQL}", (token, now(), build["id"])).rowcount
    db.commit()
    if claimed != 1:
        return again(1)
    try:
        session = client.create_session(
            prompt(db, build), repos=[build["repo"]], attachment_urls=[build["attachment_url"]],
            title=f"TwinLab2 build {build['id']} (attempt {build['attempt']})",
            tags=[build["idempotency_tag"], "twinlab2", f"twinlab2-app-{build['app_id']}"],
            max_acu_limit=build["acu_budget"])
    except DevinError as exc:
        if devin.uncertain(exc):
            _update(db, build["id"], f"AND state NOT IN {_FINAL_SQL}", state="checking_started",
                    note=f"Devin gave no clear answer ({exc.status or 'no answer'}); checking whether the session started.")
            return again(ctx.reconcile_delay)
        # A 4xx answer means Devin did not create anything: the token can go.
        db.execute("UPDATE builds SET create_token = NULL WHERE id = ? AND create_token = ?", (build["id"], token))
        db.commit()
        if exc.status == 429:
            _update(db, build["id"], note=devin.explain(exc))
            return again(60, count=True)
        _finalise(db, build["id"], "failed", error=devin.explain(exc))
        return Outcome(error=str(exc))
    return _bind(db, ctx, client, get(db, build["id"]), session)


def poll(db, ctx, client, build):
    DevinError = devin.error_class()
    try:
        session = client.get_session(build["devin_session_id"])
    except DevinError as exc:
        if exc.status == 404:
            _finalise(db, build["id"], "failed", error="The Devin session no longer exists.")
            return Outcome(error=str(exc))
        _update(db, build["id"], note=devin.explain(exc))
        return again(ctx.poll_seconds * (4 if exc.status in (401, 403) else 1))
    state, note = devin.map_status(session.get("status"), session.get("status_detail"))
    fields = {"devin_status": session.get("status"), "devin_status_detail": session.get("status_detail"),
              "devin_url": session.get("url") or build["devin_url"], "acus_consumed": session.get("acus_consumed")}
    prs = devin.pull_requests(session, build["repo"])
    if prs:
        _, number, url = max(prs, key=lambda p: p[1])
        fields.update(pr_number=number, pr_url=url)
        try:
            fields["head_sha"] = ctx.github(db).pull(build["repo"], number)["head_sha"]
        except github.GitHubError as exc:
            note = f"Pull request #{number}: {exc}"
    elif state == "ready_for_verification":
        note = f"Devin finished without opening a pull request on {build['repo']}."
    questions = []
    if state == "waiting_for_answer":
        try:
            questions = devin.questions(client.list_messages(build["devin_session_id"]))
        except DevinError as exc:
            note = f"Could not read Devin's messages: {devin.explain(exc)}"
        questions = questions or ["Devin is waiting for an answer: open the session to see what it needs."]
    fields["questions"] = json.dumps(questions)
    fields["note"] = note
    _update(db, build["id"], "AND state != 'cancelled'", **fields)
    if state in FINAL:
        _finalise(db, build["id"], state, error=note if state == "failed" else None)
        return Outcome(result={"state": state})
    _update(db, build["id"], f"AND state NOT IN {_FINAL_SQL}", state=state)
    return again(ctx.poll_seconds)


def stop(db, client, build):
    """Stop the Devin session of a cancelled build, or every session an uncertain create may
    have started under its tag."""
    DevinError = devin.error_class()
    try:
        if build["devin_session_id"]:
            ids = [build["devin_session_id"]]
        elif build["create_token"]:
            ids = [devin.client_module().session_id(str(s["session_id"]))
                   for s in client.find_sessions(build["idempotency_tag"])]
            if ids:
                _update(db, build["id"], "AND devin_session_id IS NULL", devin_session_id=ids[0])
        else:
            ids = []
        for sid in ids:
            try:
                client.terminate(sid)
            except DevinError as exc:
                if exc.status != 404:
                    raise
    except DevinError as exc:
        if exc.transient:
            return again(30, error=str(exc), count=True)
        _update(db, build["id"], note=f"Cancelled here, but the Devin session could not be stopped: {devin.explain(exc)}")
        return Outcome(error=str(exc))
    return Outcome(result={"stopped": ids})


def send(db, client, build, message_id):
    DevinError = devin.error_class()
    row = db.execute("SELECT * FROM build_messages WHERE id = ? AND build_id = ?", (message_id, build["id"])).fetchone()
    if row is None or row["state"] != "sending":
        return Outcome(result={"skipped": message_id})
    try:
        client.send_message(build["devin_session_id"], row["body"])
    except DevinError as exc:
        # A message POST that got no clear answer may have arrived: say so, never send it twice.
        state = "uncertain" if devin.uncertain(exc) else "failed"
        error = ("It may not have reached Devin: open the session to check before answering again."
                 if state == "uncertain" else devin.explain(exc))
        db.execute("UPDATE build_messages SET state = ?, error = ?, updated_at = ? WHERE id = ?",
                   (state, error, now(), message_id))
        db.commit()
        return Outcome(error=str(exc))
    db.execute("UPDATE build_messages SET state = 'sent', updated_at = ? WHERE id = ?", (now(), message_id))
    db.commit()
    _update(db, build["id"], "AND state = 'waiting_for_answer'", state="running", questions="[]")
    return Outcome(result={"sent": message_id})


def give_up(db, job, error):
    """A job reclaimed MAX_ATTEMPTS times in a row: its worker keeps dying on it."""
    build_id = (job.get("payload") or {}).get("build_id")
    if job["type"] == "build" and build_id:
        _finalise(db, build_id, "failed",
                  error=f"The worker stopped {jobqueue.MAX_ATTEMPTS} times while handling this build ({error}). "
                        "See Setup, Build worker; a Devin session may have started, so check before retrying.")


# --- a worker started from the portal, for development ------------------------------------------------

_dev_worker = None


def start_dev_worker(database):
    """Start `python worker.py` for this catalog, unless the one started here still runs."""
    global _dev_worker
    if _dev_worker is not None and _dev_worker.poll() is None:
        return _dev_worker.pid, False
    log = Path(database).parent / "worker.log"
    flags = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" else {"start_new_session": True}
    with open(log, "ab") as out:
        _dev_worker = subprocess.Popen([sys.executable, str(PROJECT / "worker.py"), "--database", str(database)],
                                       cwd=PROJECT, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **flags)
    return _dev_worker.pid, True


def short(sha):
    return (sha or "")[:12]


def label(state):
    return LABELS.get(state, state)
