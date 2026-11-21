"""The TwinLab2 portal as one ASGI application (FastAPI).

It serves, from one process and origin:

- `/api/v1/...`: the JSON API of the web app (api/openapi.yaml; routes_portal, routes_registry,
  routes_builds);
- `/api/agent/...`, `/api/uploads...`: the recorder's agent API, unchanged (routes_registry,
  uploads);
- the downloads (packages, releases, reports under /api/v1, and the signed local artifact links
  under /artifacts/<token>);
- the built web app (web/dist): its files, and index.html for every other GET (the app's router
  shows the page, including the sign-in page at /login).

    uvicorn --factory twinlab2.app:asgi            # development (DATABASE_URL or .state/catalog.db)
    gunicorn -k uvicorn.workers.UvicornWorker "twinlab2.app:asgi()"   # the container image

The build worker is a separate process: `python -m twinlab2.worker`.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import secrets

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from twinlab2 import artifacts
from twinlab2 import auth
from twinlab2 import db as database
from twinlab2 import event_ids
from twinlab2 import migrations
from twinlab2 import paths
from twinlab2 import rest
from twinlab2 import scenarios
from twinlab2.rest import Routes, fail

log = logging.getLogger("twinlab2")
MAX_BODY = 16 * 1024 * 1024  # a request body (a recorder upload chunk is 8 MiB)
NOT_THE_APP = ("api/", "artifacts/", "healthz", "assets/")


def web_dist() -> Path:
    """Where the built web app is: TWINLAB2_WEB_DIST, else web/dist of the checkout (or of the
    current directory, as in the container image)."""
    configured = os.environ.get("TWINLAB2_WEB_DIST", "").strip()
    return Path(configured) if configured else paths.ROOT / "web" / "dist"


NO_BUILD = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>TwinLab2</title></head>
<body><main><h1>TwinLab2</h1><p>The web app is not built. Run <code>npm ci &amp;&amp; npm run build</code>
in <code>web/</code>, or <code>npm run dev</code> there and open its address. The JSON API is under
<code>/api/v1/</code>.</p></main></body></html>"""


def web_routes(secret):
    routes = Routes()

    @routes.get("/healthz", auth.public)
    def health():
        rest.db().execute("SELECT 1")
        return {"ok": True}

    @routes.get("/artifacts/{token}", auth.public)  # the signed, short-lived link is the credential
    def artifact_download(token: str):
        try:
            ref, name = artifacts.read_link(token, secret)
        except PermissionError as error:
            fail(403, str(error))
        except LookupError:
            fail(404)
        headers = {"Cache-Control": "no-store", "Content-Length": str(ref.size)}
        if name:
            from twinlab2.routes_portal import disposition

            headers["Content-Disposition"] = disposition(name)
        # streamed in chunks (never the whole object in memory), checked against the ref's SHA-256
        return StreamingResponse(artifacts.stream(ref), media_type=ref.content_type, headers=headers)

    @routes.get("/assets/{name:path}", auth.public)
    def web_asset(name: str):
        root = (web_dist() / "assets").resolve()
        path = (root / name).resolve()
        if root not in path.parents or not path.is_file():
            fail(404)
        return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable"})

    @routes.add(["GET", "POST", "PUT", "PATCH", "DELETE"], "/api/v1/{rest:path}", auth.public)
    def api_not_found(rest: str):
        fail(404, "There is no such API operation.")

    @routes.get("/{path:path}", auth.public)
    def web_app(path: str):
        """index.html for every page of the web app; files at the root of web/dist as they are."""
        if path.startswith(NOT_THE_APP) or path == "api":
            fail(404)
        dist = web_dist()
        if path and "/" not in path and path != "index.html" and (dist / path).is_file():
            return FileResponse(dist / path)
        index = dist / "index.html"
        if not index.is_file():
            return HTMLResponse(NO_BUILD, headers={"Cache-Control": "no-store"})
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return routes


