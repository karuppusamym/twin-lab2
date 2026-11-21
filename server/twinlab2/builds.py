"""Build sessions: one provider's attempt at one owner-approved package revision.

Three things are kept apart:

- a *recording* is evidence; one recording can feed several applications;
- an *approved package revision* (packages.py, builder_packages) is immutable;
- a *build session* (a `builds` row) is one provider's attempt: one package revision, one
  repository and starting commit, its own branch and workspace, budget, conversation
  (`build_messages`), outputs (pull request, head commit, cost) and verification results.

One revision can have many sessions: a Devin first implementation, a Claude alternative, a Claude
improvement from a commit another session made. Sessions are isolated (each its own branch and
workspace), so there is no "one active build per application" rule any more: a repeated click
carries the same request key and finds its session; **New session** deliberately starts another.
Publishing stays coordinated (portal.publish: one publish per application at a time, and a
session cannot take over another session's release).

States:

    queued -> starting -> running <-> waiting_for_answer -> ready_for_verification
                 |            ^
                 v            |                         (any non-final) -> failed | cancelled
          checking_started ---+

A provider finishing is *Ready for verification*, never Tested: the release checks decide that.

Starting a session can cost money, so a start is never repeated blindly (for every provider):

- Each session has an `idempotency_tag`, given to the provider at the start.
- Before the start the worker sets `create_token` with a guarded UPDATE: only one worker can
  hold it, and a worker that finds it set without a provider session knows a start may have been
  sent (a timeout, a 5xx, or a worker that died mid-call). The session is then `checking_started`
  and the worker asks the provider for the tag (`find_started`). Found: that session is this
  one's. Not found after RECONCILE_CHECKS lookups: the token is cleared (guarded again) and the
  start is made once more. A provider that cannot look a start up fails the session instead.

Messages (the session chat) are durable, with a kind (clarification, tool_approval, scope_change,
status) and a delivery status: queued -> sending -> sent [-> acknowledged], or
delivery_uncertain / failed. A reply that got no clear answer (or whose worker died mid-send) is
*Delivery uncertain* and is never resent automatically. A scope change is not sent: it becomes a
proposed scenario, i.e. a new package revision for the owner to review (`held_for_review`).
"""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile  # noqa: F401 - kept for callers that patch it
import uuid

from twinlab2 import artifacts
from twinlab2 import audit
from twinlab2 import buildpack
from twinlab2 import db as database
from twinlab2 import devin
from twinlab2 import github
from twinlab2 import jobqueue
from twinlab2 import paths
from twinlab2 import providers
from twinlab2.providers import base

# The tables are created by migrations.py (3: build jobs, 7: build sessions).
STATES = ("queued", "starting", "checking_started", "running", "waiting_for_answer",
          "ready_for_verification", "failed", "cancelled")
FINAL = ("ready_for_verification", "failed", "cancelled")
LABELS = {
    "queued": "Queued",
    "starting": "Starting",
    "checking_started": "Checking whether the session started",
    "running": "Running",
    "waiting_for_answer": "Waiting for your answer",
    "ready_for_verification": "Ready for verification",
    "failed": "Failed",
    "cancelled": "Cancelled",
}
MESSAGE_KINDS = ("clarification", "tool_approval", "scope_change", "status")
DELIVERY = ("queued", "sending", "sent", "acknowledged", "delivery_uncertain", "failed", "held_for_review", "received")
DELIVERY_LABELS = {
    "queued": "Queued", "sending": "Sending", "sent": "Sent", "acknowledged": "Acknowledged",
    "delivery_uncertain": "Delivery uncertain", "failed": "Not delivered", "held_for_review": "Held for package review",
    "received": "Received",
}
SECRET_KEYS = ("DEVIN_API_KEY", "GITHUB_TOKEN", "ANTHROPIC_API_KEY", "COPILOT_GITHUB_TOKEN")
SETTING_KEYS = ("DEVIN_API_KEY", "DEVIN_ORG_ID", "DEVIN_BASE_URL", "GITHUB_TOKEN", "GITHUB_API",
                "DEFAULT_ACU_BUDGET", "ANTHROPIC_API_KEY", "CLAUDE_MODEL", "DEFAULT_CLAUDE_BUDGET_USD",
                "COPILOT_GITHUB_TOKEN", "COPILOT_MODEL")
DEFAULT_ACU_BUDGET = 20
DEFAULT_BUDGETS = {"devin": ("DEFAULT_ACU_BUDGET", DEFAULT_ACU_BUDGET), "claude": ("DEFAULT_CLAUDE_BUDGET_USD", 10),
                   "copilot": (None, 50)}
MAX_ACU_BUDGET = 1000
ACK_TIMEOUT_SECONDS = float(os.environ.get("TWINLAB2_ACK_TIMEOUT", 600))
_FINAL_SQL = "(" + ", ".join(f"'{s}'" for s in FINAL) + ")"
SHA = re.compile(r"^[0-9a-f]{40}$")


class BuildError(ValueError):
    pass


def now():
    return jobqueue.stamp()


def migrate(db):
    """The build tables (migrations 3 and 7) and every other pending migration."""
    jobqueue.migrate(db)


