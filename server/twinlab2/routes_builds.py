"""Build sessions: an application's Build page (new session, session list), a session's chat,
Compare results, and their part of Setup.

Roles (auth.py): builders (and owners) of the application start sessions, chat with them,
approve or deny tools, interrupt, retry and cancel; comparing results runs the release checks,
which is an owner's step (as "Verify this build" is); only an owner approves a package (on the
preview: a session sends only an owner-frozen revision); portal admins hold the credentials,
repository connections and the worker. Starts, cancels, retries, answers, tool decisions and
scope changes go to the audit history.
"""

from __future__ import annotations

import json

from fastapi import Query, Response
from pydantic import BaseModel

from twinlab2 import api_models as m
from twinlab2 import audit
from twinlab2 import auth
from twinlab2 import builds
from twinlab2 import devin
from twinlab2 import github
from twinlab2 import jobqueue
from twinlab2 import packages
from twinlab2 import providers
from twinlab2 import shapes
from twinlab2.auth import require_role
from twinlab2.providers import base
from twinlab2.rest import Problem, Routes, db, fail, g
from twinlab2.views import config, get_app

V1 = "/api/v1"
routes = Routes()


def get_build(build_id):
    build = builds.get(db(), build_id)
    if build is None:
        fail(404, "There is no such build session.")
    return build


def build_app(kwargs):
    return get_build(kwargs["build_id"])["app_id"]


def record(action, build, **details):
    with db():
        audit.record(db(), g.user, action, "build", build["id"], app_id=build["app_id"],
                     revision=build["revision"], provider=build["provider"], **details)


def provider_choices():
    """Every enabled provider: whether it is ready (None, or why not), its budget and model."""
    out = []
    stored = builds.stored_settings(db())
    for name in providers.names():
        ready = None
        try:
            cls = providers.adapter_class(name)
            if name == "devin":
                values = builds.devin_settings(db())
                if not values.get("api_key") or not values.get("org_id"):
                    ready = "Devin is not configured: an admin sets the API key and organisation under Setup."
            elif hasattr(cls, "problem"):
                ready = cls.problem(stored)
        except devin.DevinUnavailable as exc:
            cls, ready = None, str(exc)
        except (ImportError, providers.NotConfigured) as exc:
            cls, ready = None, str(exc)
        out.append({"name": name, "label": providers.LABELS.get(name, name),
                    "capabilities": sorted(cls.capabilities) if cls else [], "ready": ready,
                    "capability_text": ", ".join(base.CAPABILITY_LABELS.get(c, c) for c in sorted(cls.capabilities))
                    if cls else "",
                    "budget_unit": providers.BUDGET_UNITS.get(name, ""), "budget": builds.default_budget(db(), name),
                    "model": (cls.configured_model(stored) if cls and hasattr(cls, "configured_model") else "") or "",
                    "status_label": getattr(cls, "status_label", "") or ""})
    return out


def repo_view(row):
    return {"repo": row["repo"], "default_branch": row["default_branch"], "private": bool(row["private"]),
            "connected_by": row["connected_by"]}


def session_view(row):
    return shapes.build(row, len(builds.outstanding(db(), row["id"])))


@routes.get(V1 + "/apps/{app_id:int}/builds", require_role("builder"), response_model=m.BuildPage)
def build_page(app_id: int):
    from twinlab2.routes_portal import pack_view

    item = get_app(app_id)
    pack, problem = None, None
    try:
        pack = pack_view(app_id)
    except Problem as exc:
        problem = exc.detail
    frozen = builds.is_frozen(db(), app_id, pack["revision"]) if pack else None
    previous = builds.previous_of(db(), app_id)
    history = builds.for_app(db(), app_id)
    return {
        "app": shapes.ref(item), "pack": pack, "problem": problem,
        "frozen": {"frozen_by": frozen["frozen_by"], "frozen_at": frozen["frozen_at"]} if frozen else None,
        "repos": [repo_view(r) for r in builds.repos(db())], "providers": provider_choices(),
        "revisions": [shapes.package_summary(r) for r in packages.revisions(db(), app_id)],
        "sessions": [session_view(b) for b in history],
        "previous": session_view(previous) if previous else None,
        "changes": (builds.pack_changes(builds.stored_archive(db(), app_id, previous["revision"]),
                                        builds.stored_archive(db(), app_id, pack["revision"]))
                    if pack and previous and previous["revision"] != pack["revision"] else []),
        "default_budget": builds.default_budget(db()),
        "starting_points": [session_view(b) for b in history if b["head_sha"]],
    }


