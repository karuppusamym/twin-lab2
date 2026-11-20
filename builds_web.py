"""The pages of Build with Devin: Build, Build progress, and their part of Setup.

Roles (auth.py): builders (and owners) of the application open the Build page and start, answer,
retry and cancel builds; only an owner approves a package (on the preview: a build sends only
an owner-frozen revision); portal admins hold the Devin and GitHub credentials, repository
connections and the worker. Build starts, cancels, retries and answers go to the audit history.
"""

from __future__ import annotations

import json

from flask import abort, flash, g, redirect, render_template, request, url_for
from werkzeug.exceptions import HTTPException

import audit
import auth
from auth import require_role
import builds
import devin
import github
import jobqueue


def register(app, db, get_app, pack_view, database, dev_worker=True):
    app.jinja_env.globals.update(build_label=builds.label, build_final=builds.FINAL)

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
                         revision=build["revision"], **details)

    def back_to_build(app_id, error=None):
        if error:
            flash(error, "error")
        return redirect(url_for("build_page", app_id=app_id))

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
        return render_template(
            "build.html", item=item, pack=pack, problem=problem, frozen=frozen, repos=builds.repos(db()),
            current=builds.active(db(), app_id), history=builds.for_app(db(), app_id), previous=previous,
            changes=(builds.pack_changes(builds.stored_archive(db(), app_id, previous["revision"]),
                                         builds.stored_archive(db(), app_id, pack["revision"]))
                     if pack and previous and previous["revision"] != pack["revision"] else []),
            budget=builds.default_budget(db()), devin_ready=_devin_ready())

    def _devin_ready():
        try:
            values = builds.devin_settings(db())
        except devin.DevinUnavailable as exc:
            return str(exc)
        if not values.get("api_key") or not values.get("org_id"):
            return "Devin is not configured: an owner sets the API key and organisation under Setup."
        return None

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
        try:
            build, created = builds.request(db(), app_id, form.get("revision", ""), form.get("repo", ""),
                                            form.get("base_branch", ""), form.get("change_summary", ""),
                                            form.get("acu_budget", builds.default_budget(db())), g.user)
        except (builds.BuildError, ValueError, github.GitHubError) as exc:
            return back_to_build(app_id, str(exc))
        if created:
            record("build.start", build, repo=build["repo"], base_branch=build["base_branch"],
                   pack_sha256=build["pack_sha256"], acu_budget=build["acu_budget"],
                   change_summary=build["change_summary"])
        if not created:
            flash("A build of this application is already in progress: this is it.", "info")
        return redirect(url_for("build_progress", build_id=build["id"]))

    @app.get("/builds/<int:build_id>")
    @require_role("builder", resolve=build_app)
    def build_progress(build_id):
        build = get_build(build_id)
        return render_template("build_progress.html", build=build, item=get_app(build["app_id"]),
                               questions=json.loads(build["questions"] or "[]"),
                               messages=builds.messages(db(), build_id),
                               frozen=builds.is_frozen(db(), build["app_id"], build["revision"]))

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

    @app.post("/builds/<int:build_id>/retry")
    @require_role("builder", resolve=build_app)
    def retry_build(build_id):
        try:
            build, created = builds.retry(db(), build_id, g.user)
        except LookupError:
            abort(404)
        except builds.BuildError as exc:
            flash(str(exc), "error")
            return redirect(url_for("build_progress", build_id=build_id))
        if created:
            record("build.start", build, retry_of=build_id, pack_sha256=build["pack_sha256"])
        if not created:
            flash("A build of this application is already in progress: this is it.", "info")
        return redirect(url_for("build_progress", build_id=build["id"]))

    @app.post("/builds/<int:build_id>/answer")
    @require_role("builder", resolve=build_app)
    def answer_build(build_id):
        try:
            builds.answer(db(), build_id, request.form.get("answer", ""), g.user)
            record("build.answer", get_build(build_id))
        except LookupError:
            abort(404)
        except builds.BuildError as exc:
            flash(str(exc), "error")
        return redirect(url_for("build_progress", build_id=build_id) + "#questions")

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
            stale=jobqueue.later(-2 * jobqueue.LEASE_SECONDS), lease=jobqueue.LEASE_SECONDS, dev_worker=dev_worker, budget=builds.default_budget(db()))

    @app.post("/setup/builds/settings")
    @require_role(auth.ADMIN, resolve=None)
    def save_build_settings():
        form = request.form
        for key in builds.SETTING_KEYS:
            if key in form:
                value = form.get(key, "").strip()
                if key in builds.SECRET_KEYS and not value and not form.get(f"clear_{key}"):
                    continue  # an empty secret box keeps the saved secret
                if key == "DEFAULT_ACU_BUDGET" and value:
                    try:
                        builds._budget(value)
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