def connect(target):
    """A connection for the worker (db.py: SQLite waits on locks instead of failing)."""
    artifacts.configure(artifacts.from_env(target))  # packs outside a request: beside this catalog
    return database.connect(target)


def is_integrity_error(exc):
    return isinstance(exc, database.IntegrityError)


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


def default_budget(db, provider="devin"):
    key, fallback = DEFAULT_BUDGETS.get(provider, (None, 1))
    try:
        return float(stored_settings(db).get(key) or fallback) if key else fallback
    except ValueError:
        return fallback


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


def package_rev(db, app_id, revision):
    row = db.execute("SELECT rev FROM builder_packages WHERE app_id = ? AND revision = ? ORDER BY rev DESC LIMIT 1",
                     (app_id, revision)).fetchone()
    return row["rev"] if row else None


def pack_changes(old, new):
    """['added X', 'changed Y', 'removed Z'] between two pack archives."""
    before, after = buildpack.read(old), buildpack.read(new)
    out = [f"added {p}" for p in after if p not in before]
    out += [f"changed {p}" for p in after if p in before and before[p] != after[p]]
    out += [f"removed {p}" for p in before if p not in after]
    return out


# --- sessions ----------------------------------------------------------------------------------------

def get(db, build_id):
    return db.execute("SELECT * FROM builds WHERE id = ?", (build_id,)).fetchone()


def active(db, app_id):
    """The newest session of the application that has not finished (several may be running)."""
    return db.execute(f"SELECT * FROM builds WHERE app_id = ? AND state NOT IN {_FINAL_SQL} ORDER BY id DESC LIMIT 1",
                      (app_id,)).fetchone()


def for_app(db, app_id):
    return db.execute("SELECT * FROM builds WHERE app_id = ? ORDER BY id DESC", (app_id,)).fetchall()


def previous_of(db, app_id):
    """The session a provider last worked on for this app: what a later session is a change from."""
    return db.execute("SELECT * FROM builds WHERE app_id = ? AND provider_session_id IS NOT NULL "
                      "ORDER BY id DESC LIMIT 1", (app_id,)).fetchone()


def messages(db, build_id):
    return db.execute("SELECT * FROM build_messages WHERE build_id = ? ORDER BY id", (build_id,)).fetchall()


def outstanding(db, build_id):
    """Questions and tool approvals from the provider not answered yet."""
    rows = messages(db, build_id)
    answered = {m["reply_to"] for m in rows if m["direction"] == "to_provider" and m["reply_to"]}
    build = get(db, build_id)
    waiting = build is not None and build["state"] == "waiting_for_answer"
    last_out = max([m["id"] for m in rows if m["direction"] == "to_provider"] or [0])
    return [m for m in rows if m["direction"] == "from_provider" and m["id"] not in answered
            and (m["kind"] == "tool_approval" or (m["kind"] == "clarification" and waiting and m["id"] > last_out))]


def provider_of(build):
    return build["provider"] if "provider" in build.keys() else "devin"


def check_budget(provider, value):
    try:
        return providers.adapter_class(provider).check_budget(value)
    except ValueError as exc:
        raise BuildError(str(exc)) from None


def _budget(value):  # Setup's DEFAULT_ACU_BUDGET check
    return check_budget("devin", value)


def _insert(db, values):
    """Insert a session and its job in one transaction. (session, True) when inserted; (the
    session of the same request key, False) for a repeated request."""
    key = values["request_key"]
    existing = db.execute("SELECT * FROM builds WHERE request_key = ?", (key,)).fetchone()
    if existing is not None:
        return existing, False
    columns = list(values)
    try:
        cursor = db.execute(f"INSERT INTO builds ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)}) "
                            "RETURNING id", [values[c] for c in columns])
        build_id = cursor.fetchone()[0]
        db.execute("UPDATE builds SET branch = ?, workspace = ? WHERE id = ?",
                   (branch_name(values["app_id"], build_id, values["provider"]),
                    str(workspace_dir(db, build_id)), build_id))
        jobqueue.enqueue(db, "build", {"build_id": build_id}, dedupe_key=f"build:{build_id}", commit=False)
        db.commit()
    except Exception as exc:
        db.rollback()
        if is_integrity_error(exc):
            existing = db.execute("SELECT * FROM builds WHERE request_key = ?", (key,)).fetchone()
            if existing is not None:
                return existing, False
        raise
    return get(db, build_id), True


def branch_name(app_id, build_id, provider):
    return f"twinlab2/app-{app_id}/session-{build_id}-{provider}"


def workspace_dir(db, build_id):
    root = os.environ.get("TWINLAB2_WORKSPACES") or (database.state_dir(db.target) / "workspaces")
    return Path(root) / f"session-{build_id}"


def _same_request(db, values):
    """A session queued by a request without a key, identical to this one and not finished: a
    double click from a page that predates request keys."""
    return db.execute(
        f"SELECT * FROM builds WHERE app_id = ? AND revision = ? AND repo = ? AND base_sha = ? AND provider = ? "
        f"AND model = ? AND requested_by = ? AND request_key LIKE 'auto:%' AND state NOT IN {_FINAL_SQL} "
        f"ORDER BY id DESC LIMIT 1",
        (values["app_id"], values["revision"], values["repo"], values["base_sha"], values["provider"],
         values["model"], values["requested_by"])).fetchone()