def budget_text(value):
    """A budget as the build module reads it (7.0 -> "7")."""
    return str(int(value)) if float(value).is_integer() else str(value)


@routes.post(V1 + "/apps/{app_id:int}/builds", require_role("builder"), response_model=m.BuildStarted,
             status_code=201)
def start_build(app_id: int, data: m.NewBuild):
    get_app(app_id)
    provider = data.provider or "devin"
    start_commit, parent = "", None
    if data.start_from_build:
        source = builds.get(db(), data.start_from_build)
        if source is None or source["app_id"] != app_id or not source["head_sha"]:
            fail(400, "That session has no commit to start from.")
        start_commit, parent = source["head_sha"], source["id"]
    try:
        build, created = builds.request(
            db(), app_id, data.revision, data.repo, data.base_branch or "", data.change_summary or "",
            budget_text(data.budget), g.user, provider=provider, model=data.model or "",
            request_key=data.request_key, start_commit=start_commit, parent_build=parent)
    except (builds.BuildError, ValueError, github.GitHubError) as exc:
        fail(400, str(exc))
    notice = None
    if created:
        record("build.start", build, repo=build["repo"], base_branch=build["base_branch"], base_sha=build["base_sha"],
               pack_sha256=build["pack_sha256"], budget=build["budget"], budget_unit=build["budget_unit"],
               model=build["model"], branch=build["branch"], parent_build=build["parent_build"],
               change_summary=build["change_summary"])
    else:
        notice = (f"That request already started session #{build['id']}: this is it. "
                  "Use New session to start another attempt.")
    return {"build": session_view(build), "created": created, "notice": notice}


def build_detail(build_id, notice=None):
    build = get_build(build_id)
    rows = builds.messages(db(), build_id)
    answered = {msg["reply_to"]: msg for msg in rows if msg["direction"] == "to_provider" and msg["reply_to"]}
    waiting = builds.outstanding(db(), build_id)
    parent = builds.get(db(), build["parent_build"]) if build["parent_build"] else None
    return {
        "build": shapes.build_full(build, len(waiting)), "app": shapes.ref(get_app(build["app_id"])),
        "messages": [shapes.message(msg, answered) for msg in rows],
        "questions": [str(q) for q in json.loads(build["questions"] or "[]")],
        "outstanding": [{"id": msg["id"], "kind": msg["kind"]} for msg in waiting],
        "capabilities": sorted(providers.capabilities(build["provider"])),
        "parent": session_view(parent) if parent else None,
        "frozen": builds.is_frozen(db(), build["app_id"], build["revision"]) is not None,
        "notice": notice,
    }


@routes.get(V1 + "/builds/{build_id:int}", require_role("builder", resolve=build_app), response_model=m.BuildDetail)
def build_progress(build_id: int):
    return build_detail(build_id)


@routes.post(V1 + "/builds/{build_id:int}/cancel", require_role("builder", resolve=build_app),
             response_model=m.BuildDetail)
def cancel_build(build_id: int):
    try:
        builds.cancel(db(), build_id, g.user)
        record("build.cancel", get_build(build_id))
    except LookupError:
        fail(404, "There is no such build session.")
    except builds.BuildError as exc:
        fail(409, str(exc))
    return build_detail(build_id, "Cancel requested.")


@routes.post(V1 + "/builds/{build_id:int}/interrupt", require_role("builder", resolve=build_app),
             response_model=m.BuildDetail)
def interrupt_build(build_id: int):
    try:
        builds.interrupt(db(), build_id, g.user)
        record("build.interrupt", get_build(build_id))
    except LookupError:
        fail(404, "There is no such build session.")
    except builds.BuildError as exc:
        fail(409, str(exc))
    return build_detail(build_id, "Interrupt requested.")


