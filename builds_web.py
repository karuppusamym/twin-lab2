"""The pages of Build with Devin: Build, Build progress, and their part of Setup.

Roles: auth.py (`require_role`) is not on main yet, so each route carries a `# ROLE:` comment
for the roles work to turn into a decorator: builders start, answer, retry and cancel builds;
owners hold credentials, repository connections and the worker.
"""

from __future__ import annotations

import json

from flask import abort, flash, g, redirect, render_template, request, url_for
from werkzeug.exceptions import HTTPException

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

    def back_to_build(app_id, error=None):
        if error:
            flash(error, "error")
        return redirect(url_for("build_page", app_id=app_id))

    @app.get("/apps/<int:app_id>/build")
    def build_page(app_id):  # ROLE: builder
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
    def freeze_pack(app_id):  # ROLE: builder
        get_app(app_id)
        try:
            builds.freeze(db(), app_id, request.form.get("revision", ""), g.user)
        except builds.BuildError as exc:
            return back_to_build(app_id, str(exc))
        return redirect(url_for("build_page", app_id=app_id) + "#start")

    @app.post("/apps/<int:app_id>/build")
    def start_build(app_id):  # ROLE: builder
        get_app(app_id)
        form = request.form
        try:
            build, created = builds.request(db(), app_id, form.get("revision", ""), form.get("repo", ""),
                                            form.get("base_branch", ""), form.get("change_summary", ""),
                                            form.get("acu_budget", builds.default_budget(db())), g.user)
        except (builds.BuildError, ValueError, github.GitHubError) as exc:
            return back_to_build(app_id, str(exc))
        if not created:
            flash("A build of this application is already in progress: this is it.", "info")
        return redirect(url_for("build_progress", build_id=build["id"]))

    @app.get("/builds/<int:build_id>")
    def build_progress(build_id):  # ROLE: builder
        build = get_build(build_id)
        return render_template("build_progress.html", build=build, item=get_app(build["app_id"]),
                               questions=json.loads(build["questions"] or "[]"),
                               messages=builds.messages(db(), build_id),
                               frozen=builds.is_frozen(db(), build["app_id"], build["revision"]))

    @app.post("/builds/<int:build_id>/cancel")
    def cancel_build(build_id):  # ROLE: builder
        try:
            builds.cancel(db(), build_id, g.user)
        except LookupError:
            abort(404)
        except builds.BuildError as exc:
            flash(str(exc), "error")
        return redirect(url_for("build_progress", build_id=build_id))

    @app.post("/builds/<int:build_id>/retry")
    def retry_build(build_id):  # ROLE: builder
        try:
            build, created = builds.retry(db(), build_id, g.user)
        except LookupError:
            abort(404)
        except builds.BuildError as exc:
            flash(str(exc), "error")
            return redirect(url_for("build_progress", build_id=build_id))
        if not created:
            flash("A build of this application is already in progress: this is it.", "info")
        return redirect(url_for("build_progress", build_id=build["id"]))

    @app.post("/builds/<int:build_id>/answer")
    def answer_build(build_id):  # ROLE: builder
        try:
            builds.answer(db(), build_id, request.form.get("answer", ""), g.user)
        except LookupError:
            abort(404)
        except builds.BuildError as exc:
            flash(str(exc), "error")
        return redirect(url_for("build_progress", build_id=build_id) + "#questions")

    # --- Setup ---------------------------------------------------------------------------------

    @app.get("/setup/builds")
    def build_setup():  # ROLE: owner
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
    def save_build_settings():  # ROLE: owner
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
    def check_connections():  # ROLE: owner
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
    def connect_repository():  # ROLE: owner
        try:
            info = builds.connect_repo(db(), request.form.get("repo", ""), g.user)
            flash(f"Connected {info['full_name']} (default branch {info['default_branch']}).", "info")
        except (ValueError, github.GitHubError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("build_setup") + "#repos")

    @app.post("/setup/builds/repos/remove")
    def disconnect_repository():  # ROLE: owner
        db().execute("DELETE FROM github_repos WHERE repo = ?", (request.form.get("repo", ""),))
        db().commit()
        return redirect(url_for("build_setup") + "#repos")

    @app.post("/setup/builds/worker")
    def start_worker():  # ROLE: owner
        if not dev_worker:
            abort(403, "Run `python worker.py` beside the portal; starting it from here is for development.")
        pid, started = builds.start_dev_worker(database)
        flash(f"Worker {'started' if started else 'already running'} (process {pid}); its log is worker.log "
              "beside the catalog.", "info")
        return redirect(url_for("build_setup") + "#worker")