def request(db, app_id, revision, repo, base_branch, change_summary, acu_budget, user, gh=None, *,
            provider="devin", model="", request_key=None, start_commit="", parent_build=None):
    """Queue a build session of a frozen pack. (session, created): a repeated request (the same
    `request_key`) returns its session with created=False and changes nothing.

    `start_commit` (a full SHA, optional) starts from that commit instead of the branch head: an
    improvement of what another session built. `parent_build` records which session that was."""
    if not providers.enabled(provider):
        raise BuildError(f"{providers.LABELS.get(provider, provider)} is not enabled here.")
    request_key = (request_key or "").strip()[:100]
    if request_key:
        existing = db.execute("SELECT * FROM builds WHERE request_key = ?", (request_key,)).fetchone()
        if existing is not None:
            return existing, False
    _, sha = frozen_archive(db, app_id, revision)
    repo = github.normalize_repo(repo)
    connected = db.execute("SELECT * FROM github_repos WHERE repo = ?", (repo,)).fetchone()
    if connected is None:
        raise BuildError("Connect that repository under Setup before building in it.")
    base_branch = (base_branch or "").strip() or connected["default_branch"]
    budget = check_budget(provider, acu_budget)
    change_summary = (change_summary or "").strip()[:8000]
    previous = previous_of(db, app_id)
    if previous is not None and previous["revision"] != revision and not change_summary:
        raise BuildError(f"Say what changed since the previous build (revision {previous['revision'][:12]}).")
    parent = get(db, parent_build) if parent_build else None
    if parent_build and (parent is None or parent["app_id"] != app_id):
        raise BuildError("That parent session is not one of this application's.")
    start_commit = (start_commit or "").strip().lower()
    client = gh or github.GitHub(github_settings(db))
    try:
        if start_commit:
            if not SHA.fullmatch(start_commit):
                raise BuildError("Give the starting commit as a full 40-character SHA.")
            base_sha = client.commit(repo, start_commit)
        else:
            base_sha = client.branch_commit(repo, base_branch)
    except github.GitHubError as exc:
        raise BuildError(str(exc)) from exc
    attempt = db.execute("SELECT COUNT(*) FROM builds WHERE app_id = ?", (app_id,)).fetchone()[0] + 1
    stamp = now()
    model = (model or "").strip()[:100]
    values = {
        "app_id": app_id, "revision": revision, "pack_sha256": sha, "package_rev": package_rev(db, app_id, revision),
        "repo": repo, "base_branch": base_branch, "base_sha": base_sha, "attempt": attempt,
        "previous_build": previous["id"] if previous else None,
        "previous_revision": previous["revision"] if previous else None, "change_summary": change_summary,
        "acu_budget": int(budget) if provider == "devin" else 0, "budget": budget,
        "budget_unit": providers.BUDGET_UNITS.get(provider, ""), "cost_unit": providers.BUDGET_UNITS.get(provider, ""),
        "provider": provider, "model": model, "parent_build": parent["id"] if parent else None,
        "state": "queued", "idempotency_tag": f"twinlab2-build-{uuid.uuid4().hex}", "requested_by": user,
        "created_at": stamp, "updated_at": stamp}
    if not request_key:
        same = _same_request(db, values)
        if same is not None:
            return same, False
        request_key = f"auto:{uuid.uuid4().hex}"
    values["request_key"] = request_key
    return _insert(db, values)