@routes.post(V1 + "/builds/{build_id:int}/retry", require_role("builder", resolve=build_app),
             response_model=m.BuildStarted, status_code=201)
def retry_build(build_id: int, data: m.RequestKey):
    try:
        build, created = builds.retry(db(), build_id, g.user, data.request_key)
    except LookupError:
        fail(404, "There is no such build session.")
    except builds.BuildError as exc:
        fail(409, str(exc))
    notice = None
    if created:
        record("build.start", build, retry_of=build_id, pack_sha256=build["pack_sha256"])
    else:
        notice = f"That request already started session #{build['id']}: this is it."
    return {"build": session_view(build), "created": created, "notice": notice}


SCOPE_NOTICE = ("Not sent: a change to agreed behaviour becomes a proposed scenario. An owner reviews and "
                "approves it as a new package revision; this session's revision is unchanged.")


@routes.post(V1 + "/builds/{build_id:int}/messages", require_role("builder", resolve=build_app),
             response_model=m.AnswerResult, status_code=201)
def answer_build(build_id: int, data: m.AnswerInput):
    kind = "scope_change" if data.scope_change else "clarification"
    if not data.answer.strip():
        fail(400, "Write an answer first.")
    try:
        message, created = builds.answer(db(), build_id, data.answer, g.user, kind=kind,
                                         request_key=data.request_key, reply_to=data.reply_to)
        if created and kind == "clarification":
            record("build.answer", get_build(build_id), message_id=message["id"])
    except LookupError:
        fail(404, "There is no such build session.")
    except builds.BuildError as exc:
        fail(409, str(exc))
    rows = builds.messages(db(), build_id)
    answered = {msg["reply_to"]: msg for msg in rows if msg["direction"] == "to_provider" and msg["reply_to"]}
    return {"message": shapes.message(message, answered), "created": created,
            "notice": SCOPE_NOTICE if kind == "scope_change" else None}


@routes.post(V1 + "/builds/{build_id:int}/tool-decisions/{message_id:int}",
             require_role("builder", resolve=build_app), response_model=m.BuildDetail)
def decide_tool(build_id: int, message_id: int, data: m.ToolDecision):
    try:
        message, created = builds.decide_tool(db(), build_id, message_id, data.decision, g.user, data.request_key)
        if created:
            record("build.tool_decision", get_build(build_id), message_id=message_id, decision=data.decision)
    except LookupError:
        fail(404, "There is no such build session.")
    except builds.BuildError as exc:
        fail(409, str(exc))
    return build_detail(build_id)


# --- Compare results --------------------------------------------------------------------------------


def candidates(app_id, ids):
    rows = [builds.get(db(), i) for i in ids]
    return [r for r in rows if r is not None and r["app_id"] == app_id]


def comparison(build):
    """A candidate's commit and what the release checks said about it."""
    release = (db().execute("SELECT * FROM releases WHERE app_id = ? AND commit_sha = ? ORDER BY id DESC",
                            (build["app_id"], build["head_sha"])).fetchone() if build["head_sha"] else None)
    verification = tests = report = None
    if release is not None:
        row = db().execute("SELECT content FROM release_artifacts WHERE release_id = ? AND kind = 'verification_report' "
                           "ORDER BY id DESC LIMIT 1", (release["id"],)).fetchone()
        report = json.loads(bytes(row["content"]).decode("utf-8")) if row else None
        verification = db().execute("SELECT * FROM release_verifications WHERE release_id = ?",
                                    (release["id"],)).fetchone()
        tests = db().execute("SELECT * FROM release_tests WHERE release_id = ?", (release["id"],)).fetchone()
    checks = (db().execute("SELECT * FROM commit_checks WHERE repository = ? AND commit_sha = ? ORDER BY kind, name",
                           (build["repo"], build["head_sha"])).fetchall() if build["head_sha"] else [])
    return {"build": session_view(build), "release": shapes.release_summary(release) if release else None,
            "checks": [{"name": c["name"], "conclusion": c["conclusion"]} for c in checks],
            "reasons": list((report or {}).get("reasons") or (json.loads(verification["reasons"]) if verification else [])),
            "tested": bool((report or {}).get("tested")),
            "scenarios": [{"name": s["name"], "result": s["result"]} for s in json.loads(tests["results"])] if tests else []}


