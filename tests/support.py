"""The database a test runs on.

By default every test gets a new SQLite file in its temporary directory. With
`TWINLAB2_TEST_DATABASE_URL=postgresql://user:password@host:port/postgres` every test gets a new,
empty PostgreSQL database on that server instead (created from that URL's database, dropped when
the test run ends), so the whole suite runs on PostgreSQL:

    docker run -d --name twinlab2-pg -e POSTGRES_PASSWORD=twinlab2 -p 55432:5432 postgres:16
    TWINLAB2_TEST_DATABASE_URL=postgresql://postgres:twinlab2@localhost:55432/postgres pytest

Tests of catalogs made by older portals stay on SQLite: those catalogs only exist as SQLite files.
"""

import atexit
import os
from pathlib import Path
import uuid
from urllib.parse import urlsplit, urlunsplit

from twinlab2.db import connect  # noqa: F401 - tests open the database with it

URL = os.environ.get("TWINLAB2_TEST_DATABASE_URL", "").strip()
ENGINE = "postgresql" if URL else "sqlite"
_created = []


def _admin():
    import psycopg
    return psycopg.connect(URL, autocommit=True)


def new_database(directory):
    """A new, empty database for one test: a SQLite file in `directory`, or a PostgreSQL URL."""
    if not URL:
        return Path(directory) / "catalog.db"
    name = "twinlab2_test_" + uuid.uuid4().hex[:16]
    with _admin() as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    _created.append(name)
    parts = urlsplit(URL)
    return urlunsplit((parts.scheme, parts.netloc, "/" + name, parts.query, ""))


@atexit.register
def _drop():
    if not _created:
        return
    import shutil
    from twinlab2.db import state_dir

    parts = urlsplit(URL)
    with _admin() as admin:
        for name in _created:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            # the database's local artifact files (db.state_dir)
            url = urlunsplit((parts.scheme, parts.netloc, "/" + name, parts.query, ""))
            shutil.rmtree(state_dir(url), ignore_errors=True)


# -- the portal under test ----------------------------------------------------------------------


class Client:
    """A test client of the portal (an ASGI app): FastAPI's TestClient on http://localhost from
    127.0.0.1 (so the machine's own user `local` is signed in unless the portal needs a sign-in),
    which sends the session's CSRF token on every /api/v1 request that changes something and
    checks every /api/v1 response against api/openapi.yaml (tests/contract.py).

    `user` is sent as X-Forwarded-User (the OAuth proxy) on every request; `as_user(name)` is
    the same client (the same cookies) as another proxy user. Responses are httpx responses:
    `.status_code`, `.json()`, `.content`, `.text`, `.headers`."""

    def __init__(self, app, user=None, remote="127.0.0.1", _http=None):
        from fastapi.testclient import TestClient

        self.app, self.user = app, user
        self.http = _http or TestClient(app, base_url="http://localhost", client=(remote, 50000),
                                        follow_redirects=False)

    def as_user(self, name):
        return Client(self.app, name, _http=self.http)

    @property
    def token(self):
        """The session's form token (the CSRF token of /api/v1)."""
        token = getattr(self.http, "_twinlab_token", None)
        if not token:
            token = self.http.get("/api/v1/session", headers=self._headers()).json()["csrf_token"]
            self.http._twinlab_token = token
        return token

    def _headers(self, extra=None):
        headers = {"X-Forwarded-User": self.user} if self.user else {}
        headers.update(extra or {})
        return headers

    def request(self, method, path, *, headers=None, csrf=True, **kwargs):
        import contract

        headers = self._headers(headers)
        if csrf and method.upper() not in ("GET", "HEAD", "OPTIONS") and path.startswith("/api/v1/")                 and "X-CSRF-Token" not in headers:
            headers["X-CSRF-Token"] = self.token
        response = self.http.request(method, path, headers=headers, **kwargs)
        if path == "/api/v1/session" and response.status_code == 200 and method.upper() != "DELETE":
            self.http._twinlab_token = response.json()["csrf_token"]
        elif path == "/api/v1/session" and method.upper() == "DELETE" and response.status_code == 204:
            self.http._twinlab_token = None  # sign-out rotates the form token: read the session again
        contract.check(method, path, response.status_code, response.headers.get("content-type", ""), response.content)
        return response

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, **kw):
        return self.request("POST", path, **kw)

    def put(self, path, **kw):
        return self.request("PUT", path, **kw)

    def patch(self, path, **kw):
        return self.request("PATCH", path, **kw)

    def delete(self, path, **kw):
        return self.request("DELETE", path, **kw)