def retry(db, build_id, user, request_key=None):
    """A new session of a failed or cancelled one: the same provider, model, frozen pack,
    repository and starting commit, and the same previous revision and change summary."""
    old = get(db, build_id)
    if old is None:
        raise LookupError("No such build.")
    if old["state"] not in ("failed", "cancelled"):
        raise BuildError("Only a failed or cancelled build can be retried.")
    frozen_archive(db, old["app_id"], old["revision"])
    key = (request_key or "").strip()[:100] or f"retry:{build_id}:{uuid.uuid4().hex}"
    attempt = db.execute("SELECT COUNT(*) FROM builds WHERE app_id = ?", (old["app_id"],)).fetchone()[0] + 1
    stamp = now()
    return _insert(db, {
        "app_id": old["app_id"], "revision": old["revision"], "pack_sha256": old["pack_sha256"],
        "package_rev": old["package_rev"], "repo": old["repo"], "base_branch": old["base_branch"],
        "base_sha": old["base_sha"], "attempt": attempt, "previous_build": old["previous_build"],
        "previous_revision": old["previous_revision"], "change_summary": old["change_summary"],
        "acu_budget": old["acu_budget"], "budget": old["budget"], "budget_unit": old["budget_unit"],
        "cost_unit": old["cost_unit"], "provider": old["provider"], "model": old["model"],
        "parent_build": old["parent_build"], "state": "queued", "request_key": key,
        "idempotency_tag": f"twinlab2-build-{uuid.uuid4().hex}", "requested_by": user,
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
    """Cancel now; the worker stops the provider session, including one an uncertain start may
    have made."""
    build = get(db, build_id)
    if build is None:
        raise LookupError("No such build.")
    if not _finalise(db, build_id, "cancelled", note=f"Cancelled by {user}."):
        raise BuildError("This build has already finished.")
    jobqueue.enqueue(db, "build.cancel", {"build_id": build_id})
    return get(db, build_id)


def interrupt(db, build_id, user):
    build = get(db, build_id)
    if build is None:
        raise LookupError("No such build.")
    if base.INTERRUPT not in providers.capabilities(provider_of(build)):
        raise BuildError(f"{providers.LABELS.get(provider_of(build))} sessions cannot be interrupted: cancel instead.")
    if build["state"] in FINAL or not build["provider_session_id"]:
        raise BuildError("Only a running session can be interrupted.")
    jobqueue.enqueue(db, "build.interrupt", {"build_id": build_id})


def _message(db, build_id, values):
    """Insert a chat message (deduplicated by its request key); (row, created)."""
    key = values.get("request_key")
    if key:
        row = db.execute("SELECT * FROM build_messages WHERE request_key = ?", (key,)).fetchone()
        if row is not None:
            return row, False
    stamp = now()
    values = {"build_id": build_id, "created_at": stamp, "updated_at": stamp, **values}
    columns = list(values)
    try:
        message_id = db.execute(
            f"INSERT INTO build_messages ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)}) RETURNING id",
            [values[c] for c in columns]).fetchone()[0]
        if values["state"] == "queued":
            jobqueue.enqueue(db, "build.message", {"build_id": build_id, "message_id": message_id}, commit=False)
        db.commit()
    except Exception as exc:
        db.rollback()
        if key and is_integrity_error(exc):
            return db.execute("SELECT * FROM build_messages WHERE request_key = ?", (key,)).fetchone(), False
        raise
    return db.execute("SELECT * FROM build_messages WHERE id = ?", (message_id,)).fetchone(), True


def answer(db, build_id, text, user, kind="clarification", request_key=None, reply_to=None):
    """Send `text` to the session (clarification), or, for a scope change, propose it as a new
    package revision for review instead of sending it. Returns (message, created)."""
    build = get(db, build_id)
    if build is None:
        raise LookupError("No such build.")
    text = (text or "").strip()
    if not text:
        raise BuildError("Write an answer first.")
    if kind not in ("clarification", "scope_change"):
        raise BuildError("Choose a clarification or a scope change.")
    key = (request_key or "").strip()[:100] or None
    if key:
        row = db.execute("SELECT * FROM build_messages WHERE request_key = ?", (key,)).fetchone()
        if row is not None:
            return row, False
    reply_to = int(reply_to) if reply_to else None
    if kind == "scope_change":
        return propose_scope_change(db, build, text, user, key, reply_to)
    if base.REPLY not in providers.capabilities(provider_of(build)):
        raise BuildError(f"{providers.LABELS.get(provider_of(build))} sessions take no replies.")
    if build["state"] not in ("waiting_for_answer", "running") or not build["provider_session_id"]:
        raise BuildError(f"{providers.LABELS.get(provider_of(build))} is not waiting for an answer on this build.")
    return _message(db, build_id, {"author": user, "body": text[:8000], "state": "queued", "kind": "clarification",
                                   "direction": "to_provider", "request_key": key, "reply_to": reply_to})


def decide_tool(db, build_id, message_id, decision, user, request_key=None):
    """Approve or deny a tool the provider asked to run: a separate control, never a chat answer."""
    build = get(db, build_id)
    if build is None:
        raise LookupError("No such build.")
    if decision not in ("approve", "deny"):
        raise BuildError("Approve or deny the tool.")
    asked = db.execute("SELECT * FROM build_messages WHERE id = ? AND build_id = ? AND kind = 'tool_approval' "
                       "AND direction = 'from_provider'", (message_id, build_id)).fetchone()
    if asked is None:
        raise BuildError("No such tool request on this session.")
    done = db.execute("SELECT * FROM build_messages WHERE reply_to = ? AND direction = 'to_provider'",
                      (message_id,)).fetchone()
    if done is not None:
        return done, False
    if build["state"] in FINAL:
        raise BuildError("This session has finished.")
    return _message(db, build_id, {
        "author": user, "body": decision, "state": "queued", "kind": "tool_approval", "direction": "to_provider",
        "request_key": (request_key or "").strip()[:100] or f"tool:{message_id}", "reply_to": message_id,
        "data": json.dumps({"decision": decision, "event_id": asked["event_id"],
                            **{k: v for k, v in json.loads(asked["data"] or "{}").items() if k in ("tool", "request_id")}})})


def propose_scope_change(db, build, text, user, key, reply_to):
    """An answer that changes agreed behaviour: a new proposed scenario, so the application's draft
    package changes and an owner must review and approve a new revision. The revision this
    session builds is untouched, and nothing is sent to the provider."""
    from twinlab2 import scenarios

    stamp = now()
    name = f"Agreed in build session #{build['id']}"[:200]
    cursor = db.execute(
        "INSERT INTO scenarios(app_id, position, name, notes) VALUES (?, ?, ?, ?) RETURNING id",
        (build["app_id"], scenarios.next_position(db, build["app_id"]), name,
         f"Proposed by {user} at {stamp} in build session #{build['id']} (package revision "
         f"{build['package_rev'] or build['revision'][:12]}); an owner reviews it with the next package.\n\n{text[:8000]}"))
    scenario_id = cursor.fetchone()[0]
    message_id = db.execute(
        "INSERT INTO build_messages (build_id, author, body, state, kind, direction, request_key, reply_to, "
        "scenario_id, created_at, updated_at) VALUES (?, ?, ?, 'held_for_review', 'scope_change', 'to_provider', "
        "?, ?, ?, ?, ?) RETURNING id",
        (build["id"], user, text[:8000], key, reply_to, scenario_id, stamp, stamp)).fetchone()[0]
    audit.record(db, user, "build.scope_change", "build", build["id"], app_id=build["app_id"],
                 revision=build["revision"], scenario_id=scenario_id, message_id=message_id)
    db.commit()
    return db.execute("SELECT * FROM build_messages WHERE id = ?", (message_id,)).fetchone(), True


def prompt(db, build):
    app = db.execute("SELECT name FROM apps WHERE id = ?", (build["app_id"],)).fetchone()
    name = app["name"] if app else f"application {build['app_id']}"
    lines = [
        f"Build the TwinLab2 application \"{name}\" from the attached build pack.", "",
        f"- Build pack revision `{build['revision']}`, archive SHA-256 `{build['pack_sha256']}`. Its "
        "BUILD_TASK.md is the task; evidence.json has the same scenarios with the recorded events.",
        f"- Repository `{build['repo']}`: start from branch `{build['base_branch']}` at commit "
        f"`{build['base_sha']}`, work on branch `{build['branch']}` and open one pull request against "
        f"`{build['base_branch']}`.",
        "- Ask when the pack leaves a decision open; record assumptions in BUILD-NOTES.md.",
        "- Do not call the result tested: TwinLab2 verifies the pull request's head commit independently.",
        f"- This is build attempt {build['attempt']} for this application; budget {_number(build)} "
        f"{build['budget_unit'] or 'ACU'}.",
    ]
    if build["parent_build"]:
        parent = get(db, build["parent_build"])
        if parent is not None:
            lines += ["", f"This session improves what build session #{parent['id']} "
                          f"({providers.LABELS.get(provider_of(parent), provider_of(parent))}) built: its commit "
                          f"`{build['base_sha']}` is the starting point."]
    if build["previous_revision"] and build["previous_revision"] != build["revision"]:
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
    elif build["change_summary"]:
        lines += ["", build["change_summary"]]
    return "\n".join(lines)


def _number(build):
    value = build["budget"] if build["budget"] is not None else build["acu_budget"]
    return int(value) if float(value).is_integer() else value


# --- the worker's side -------------------------------------------------------------------------------

class Context:
    """What the session handlers need. Tests replace the timings (and may replace `provider`);
    the adapters are made from the settings each time, so a key updated under Setup takes effect
    on the next job."""

    def __init__(self, poll_seconds=None, reconcile_delay=None, reconcile_checks=None, timeout=None):
        env = os.environ
        self.poll_seconds = float(poll_seconds if poll_seconds is not None else env.get("TWINLAB2_POLL_SECONDS", 30))
        self.reconcile_delay = float(reconcile_delay if reconcile_delay is not None
                                     else env.get("TWINLAB2_RECONCILE_SECONDS", 20))
        self.reconcile_checks = int(reconcile_checks if reconcile_checks is not None
                                    else env.get("TWINLAB2_RECONCILE_CHECKS", 2))
        self.timeout = float(timeout if timeout is not None else env.get("TWINLAB2_DEVIN_TIMEOUT", 30))

    def provider(self, db, name):
        """The adapter for `name`. ValueError / NotConfigured / DevinUnavailable when it cannot be used."""
        if not providers.enabled(name):
            raise base.NotConfigured(f"{providers.LABELS.get(name, name)} is not enabled here.")
        if name == "devin":
            return providers.adapter_class("devin")(devin_settings(db), timeout=self.timeout,
                                                     github_factory=lambda: self.github(db))
        return providers.adapter_class(name).from_settings(stored_settings(db), github_settings(db), self)

    def devin(self, db):  # kept for callers of the Devin-only worker
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


def _state(build):
    state = json.loads(build["provider_state"] or "{}")
    if build["attachment_url"] and "attachment_url" not in state:  # a build queued before sessions
        state["attachment_url"] = build["attachment_url"]
    return state


def _save_state(db, build, update):
    if update:
        state = {**_state(build), **update}
        _update(db, build["id"], provider_state=json.dumps(state, sort_keys=True))
    return get(db, build["id"])


def spec_of(db, build):
    archive, sha = frozen_archive(db, build["app_id"], build["revision"])
    app = db.execute("SELECT name FROM apps WHERE id = ?", (build["app_id"],)).fetchone()
    return base.SessionSpec(
        build_id=build["id"], app_id=build["app_id"], app_name=app["name"] if app else str(build["app_id"]),
        prompt=prompt(db, build), archive=archive, pack_sha256=sha, revision=build["revision"], repo=build["repo"],
        base_branch=build["base_branch"], base_sha=build["base_sha"], branch=build["branch"],
        workspace=Path(build["workspace"] or workspace_dir(db, build["id"])), model=build["model"],
        budget=build["budget"] if build["budget"] is not None else build["acu_budget"],
        budget_unit=build["budget_unit"], tag=build["idempotency_tag"],
        title=f"TwinLab2 build {build['id']} (attempt {build['attempt']})", attempt=build["attempt"])


def _bind(db, adapter, build, started):
    """The provider session is this build's. A build cancelled meanwhile has its session stopped."""
    _update(db, build["id"], "AND provider_session_id IS NULL", provider_session_id=started.session_id,
            provider_url=started.url, provider_status=started.status, provider_detail=started.detail)
    if build["provider"] == "devin":  # the columns the Devin-only pages read
        _update(db, build["id"], "AND devin_session_id IS NULL", devin_session_id=started.session_id,
                devin_url=started.url, devin_status=started.status, devin_status_detail=started.detail)
    if started.state:
        build = _save_state(db, get(db, build["id"]), started.state)
    _update(db, build["id"], "AND state IN ('queued', 'starting', 'checking_started')", state="running", error=None)
    build = get(db, build["id"])
    if build["state"] == "cancelled":
        try:
            adapter.cancel(dict(build))
        except base.ProviderError as exc:
            _update(db, build["id"], note=f"Cancelled, but the session could not be stopped: {adapter.explain(exc)}")
        return Outcome(result={"cancelled": started.session_id})
    return again(0)


def handle(db, job, ctx):
    """Run one job. Returns an Outcome: finished, or back in the queue after `delay`."""
    payload = job["payload"]
    build = get(db, payload.get("build_id"))
    if build is None:
        return Outcome(result={"missing": payload.get("build_id")})
    try:
        adapter = ctx.provider(db, provider_of(build))
    except (ValueError, base.ProviderError, devin.DevinUnavailable) as exc:
        if job["type"] == "build":
            _finalise(db, build["id"], "failed", error=str(exc))
        elif job["type"] == "build.message":
            _deliver(db, payload["message_id"], "failed", str(exc))
        return Outcome(error=str(exc))
    with closing(adapter):
        if job["type"] == "build":
            if build["state"] in FINAL:
                return Outcome(result={"state": build["state"]})
            if build["provider_session_id"]:
                return poll(db, ctx, adapter, build)
            return start(db, ctx, adapter, build)
        if job["type"] == "build.cancel":
            return stop(db, adapter, build)
        if job["type"] == "build.message":
            return send(db, adapter, build, payload["message_id"])
        if job["type"] == "build.interrupt":
            try:
                adapter.interrupt(dict(build))
            except base.ProviderError as exc:
                _update(db, build["id"], note=f"Could not interrupt: {adapter.explain(exc)}")
                return Outcome(error=str(exc))
            _update(db, build["id"], note="Interrupted: the session waits for your next message.")
            return Outcome(result={"interrupted": build["id"]})
    return Outcome(error=f"unknown job type {job['type']}")


def start(db, ctx, adapter, build):
    label = adapter.label
    try:
        spec = spec_of(db, build)
    except BuildError as exc:
        _finalise(db, build["id"], "failed", error=str(exc))
        return Outcome(error=str(exc))
    if build["create_token"]:
        # A start may have been sent and its answer lost: look before starting again.
        _update(db, build["id"], f"AND state NOT IN {_FINAL_SQL}", state="checking_started")
        if not adapter.has(base.RECONCILE_START):
            _finalise(db, build["id"], "failed", error=f"The start got no clear answer and {label} offers no way to "
                      "check whether it happened, so it is not repeated: check the provider before a new session.")
            return Outcome(error="start uncertain")
        try:
            found = adapter.find_started(spec, _state(build))
        except base.ProviderError as exc:
            if exc.auth:
                _finalise(db, build["id"], "failed", error=adapter.explain(exc)
                          + f" A session may have started: check {label} before retrying.")
                return Outcome(error=str(exc))
            _update(db, build["id"], note=f"Could not check yet: {adapter.explain(exc)}")
            return again(ctx.reconcile_delay)
        if found:
            found.sort(key=lambda s: str(s.created_at or ""))
            if len(found) > 1:
                _update(db, build["id"], note=f"{len(found)} sessions carry this build's tag; following the first.")
            return _bind(db, adapter, build, found[0])
        if build["reconcile_checks"] + 1 < ctx.reconcile_checks:
            _update(db, build["id"], reconcile_checks=build["reconcile_checks"] + 1)
            return again(ctx.reconcile_delay)
        # Not found: no session has this tag, so starting one now cannot make a second. Clearing
        # the token is guarded, so only one worker goes on to start.
        released = db.execute("UPDATE builds SET create_token = NULL, reconcile_checks = 0, note = ?, updated_at = ? "
                              "WHERE id = ? AND create_token = ? AND provider_session_id IS NULL",
                              (f"No {label} session had started; it was started again.", now(), build["id"],
                               build["create_token"])).rowcount
        db.commit()
        if released != 1:
            return again(1)
        build = get(db, build["id"])
        if build["state"] in FINAL:
            return Outcome(result={"state": build["state"]})

    try:
        build = _save_state(db, build, adapter.prepare(spec, _state(build)))
    except base.ProviderError as exc:
        if exc.auth or 400 <= exc.status < 429:
            _finalise(db, build["id"], "failed", error=adapter.explain(exc))
            return Outcome(error=str(exc))
        _update(db, build["id"], note=f"Preparing the session: {adapter.explain(exc)}")
        return again(max(5.0, ctx.reconcile_delay), error=str(exc), count=True)
    if build["provider"] == "devin" and _state(build).get("attachment_url") and not build["attachment_url"]:
        _update(db, build["id"], attachment_url=_state(build)["attachment_url"])

    token = uuid.uuid4().hex
    claimed = db.execute(f"UPDATE builds SET create_token = ?, state = 'starting', create_calls = create_calls + 1, "
                         f"updated_at = ? WHERE id = ? AND create_token IS NULL AND provider_session_id IS NULL "
                         f"AND state NOT IN {_FINAL_SQL}", (token, now(), build["id"])).rowcount
    db.commit()
    if claimed != 1:
        return again(1)
    try:
        started = adapter.start(spec, _state(build))
    except base.ProviderError as exc:
        if exc.uncertain:
            _update(db, build["id"], f"AND state NOT IN {_FINAL_SQL}", state="checking_started",
                    note=f"{label} gave no clear answer ({exc.status or 'no answer'}); checking whether the session started.")
            return again(ctx.reconcile_delay)
        # A clear refusal means nothing was started: the token can go.
        db.execute("UPDATE builds SET create_token = NULL WHERE id = ? AND create_token = ?", (build["id"], token))
        db.commit()
        if exc.rate_limited:
            _update(db, build["id"], note=adapter.explain(exc))
            return again(60, count=True)
        _finalise(db, build["id"], "failed", error=adapter.explain(exc))
        return Outcome(error=str(exc))
    return _bind(db, adapter, get(db, build["id"]), started)


def _deliver(db, message_id, state, error=None, guard=""):
    cursor = db.execute(f"UPDATE build_messages SET state = ?, error = ?, updated_at = ? WHERE id = ? {guard}",
                        (state, error, now(), message_id))
    db.commit()
    return cursor.rowcount == 1


def record_events(db, build, events):
    """Store the provider's events once each (by event id); apply acknowledgements."""
    for event in events:
        message_id = (event.data or {}).get("message_id")
        if event.kind == "ack" and message_id:
            _deliver(db, message_id, "acknowledged", guard="AND state IN ('sending', 'sent', 'delivery_uncertain')")
            continue
        if event.kind in ("uncertain", "failed") and message_id:
            _deliver(db, message_id, "delivery_uncertain" if event.kind == "uncertain" else "failed",
                     event.text or None, guard="AND state IN ('sending', 'sent')")
            continue
        stamp = now()
        db.execute("INSERT INTO build_messages (build_id, author, body, state, kind, direction, event_id, data, "
                   "created_at, updated_at) VALUES (?, ?, ?, 'received', ?, 'from_provider', ?, ?, ?, ?) "
                   "ON CONFLICT (build_id, event_id) DO NOTHING",
                   (build["id"], providers.LABELS.get(provider_of(build), provider_of(build)), (event.text or "")[:8000],
                    event.kind if event.kind in MESSAGE_KINDS else "status", event.event_id,
                    json.dumps(event.data or {}, sort_keys=True), stamp, stamp))
    db.commit()


def poll(db, ctx, adapter, build):
    label = adapter.label
    try:
        snap = adapter.read(dict(build))
    except base.ProviderError as exc:
        if exc.gone:
            _finalise(db, build["id"], "failed", error=f"The {label} session no longer exists.")
            return Outcome(error=str(exc))
        _update(db, build["id"], note=adapter.explain(exc))
        return again(ctx.poll_seconds * (4 if exc.auth else 1))
    record_events(db, build, snap.events)
    if adapter.has(base.ACKNOWLEDGE):
        db.execute("UPDATE build_messages SET state = 'delivery_uncertain', error = ?, updated_at = ? "
                   "WHERE build_id = ? AND state = 'sent' AND updated_at < ?",
                   (f"{label} has not confirmed it received this; it is not sent again.", now(), build["id"],
                    jobqueue.later(-ACK_TIMEOUT_SECONDS)))
        db.commit()
    fields = {"provider_status": snap.status, "provider_detail": snap.detail,
              "provider_url": snap.url or build["provider_url"], "note": snap.note,
              "questions": json.dumps(snap.questions if snap.state == base.WAITING else [])}
    if snap.cost is not None:
        fields["cost"] = snap.cost
    if snap.cursor is not None:
        fields["event_cursor"] = snap.cursor
    for key in ("pr_number", "pr_url", "head_sha", "branch"):
        if getattr(snap, key):
            fields[key] = getattr(snap, key)
    if build["provider"] == "devin":
        fields.update(devin_status=snap.status, devin_status_detail=snap.detail,
                      devin_url=fields["provider_url"], acus_consumed=snap.cost)
    _update(db, build["id"], "AND state != 'cancelled'", **fields)
    if snap.state_update:
        _save_state(db, get(db, build["id"]), snap.state_update)
    if snap.state in FINAL:
        _finalise(db, build["id"], snap.state, error=snap.note if snap.state == "failed" else None)
        return Outcome(result={"state": snap.state})
    _update(db, build["id"], f"AND state NOT IN {_FINAL_SQL}", state=snap.state)
    return again(ctx.poll_seconds)


def stop(db, adapter, build):
    """Stop the provider session of a cancelled build, or every session an uncertain start may
    have made under its tag."""
    try:
        spec = spec_of(db, build) if not build["provider_session_id"] and build["create_token"] else None
    except BuildError:
        spec = None
    try:
        ids = adapter.cancel(dict(build), spec)
        if ids and not build["provider_session_id"]:
            _update(db, build["id"], "AND provider_session_id IS NULL", provider_session_id=ids[0])
            if build["provider"] == "devin":
                _update(db, build["id"], "AND devin_session_id IS NULL", devin_session_id=ids[0])
    except base.ProviderError as exc:
        if exc.transient:
            return again(30, error=str(exc), count=True)
        _update(db, build["id"], note=f"Cancelled here, but the {adapter.label} session could not be stopped: "
                                      f"{adapter.explain(exc)}")
        return Outcome(error=str(exc))
    return Outcome(result={"stopped": ids})


def send(db, adapter, build, message_id):
    row = db.execute("SELECT * FROM build_messages WHERE id = ? AND build_id = ?", (message_id, build["id"])).fetchone()
    if row is None or row["state"] not in ("queued", "sending"):
        return Outcome(result={"skipped": message_id})
    if row["state"] == "sending":
        # An earlier attempt began sending and never concluded (its worker died): it may have
        # arrived. Say so; never send it twice.
        _deliver(db, message_id, "delivery_uncertain",
                 f"The worker stopped while sending this; it may have reached {adapter.label}. It is not sent again.",
                 guard="AND state = 'sending'")
        return Outcome(error="delivery uncertain")
    claimed = db.execute("UPDATE build_messages SET state = 'sending', sent_at = ?, updated_at = ? "
                         "WHERE id = ? AND state = 'queued'", (now(), now(), message_id)).rowcount
    db.commit()
    if claimed != 1:
        return Outcome(result={"skipped": message_id})
    try:
        adapter.reply(dict(build), dict(row))
    except base.ProviderError as exc:
        # A reply that got no clear answer may have arrived: say so, never send it twice.
        state = "delivery_uncertain" if exc.uncertain else "failed"
        error = (f"It may not have reached {adapter.label}: open the session to check before answering again."
                 if exc.uncertain else adapter.explain(exc))
        _deliver(db, message_id, state, error)
        return Outcome(error=str(exc))
    _deliver(db, message_id, "sent", guard="AND state = 'sending'")
    _update(db, build["id"], "AND state = 'waiting_for_answer'", state="running", questions="[]")
    return Outcome(result={"sent": message_id})


def give_up(db, job, error):
    """A job reclaimed MAX_ATTEMPTS times in a row: its worker keeps dying on it."""
    payload = job.get("payload") or {}
    build_id = payload.get("build_id")
    if job["type"] == "build" and build_id:
        _finalise(db, build_id, "failed",
                  error=f"The worker stopped {jobqueue.MAX_ATTEMPTS} times while handling this build ({error}). "
                        "See Setup, Build worker; a provider session may have started, so check before retrying.")
    if job["type"] == "build.message" and payload.get("message_id"):
        _deliver(db, payload["message_id"], "delivery_uncertain", f"The worker kept stopping while sending this ({error}).",
                 guard="AND state IN ('queued', 'sending')")


# --- a worker started from the portal, for development ------------------------------------------------

_dev_worker = None


def start_dev_worker(target):
    """Start `python -m twinlab2.worker` for this catalog, unless the one started here still runs."""
    global _dev_worker
    if _dev_worker is not None and _dev_worker.poll() is None:
        return _dev_worker.pid, False
    folder = database.state_dir(target)
    folder.mkdir(parents=True, exist_ok=True)
    log = folder / "worker.log"
    # Windows: a console without a window, so neither the worker nor the git it runs flashes one
    flags = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
             if sys.platform == "win32" else {"start_new_session": True})
    with open(log, "ab") as out:
        _dev_worker = subprocess.Popen([sys.executable, "-m", "twinlab2.worker", "--database", str(target)],
                                       cwd=paths.ROOT, env=paths.child_env(), stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **flags)
    return _dev_worker.pid, True


def short(sha):
    return (sha or "")[:12]


def label(state):
    return LABELS.get(state, state)


def delivery_label(state):
    return DELIVERY_LABELS.get(state, state)