@routes.get(V1 + "/apps/{app_id:int}/builds/compare", require_role("builder"), response_model=m.Comparison)
def compare_sessions(app_id: int, session: list[int] | None = Query(None)):
    item = get_app(app_id)
    ids = sorted(set(session or []))
    rows = candidates(app_id, ids) or [b for b in builds.for_app(db(), app_id) if b["head_sha"]]
    return {"app": shapes.ref(item), "results": [comparison(b) for b in rows]}


@routes.post(V1 + "/apps/{app_id:int}/builds/compare", require_role("owner"), response_model=m.Comparison)
def run_comparison(app_id: int, data: m.CompareInput):
    """The same acceptance checks (release verification) against each candidate's commit."""
    from twinlab2.routes_portal import verify_session

    item = get_app(app_id)
    rows = candidates(app_id, sorted(set(data.sessions)))
    if not rows:
        fail(400, "Choose sessions with a commit to compare.")
    errors = []
    for build in rows:
        if not build["head_sha"]:
            errors.append({"build_id": build["id"], "detail": f"Session #{build['id']} has no commit yet."})
            continue
        try:
            outcome = verify_session(db(), build)
            with db():
                audit.record(db(), g.user, "release.verify", "release", outcome["release_id"], app_id=app_id,
                             commit=build["head_sha"], build_id=build["id"], tested=outcome.get("tested"),
                             reasons=outcome.get("reasons"), report_sha=outcome.get("report_sha"))
        except ValueError as exc:
            db().rollback()
            errors.append({"build_id": build["id"], "detail": f"Session #{build['id']}: {exc}"})
    return {"app": shapes.ref(item), "results": [comparison(get_build(b["id"])) for b in rows], "errors": errors}


# --- Setup ---------------------------------------------------------------------------------------------


def build_setup_view():
    stored = builds.stored_settings(db())
    try:
        dv, dv_error = builds.devin_settings(db()), None
    except devin.DevinUnavailable as exc:
        dv, dv_error = {"sources": {}}, str(exc)
    sources = dv.get("sources") or {}
    gh = builds.github_settings(db())
    stale = jobqueue.later(-2 * jobqueue.LEASE_SECONDS)

    def worker_state(w):
        if w["stopped_at"]:
            return "stopped"
        return "stale" if (w["seen_at"] or "") < stale else "running"

    return {
        "devin": {"api_key_set": bool(dv.get("api_key")), "api_key_source": sources.get("DEVIN_API_KEY"),
                  "org_id": dv.get("org_id") or None, "org_id_source": sources.get("DEVIN_ORG_ID"),
                  "base_url": dv.get("base_url") or ""},
        "devin_error": dv_error,
        "github": {"token_set": bool(gh.get("token")), "source": gh.get("source"), "api": gh.get("api") or ""},
        "saved": {key: bool(stored.get(key)) for key in builds.SECRET_KEYS},
        "settings": {key: stored.get(key) for key in builds.SETTING_KEYS if key not in builds.SECRET_KEYS},
        "repos": [repo_view(r) for r in builds.repos(db())], "providers": provider_choices(),
        "workers": [{"id": str(w["id"]), "started_at": w["started_at"], "seen_at": w["seen_at"],
                     "current_job": w["current_job"], "stopped_at": w["stopped_at"], "state": worker_state(w)}
                    for w in jobqueue.workers(db())],
        "jobs": [{"id": str(j["id"]), "type": j["type"], "state": j["state"], "attempts": j["attempts"],
                  "due": j["run_after"], "lease_owner": j["lease_owner"], "error": j["error"]}
                 for j in jobqueue.recent(db())],
        "job_counts": jobqueue.counts(db()), "lease_seconds": int(jobqueue.LEASE_SECONDS),
        "dev_worker": bool(config().get("DEV_WORKER")), "budget": builds.default_budget(db()),
        "claude_budget": builds.default_budget(db(), "claude"),
    }