def client(app, user=None, remote="127.0.0.1"):
    return Client(app, user, remote)


class Server:
    """The portal served over HTTP (uvicorn in a thread) on 127.0.0.1, for the recorder's tests:
    `url`, and `stop()` (also on exit of `with`)."""

    def __init__(self, app):
        import socket
        import threading

        import uvicorn

        self.app = app
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        config = uvicorn.Config(app, log_level="warning", lifespan="off", proxy_headers=False)
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [sock]}, daemon=True)
        self.thread.start()
        import time

        for _ in range(500):
            if self.server.started:
                break
            time.sleep(0.01)

    def stop(self):
        self.server.should_exit = True
        self.thread.join(10)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()


# -- shared test steps ------------------------------------------------------------------------------


# Most test fixtures were recorded before screen structure (recorder contract 1.3), so their
# packages miss critical completeness items (pack_validator); tests that are not about
# completeness freeze them over those gaps with this reason. tests/server/test_pack_validator.py
# checks the refusal and the override themselves.
FIXTURE_GAPS = "Test fixture recorded before screen structure (contract 1.3)."


def preview(client, app_id, reason=FIXTURE_GAPS):
    """(the package-draft response, the freeze body's override): a draft that misses critical
    items is previewed again with `reason`, the bytes a freeze with that reason stores."""
    draft = client.get(f"/api/v1/apps/{app_id}/package-draft")
    if draft.status_code != 200 or draft.json()["completeness"]["passed"] or not reason:
        return draft, {}
    again = client.get(f"/api/v1/apps/{app_id}/package-draft", params={"override_reason": reason})
    return again, {"override_reason": reason}


def freeze_draft(db, app_id, user, version_path, reason=FIXTURE_GAPS):
    """Freeze the application's draft through the domain module, over its critical gaps with
    `reason` when it has any; returns the revision number."""
    from twinlab2 import buildpack, packages

    draft = buildpack.draft(db, app_id, version_path)
    if not draft.validation["passed"]:
        draft = buildpack.draft(db, app_id, version_path, known_gaps=reason)
    with db:
        return packages.freeze(db, app_id, user, draft.sha256, "", version_path, override=reason)


def freeze(client, app_id, summary="", override=FIXTURE_GAPS):
    """Preview the draft, approve its SHA-256 and freeze it (a draft identical to the latest
    revision is not frozen again: 409 unchanged), then download the latest builder package.
    A draft with critical completeness gaps is frozen over them with `override` as the reason."""
    draft, extra = preview(client, app_id, override)
    if draft.status_code != 200:
        return draft
    response = client.post(f"/api/v1/apps/{app_id}/packages",
                           json={"approve": True, "sha256": draft.json()["sha256"], "summary": summary, **extra})
    assert response.status_code in (201, 409), response.text
    return client.get(f"/api/v1/apps/{app_id}/build-pack")


def wait_verification(client, response, timeout=60):
    """Verify is a job: the 202 answer's verification, once it ended. The portal works it in a
    thread of the test process (the app's TWINLAB_RUNNER is a fake), joined here."""
    import time

    from twinlab2 import verifications

    assert response.status_code == 202, (response.status_code, response.text[:1500])
    item = response.json()
    end = time.monotonic() + timeout
    while item["state"] in ("queued", "running"):
        assert time.monotonic() < end, item
        time.sleep(0.05)
        got = client.get(f"/api/v1/verifications/{item['id']}")
        assert got.status_code == 200, got.text
        item = got.json()
    verifications.join_in_process()
    return item