def prepare(target, store):
    """Apply pending migrations and the data steps older catalogs need (once, at start)."""
    token = artifacts.bind(artifacts.Binding(store))
    connection = database.connect(target)
    try:
        migrations.run(connection)
        with connection:  # requirements text written by older portals (or by hand) becomes a scenario
            scenarios.import_requirements(connection)
        event_ids.migrate(connection)  # data: references by number become references by event_id
        if os.environ.get("TWINLAB2_ARTIFACT_MIGRATE", "1") != "0":
            moved = artifacts.migrate_blobs(connection)
            for table, key, reason in moved["failed"]:
                log.warning("blob of %s %s not moved to artifact storage: %s", table, key, reason)
        settings = auth.Settings.from_env()
        from twinlab2.portal import migrate_owners

        with connection:  # data: the owner text of catalogs from before roles becomes an owner role
            local_dev = auth.mode_of(settings, connection) == auth.LOCAL_DEV
            migrate_owners(connection, auth.LOCAL_USER if local_dev else None)
        # refuses unsafe settings (ConfigError) before the portal serves anything
        log.info(auth.check_settings(settings, connection, bool(os.environ.get("TWINLAB2_SECRET_KEY"))))
    finally:
        connection.close()
        artifacts.unbind(token)


def create_app(target, store=None):
    """The portal on the database at `target`: a DATABASE_URL (sqlite:///…, postgresql://…) or a
    SQLite file path. Pending migrations are applied first. `store` is the artifact store
    (default: from the environment)."""
    from twinlab2 import routes_builds, routes_portal, routes_registry, uploads

    target = str(target)
    if database.engine_of(target) == "sqlite":
        database.sqlite_path(target).parent.mkdir(parents=True, exist_ok=True)
    store = store or artifacts.from_env(target)
    prepare(target, store)
    settings = auth.Settings.from_env()
    # local-dev only (prepare refused every other mode without it): a per-process key, so
    # sessions and download links end when the portal restarts
    secret = os.environ.get("TWINLAB2_SECRET_KEY") or secrets.token_hex(32)

    app = FastAPI(title="TwinLab2 portal", version="1.0.0", docs_url=None, redoc_url=None, openapi_url=None)
    # test hooks and settings the routes read (views.config): GITHUB, TWINLAB_RUNNER, DEV_WORKER
    app.config = {"DEV_WORKER": not settings.proxy and os.environ.get("TWINLAB2_DEV_WORKER", "1") != "0",
                  "SECRET_KEY": secret, "DATABASE": target}
    app.state.artifacts = store
    app.state.target = target
    rest.install_errors(app)
    for module in (routes_portal, routes_registry, routes_builds, uploads):
        app.include_router(module.routes.router)
    app.include_router(web_routes(secret).router)  # last: its fallbacks match every other path

    def link(token):
        request = rest.g.get("request")
        base = str(request.base_url).rstrip("/") if request is not None else ""
        return f"{base}/artifacts/{token}"

    def around(scope):
        bound = artifacts.bind(artifacts.Binding(app.state.artifacts, secret, link))
        return lambda: artifacts.unbind(bound)

    hosts = [h.strip() for h in os.environ.get("TWINLAB2_HOSTS", "").split(",") if h.strip()]
    # outermost first: hosts, body size, the session cookie, then the request's state
    app.add_middleware(rest.RequestState, target=target, settings=settings, around=around)
    # the cookie ends with the server-side session (auth.start_session), not with the browser
    app.add_middleware(SessionMiddleware, secret_key=secret, session_cookie="session",
                       max_age=int(settings.session_hours * 3600),
                       same_site="lax", https_only=os.environ.get("TWINLAB2_SECURE_COOKIES") == "1")
    app.add_middleware(rest.BodyLimit, limit=MAX_BODY)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "::1", *hosts])
    return app


def asgi():
    """The portal for an ASGI server: its database at DATABASE_URL (else the SQLite file
    TWINLAB2_DATABASE, else .state/catalog.db)."""
    logging.basicConfig(level=os.environ.get("TWINLAB2_LOG_LEVEL", "INFO"))
    return create_app(database.from_env())