@routes.get(V1 + "/setup/builds", require_role(auth.ADMIN, resolve=None), response_model=m.BuildSetup)
def build_setup():
    return build_setup_view()


@routes.put(V1 + "/setup/builds/settings", require_role(auth.ADMIN, resolve=None), response_model=m.BuildSetup)
def save_build_settings(data: m.BuildSettingsInput):
    values = {key: (value or "").strip() for key, value in data.settings.items()}
    unknown = sorted(set(values) - set(builds.SETTING_KEYS))
    if unknown:
        fail(400, "Unknown setting: " + ", ".join(unknown) + ".")
    for key in ("DEFAULT_ACU_BUDGET", "DEFAULT_CLAUDE_BUDGET_USD"):
        if values.get(key):
            try:
                builds.check_budget("devin" if key == "DEFAULT_ACU_BUDGET" else "claude", values[key])
            except builds.BuildError as exc:
                fail(400, str(exc), errors=[{"field": f"settings.{key}", "message": str(exc)}])
    for key, value in values.items():
        if key in builds.SECRET_KEYS and not value:
            continue  # an empty secret box keeps the saved secret
        builds.save_setting(db(), key, value, g.user)
    for key in data.clear or []:
        builds.save_setting(db(), key, "", g.user)
    return build_setup_view()


@routes.post(V1 + "/setup/builds/check", require_role(auth.ADMIN, resolve=None), response_model=m.ConnectionCheck)
def check_connections():
    results = []
    try:
        with devin.make_client(builds.devin_settings(db()), timeout=15) as client:
            client.list_sessions(first=1)
        results.append({"service": "devin", "ok": True, "message": "Devin: the key and organisation work."})
    except devin.DevinUnavailable as exc:
        results.append({"service": "devin", "ok": False, "message": str(exc)})
    except ValueError as exc:
        results.append({"service": "devin", "ok": False, "message": str(exc)})
    except Exception as exc:  # noqa: BLE001 - the pinned DevinError
        results.append({"service": "devin", "ok": False,
                        "message": devin.explain(exc) if hasattr(exc, "status") else f"Devin: {exc}"})
    try:
        values = builds.github_settings(db())
        if values["token"]:
            for row in builds.repos(db()):
                github.GitHub(values).repository(row["repo"])
            results.append({"service": "github", "ok": True, "message": "GitHub: the token works" + (
                " for every connected repository." if builds.repos(db()) else ".")})
        else:
            results.append({"service": "github", "ok": False,
                            "message": "GitHub is not connected: set TWINLAB2_GITHUB_TOKEN or save a token."})
    except github.GitHubError as exc:
        results.append({"service": "github", "ok": False, "message": str(exc)})
    return {"results": results}


class RepoInput(BaseModel):
    repo: str


@routes.post(V1 + "/setup/builds/repos", require_role(auth.ADMIN, resolve=None), response_model=m.Repo,
             status_code=201)
def connect_repository(data: RepoInput):
    repo = data.repo
    try:
        builds.connect_repo(db(), repo, g.user)
    except (ValueError, github.GitHubError) as exc:
        fail(400, str(exc))
    full = github.normalize_repo(repo)
    row = db().execute("SELECT * FROM github_repos WHERE lower(repo) = lower(?)", (full,)).fetchone()
    return repo_view(row)


@routes.delete(V1 + "/setup/builds/repos", require_role(auth.ADMIN, resolve=None), status_code=204)
def disconnect_repository(repo: str = ""):
    db().execute("DELETE FROM github_repos WHERE repo = ?", (repo,))
    db().commit()
    return Response(status_code=204)


@routes.post(V1 + "/setup/builds/worker", require_role(auth.ADMIN, resolve=None))
def start_worker():
    if not config().get("DEV_WORKER"):
        fail(403, "Run `python -m twinlab2.worker` beside the portal; starting it from here is for development.")
    pid, started = builds.start_dev_worker(g.target)
    return {"pid": pid, "started": started}
