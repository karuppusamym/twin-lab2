"""The pages of build sessions: Build (new session, session list), a session's chat, Compare
results, and their part of Setup.

Roles (auth.py): builders (and owners) of the application start sessions, chat with them,
approve or deny tools, interrupt, retry and cancel; comparing results runs the release checks,
which is an owner's step (as "Verify this build" is); only an owner approves a package (on the
preview: a session sends only an owner-frozen revision); portal admins hold the credentials,
repository connections and the worker. Starts, cancels, retries, answers, tool decisions and
scope changes go to the audit history.
"""

from __future__ import annotations

import json
import uuid

from flask import abort, flash, g, redirect, render_template, request, url_for
from werkzeug.exceptions import HTTPException

from twinlab2 import audit
from twinlab2 import auth
from twinlab2.auth import require_role
from twinlab2 import builds
from twinlab2 import devin
from twinlab2 import github
from twinlab2 import jobqueue
from twinlab2 import packages
from twinlab2 import providers
from twinlab2.providers import base


def new_key():
    return uuid.uuid4().hex


def amount(value):
    """7.0 -> 7, 2.5 -> 2.5, None -> ''."""
    if value is None:
        return ""
    value = float(value)
    return str(int(value)) if value.is_integer() else f"{value:.4g}"


def register(app, db, get_app, pack_view, database, dev_worker=True, verify=None):
    app.jinja_env.filters["amount"] = amount
    app.jinja_env.globals.update(build_label=builds.label, build_final=builds.FINAL,
                                 delivery_label=builds.delivery_label, provider_label=providers.LABELS.get,
                                 new_request_key=new_key, capability_labels=base.CAPABILITY_LABELS)

    def get_build(build_id):
        build = builds.get(db(), build_id)
        if build is None:
            abort(404)
        return build

    def build_app(kwargs):
        return get_build(kwargs["build_id"])["app_id"]

    def record(action, build, **details):
        with db():
            audit.record(db(), g.user, action, "build", build["id"], app_id=build["app_id"],
                         revision=build["revision"], provider=build["provider"], **details)

    def back_to_build(app_id, error=None):
        if error:
            flash(error, "error")
        return redirect(url_for("build_page", app_id=app_id))

    def provider_choices():
        """[{name, label, capabilities, ready (None or why not), budget_unit, budget, model}]"""
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
                        "model": (cls.configured_model(stored) if cls and hasattr(cls, "configured_model") else ""),
                        "status_label": getattr(cls, "status_label", "")})
        return out

    @app.get("/apps/<int:app_id>/build")
    @require_role("builder")
    def build_page(app_id):
        item = get_app(app_id)
        pack, problem = None, None
        try:
            pack = pack_view(app_id)
        except HTTPException as exc:
            problem = exc.description
        frozen = builds.is_frozen(db(), app_id, pack["revision"]) if pack else None
        previous = builds.previous_of(db(), app_id)
        history = builds.for_app(db(), app_id)
        waiting = {b["id"]: len(builds.outstanding(db(), b["id"])) for b in history}
        choices = provider_choices()
        return render_template(
            "build.html", item=item, pack=pack, problem=problem, frozen=frozen, repos=builds.repos(db()),
            current=builds.active(db(), app_id), history=history, previous=previous, waiting=waiting,
            revisions=packages.revisions(db(), app_id), providers=choices,
            starting_points=[b for b in history if b["head_sha"]],
            changes=(builds.pack_changes(builds.stored_archive(db(), app_id, previous["revision"]),
                                         builds.stored_archive(db(), app_id, pack["revision"]))
                     if pack and previous and previous["revision"] != pack["revision"] else []),
            budget=builds.default_budget(db()), devin_ready=next((c["ready"] for c in choices if c["name"] == "devin"), None),
            request_key=new_key(), parent=request.args.get("parent", type=int))

    @app.post("/apps/<int:app_id>/build/freeze")
    @require_role("owner")
    def freeze_pack(app_id):
        """Freezing is the owner's approval of the previewed package: it happens on the preview."""
        get_app(app_id)
        return redirect(url_for("preview_pack", app_id=app_id) + "#approve")

    @app.post("/apps/<int:app_id>/build")
    @require_role("builder")
    def start_build(app_id):
        get_app(app_id)
        form = request.form
        provider = form.get("provider", "devin") or "devin"
        start_commit = form.get("start_commit", "").strip()
        parent = form.get("parent_build", type=int)
        if start_commit.startswith("session:"):
            source = builds.get(db(), int(start_commit.split(":", 1)[1] or 0))
            if source is None or source["app_id"] != app_id or not source["head_sha"]:
                return back_to_build(app_id, "That session has no commit to start from.")
            start_commit, parent = source["head_sha"], source["id"]
        try:
            build, created = builds.request(
                db(), app_id, form.get("revision", ""), form.get("repo", ""), form.get("base_branch", ""),
                form.get("change_summary", ""), form.get("budget") or form.get("acu_budget")
                or builds.default_budget(db(), provider), g.user, provider=provider, model=form.get("model", ""),
                request_key=form.get("request_key", ""), start_commit=start_commit, parent_build=parent)
        except (builds.BuildError, ValueError, github.GitHubError) as exc:
            return back_to_build(app_id, str(exc))
        if created:
            record("build.start", build, repo=build["repo"], base_branch=build["base_branch"], base_sha=build["base_sha"],
                   pack_sha256=build["pack_sha256"], budget=build["budget"], budget_unit=build["budget_unit"],
                   model=build["model"], branch=build["branch"], parent_build=build["parent_build"],
                   change_summary=build["change_summary"])
        else:
            flash(f"That request already started session #{build['id']}: this is it. "
                  "Use New session to start another attempt.", "info")
        return redirect(url_for("build_progress", build_id=build["id"]))

    @app.get("/builds/<int:build_id>")
    @require_role("builder", resolve=build_app)
    def build_progress(build_id):
        build = get_build(build_id)
        rows = builds.messages(db(), build_id)
        answered = {m["reply_to"]: m for m in rows if m["direction"] == "to_provider" and m["reply_to"]}
        return render_template("build_progress.html", build=build, item=get_app(build["app_id"]),
                               questions=json.loads(build["questions"] or "[]"), messages=rows, answered=answered,
                               outstanding=builds.outstanding(db(), build_id),
                               capabilities=providers.capabilities(build["provider"]),
                               parent=builds.get(db(), build["parent_build"]) if build["parent_build"] else None,
                               frozen=builds.is_frozen(db(), build["app_id"], build["revision"]),
                               message_data=lambda m: json.loads(m["data"] or "{}"), request_key=new_key())

    @app.post("/builds/<int:build_id>/cancel")
    @require_role("builder", resolve=build_app)
    def cancel_build(build_id):
        try:
            builds.cancel(db(), build_id, g.user)
            record("build.cancel", get_build(build_id))
        except LookupError:
            abort(404)
        except builds.BuildError as exc:
            flash(str(exc), "error")
        return redirect(url_for("build_progress", build_id=build_id))

    @app.post("/builds/<int:build_id>/interrupt")
    @require_role("builder", resolve=build_app)
    def interrupt_build(build_id):
        try:
            builds.interrupt(db(), build_id, g.user)
            record("build.interrupt", get_build(build_id))
            flash("Interrupt requested.", "info")
        except LookupError:
            abort(404)
        except builds.BuildError as exc:
            flash(str(exc), "error")
        return redirect(url_for("build_progress", build_id=build_id))

    @app.post("/builds/<int:build_id>/retry")
    @require_role("builder", resolve=build_app)
    def retry_build(build_id):
        try:
            build, created = builds.retry(db(), build_id, g.user, request.form.get("request_key"))
        except LookupError:
            abort(404)
        except builds.BuildError as exc:
            flash(str(exc), "error")
            return redirect(url_for("build_progress", build_id=build_id))
        if created:
            record("build.start", build, retry_of=build_id, pack_sha256=build["pack_sha256"])
        else:
            flash(f"That request already started session #{build['id']}: this is it.", "info")
        return redirect(url_for("build_progress", build_id=build["id"]))

    @app.post("/builds/<int:build_id>/answer")
    @require_role("builder", resolve=build_app)
    def answer_build(build_id):
        form = request.form
        kind = "scope_change" if form.get("scope_change") or form.get("kind") == "scope_change" else "clarification"
        try:
            message, created = builds.answer(db(), build_id, form.get("answer", ""), g.user, kind=kind,
                                             request_key=form.get("request_key"), reply_to=form.get("reply_to"))
            if created and kind == "clarification":
                record("build.answer", get_build(build_id), message_id=message["id"])
            if kind == "scope_change":
                flash("Not sent: a change to agreed behaviour becomes a proposed scenario. An owner reviews and "
                      "approves it as a new package revision; this session's revision is unchanged.", "info")
        except LookupError:
            abort(404)
        except builds.BuildError as exc:
            flash(str(exc), "error")
        return redirect(url_for("build_progress", build_id=build_id) + "#chat")

    @app.post("/builds/<int:build_id>/tools/<int:message_id>")
    @require_role("builder", resolve=build_app)
    def decide_tool(build_id, message_id):
        try:
            message, created = builds.decide_tool(db(), build_id, message_id, request.form.get("decision", ""), g.user,
                                                  request.form.get("request_key"))
            if created:
                record("build.tool_decision", get_build(build_id), message_id=message_id,
                       decision=request.form.get("decision"))
        except LookupError:
            abort(404)
        except builds.BuildError as exc:
            flash(str(exc), "error")
        return redirect(url_for("build_progress", build_id=build_id) + "#chat")

    # --- Compare results ------------------------------------------------------------------------

    def candidates(app_id, ids):
        rows = [builds.get(db(), i) for i in ids]
        return [r for r in rows if r is not None and r["app_id"] == app_id]

    def selected_ids():
        values = request.values.getlist("session") or (request.values.get("ids", "").split(","))
        return sorted({int(v) for v in values if str(v).strip().isdigit()})

    @app.get("/apps/<int:app_id>/sessions/compare")
    @require_role("builder")
    def compare_sessions(app_id):
        item = get_app(app_id)
        rows = candidates(app_id, selected_ids()) or [b for b in builds.for_app(db(), app_id) if b["head_sha"]]
        results = [comparison(b) for b in rows]
        return render_template("compare.html", item=item, results=results, ids=",".join(str(b["id"]) for b in rows))

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
        return {"build": build, "release": release, "verification": verification, "checks": checks,
                "reasons": (report or {}).get("reasons") or (json.loads(verification["reasons"]) if verification else []),
                "tested": bool((report or {}).get("tested")), "report": report,
                "scenarios": json.loads(tests["results"]) if tests else []}

    @app.post("/apps/<int:app_id>/sessions/compare")
    @require_role("owner")
    def run_comparison(app_id):
        """The same acceptance checks (release verification) against each candidate's commit."""
        get_app(app_id)
        rows = candidates(app_id, selected_ids())
        if not rows:
            abort(400, "Choose sessions with a commit to compare.")
        for build in rows:
            if not build["head_sha"]:
                flash(f"Session #{build['id']} has no commit yet.", "error")
                continue
            try:
                outcome = verify(db(), build)
                with db():
                    audit.record(db(), g.user, "release.verify", "release", outcome["release_id"], app_id=app_id,
                                 commit=build["head_sha"], build_id=build["id"], tested=outcome.get("tested"),
                                 reasons=outcome.get("reasons"), report_sha=outcome.get("report_sha"))
            except ValueError as exc:
                db().rollback()
                flash(f"Session #{build['id']}: {exc}", "error")
        return redirect(url_for("compare_sessions", app_id=app_id, ids=",".join(str(b["id"]) for b in rows)))

    # --- Setup ---------------------------------------------------------------------------------

    @app.get("/setup/builds")
    @require_role(auth.ADMIN, resolve=None)
    def build_setup():
        stored = builds.stored_settings(db())
        try:
            dv, dv_error = builds.devin_settings(db()), None
        except devin.DevinUnavailable as exc:
            dv, dv_error = {"sources": {}}, str(exc)
        gh = builds.github_settings(db())
        return render_template(
            "setup_builds.html", devin=dv, devin_error=dv_error, gh=gh, stored=stored, repos=builds.repos(db()),
            counts=jobqueue.counts(db()), jobs=jobqueue.recent(db()), workers=jobqueue.workers(db()),
            stale=jobqueue.later(-2 * jobqueue.LEASE_SECONDS), lease=jobqueue.LEASE_SECONDS, dev_worker=dev_worker,
            budget=builds.default_budget(db()), providers=provider_choices(),
            claude_budget=builds.default_budget(db(), "claude"))

    @app.post("/setup/builds/settings")
    @require_role(auth.ADMIN, resolve=None)
    def save_build_settings():
        form = request.form
        for key in builds.SETTING_KEYS:
            if key in form:
                value = form.get(key, "").strip()
                if key in builds.SECRET_KEYS and not value and not form.get(f"clear_{key}"):
                    continue  # an empty secret box keeps the saved secret
                if key in ("DEFAULT_ACU_BUDGET", "DEFAULT_CLAUDE_BUDGET_USD") and value:
                    try:
                        builds.check_budget("devin" if key == "DEFAULT_ACU_BUDGET" else "claude", value)
                    except builds.BuildError as exc:
                        flash(str(exc), "error")
                        continue
                builds.save_setting(db(), key, value, g.user)
        flash("Saved.", "info")
        return redirect(url_for("build_setup"))

    @app.post("/setup/builds/check")
    @require_role(auth.ADMIN, resolve=None)
    def check_connections():
        try:
            with devin.make_client(builds.devin_settings(db()), timeout=15) as client:
                client.list_sessions(first=1)
            flash("Devin: the key and organisation work.", "info")
        except devin.DevinUnavailable as exc:
            flash(str(exc), "error")
        except ValueError as exc:
            flash(str(exc), "error")
        except Exception as exc:  # the pinned DevinError
            flash(devin.explain(exc) if hasattr(exc, "status") else f"Devin: {exc}", "error")
        try:
            values = builds.github_settings(db())
            if values["token"]:
                for row in builds.repos(db()):
                    github.GitHub(values).repository(row["repo"])
                flash("GitHub: the token works" + (" for every connected repository." if builds.repos(db()) else "."), "info")
            else:
                flash("GitHub is not connected: set TWINLAB2_GITHUB_TOKEN or save a token.", "error")
        except github.GitHubError as exc:
            flash(str(exc), "error")
        return redirect(url_for("build_setup"))

    @app.post("/setup/builds/repos")
    @require_role(auth.ADMIN, resolve=None)
    def connect_repository():
        try:
            info = builds.connect_repo(db(), request.form.get("repo", ""), g.user)
            flash(f"Connected {info['full_name']} (default branch {info['default_branch']}).", "info")
        except (ValueError, github.GitHubError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("build_setup") + "#repos")

    @app.post("/setup/builds/repos/remove")
    @require_role(auth.ADMIN, resolve=None)
    def disconnect_repository():
        db().execute("DELETE FROM github_repos WHERE repo = ?", (request.form.get("repo", ""),))
        db().commit()
        return redirect(url_for("build_setup") + "#repos")

    @app.post("/setup/builds/worker")
    @require_role(auth.ADMIN, resolve=None)
    def start_worker():
        if not dev_worker:
            abort(403, "Run `python worker.py` beside the portal; starting it from here is for development.")
        pid, started = builds.start_dev_worker(database)
        flash(f"Worker {'started' if started else 'already running'} (process {pid}); its log is worker.log "
              "beside the catalog.", "info")
        return redirect(url_for("build_setup") + "#worker")
