"""The portal's request layer on ASGI (FastAPI): the request's state, errors, policies, CSRF.

- State: every request gets a `State` (its database connection, opened on first use and closed
  when the response is sent, the signed-in user, and the request). `g` and `db()` read the
  current request's state from any thread the server runs the view in.
- Errors: `fail(status, detail, code)` raises a `Problem`. Under /api/v1 every error is
  `application/problem+json` (RFC 9457): `type` (`/errors/<code>`, a stable URI reference per
  code), `title`, `status`, `detail` (the sentence to show), `code` (one of CODES, listed in
  api/openapi.yaml as ErrorCode; a status without a specific code gets its default) and
  `request_id`; a request body that is not in the contract's shape is 400 with per-field
  `errors`. Outside /api/v1 (the recorder's agent API) an error is `{"error": detail}`, as the
  recorder has always read it.
- Request ids: every response has `Request-Id` (the caller's own `Request-Id`/`X-Request-Id`
  when it is a short token, else a new one), and every request is logged with it (RequestLog).
- Policies (auth.py decides): every route is added with one (`Routes.get(path, policy)`), and a
  route without one answers 403: a route added later without a decision is closed, not open.
- CSRF: every /api/v1 request other than GET, HEAD and OPTIONS carries `X-CSRF-Token`, the
  session's form token (`GET /api/v1/session` hands it out); otherwise 403 `csrf`.
"""

from __future__ import annotations

from contextvars import ContextVar
import hashlib
import hmac
from http import HTTPStatus
import logging
import re
import secrets
import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException as StarletteHTTPException

from twinlab2 import db as database

PREFIX = "/api/v1/"
SAFE = {"GET", "HEAD", "OPTIONS"}
log = logging.getLogger("twinlab2.http")

# -- request ids --------------------------------------------------------------------------------

REQUEST_ID = "Request-Id"
_request_id: ContextVar[str] = ContextVar("twinlab2_request_id", default="-")
_SAFE_ID = re.compile(r"[A-Za-z0-9._:-]{8,128}")
QUIET = {"/livez", "/readyz", "/healthz"}  # probes, every few seconds: not logged


def request_id() -> str:
    """The current request's id ("-" outside a request)."""
    return _request_id.get()


class RequestIdFilter(logging.Filter):
    """Adds `request_id` to every log record (app.asgi's log format prints it)."""

    def filter(self, record):
        if not hasattr(record, "request_id"):
            record.request_id = _request_id.get()
        return True


class RequestLog:
    """ASGI middleware (outermost): the request's id, the `Request-Id` response header, and one
    log line per request (method, path, status, milliseconds). A caller's own id is kept when it
    is a short token (an upstream proxy's), so one id follows the request through the logs."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        sent = ""
        for name, value in scope.get("headers", []):
            if name in (b"request-id", b"x-request-id"):
                sent = value.decode("latin-1").strip()
                break
        rid = sent if _SAFE_ID.fullmatch(sent) else secrets.token_hex(8)
        token = _request_id.set(rid)
        started, status = time.monotonic(), 0

        async def stamped(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"request-id"]
                message = dict(message, headers=[*headers, (b"request-id", rid.encode())])
            await send(message)

        try:
            await self.app(scope, receive, stamped)
        except Exception:
            # answered here, not by Starlette's outermost error handler: the 500 keeps its id
            log.exception("unhandled error on %s %s", scope.get("method"), scope.get("path", ""))
            if status:  # the response had started: it just ends
                raise
            await error_response(scope.get("path", ""), 500,
                                 f"The portal failed to answer: the error is in its log (request {rid}).")(
                scope, receive, stamped)
        finally:
            path = scope.get("path", "")
            if path not in QUIET:
                log.info("%s %s %s %.0fms", scope.get("method"), path, status or "-",
                         (time.monotonic() - started) * 1000, extra={"request_id": rid})
            _request_id.reset(token)


# -- the request's state ------------------------------------------------------------------------


class State:
    """What a request carries: its connection (lazy), who is signed in, the Request."""

    def __init__(self, target, settings=None):
        self.target = target
        self.settings = settings
        self.request: Request | None = None
        self.user: str | None = None
        self.is_admin = False
        self.response_headers: dict[str, str] = {}  # added to the response (RequestState)
        self._db = None

    @property
    def db(self):
        if self._db is None:
            self._db = database.connect(self.target)
        return self._db

    def close(self):
        connection, self._db = self._db, None
        if connection is not None:
            try:
                connection.close()
            except Exception:  # noqa: BLE001 - closing never fails a response
                log.exception("closing the request's database connection")

    @property
    def session(self) -> dict:
        return self.request.session if self.request is not None else {}

    def get(self, name, default=None):
        return getattr(self, name, default)


_state: ContextVar[State | None] = ContextVar("twinlab2_request", default=None)


def current() -> State:
    state = _state.get()
    if state is None:
        raise RuntimeError("not in a portal request")
    return state


class _G:
    """The current request's state (`g.user`, `g.is_admin`, `g.request`, `g.session`)."""

    def __getattr__(self, name):
        return getattr(current(), name)

    def __setattr__(self, name, value):
        setattr(current(), name, value)

    def get(self, name, default=None):
        state = _state.get()
        return default if state is None else getattr(state, name, default)


g = _G()


def db():
    """The current request's database connection."""
    return current().db


class RequestState:
    """ASGI middleware: a State per HTTP request, closed once the response is sent."""

    def __init__(self, app, target, settings=None, around=None):
        self.app, self.target, self.settings, self.around = app, target, settings, around

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        state = State(self.target, self.settings)
        token = _state.set(state)
        extra = self.around(scope) if self.around else None

        async def with_headers(message):
            if message["type"] == "http.response.start" and state.response_headers:
                added = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in state.response_headers.items()]
                message = dict(message, headers=[*message.get("headers", []), *added])
            await send(message)

        try:
            await self.app(scope, receive, with_headers)
        finally:
            if extra is not None:
                extra()
            state.close()
            _state.reset(token)


# the polling endpoints of the build chat: a GET answers 304 when nothing changed
ETAG_PATHS = (re.compile(r"^/api/v1/builds/\d+$"), re.compile(r"^/api/v1/apps/\d+/builds$"))


def etag_of(body: bytes) -> str:
    return '"' + hashlib.sha256(body).hexdigest()[:32] + '"'


def matches(if_none_match: str, etag: str) -> bool:
    """Whether an If-None-Match header names `etag` (weak comparison, as RFC 9110 asks)."""
    tags = [t.strip().removeprefix("W/") for t in if_none_match.split(",")]
    return "*" in tags or etag in tags


class ETags:
    """ASGI middleware: `ETag` on the 200s of ETAG_PATHS (a hash of the body), and 304 without a
    body when the request's `If-None-Match` names it. Polling clients send back the last ETag
    and skip the body (and the re-render) while the build chat is unchanged."""

    def __init__(self, app, paths=ETAG_PATHS):
        self.app, self.paths = app, paths

    async def __call__(self, scope, receive, send):
        if (scope["type"] != "http" or scope.get("method") not in ("GET", "HEAD")
                or not any(p.match(scope.get("path", "")) for p in self.paths)):
            return await self.app(scope, receive, send)
        start, chunks = None, []

        async def buffered(message):
            nonlocal start
            if message["type"] == "http.response.start":
                start = message
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))
                if not message.get("more_body"):
                    await finish()
            else:
                await send(message)

        async def finish():
            body = b"".join(chunks)
            if start["status"] != 200:
                await send(start)
                await send({"type": "http.response.body", "body": body})
                return
            etag = etag_of(body)
            headers = [(k, v) for k, v in start.get("headers", []) if k.lower() not in (b"etag", b"cache-control")]
            headers += [(b"etag", etag.encode()), (b"cache-control", b"no-cache")]
            sent = next((v.decode("latin-1") for k, v in scope.get("headers", []) if k == b"if-none-match"), "")
            if sent and matches(sent, etag):
                keep = [(k, v) for k, v in headers if k.lower() not in (b"content-length", b"content-type")]
                await send({"type": "http.response.start", "status": 304, "headers": keep})
                await send({"type": "http.response.body", "body": b""})
                return
            await send(dict(start, headers=headers))
            await send({"type": "http.response.body", "body": body})

        await self.app(scope, receive, buffered)


TOO_LARGE = "The request is too large."


class BodyTooLarge(StarletteHTTPException):
    """Raised from `receive` once a request body passes the limit. An HTTPException, so FastAPI
    passes it on as it is (it turns other errors while reading a body into 400)."""

    def __init__(self):
        super().__init__(413, TOO_LARGE)


class BodyLimit:
    """ASGI middleware: 413 for a request body over `limit` bytes. A Content-Length over the
    limit is refused before the app runs; otherwise the bytes actually received are counted
    (a chunked body, or one whose Content-Length understates it), and the first chunk past the
    limit ends the request with 413: the app never gets more than `limit` bytes."""

    def __init__(self, app, limit):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        for name, value in scope.get("headers", []):
            if name == b"content-length" and (not value.isdigit() or int(value) > self.limit):
                status, detail = (413, TOO_LARGE) if value.isdigit() else (400, "The Content-Length is not a number.")
                return await error_response(path, status, detail)(scope, receive, send)
        received, started = 0, False

        async def counted():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.limit:
                    raise BodyTooLarge()
            return message

        async def watched(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counted, watched)
        except BodyTooLarge:
            if started:  # the app already answered: the request just ends here
                raise
            log.warning("request body over %d bytes refused: %s %s", self.limit, scope.get("method"), path)
            await error_response(path, 413, TOO_LARGE)(scope, receive, send)


# -- errors -------------------------------------------------------------------------------------


# Every `code` an /api/v1 error may carry: (its usual status, what it means). api/openapi.yaml
# lists the same codes (ErrorCode); tests hold the two and the server's fail() calls together.
CODES: dict[str, tuple[int, str]] = {
    "invalid_request": (400, "The request (its body, query or headers) is not valid; `errors` names the fields."),
    "idempotency_key_mismatch": (400, "`Idempotency-Key` and the body's `request_key` differ: send one key."),
    "signed_out": (401, "Nobody is signed in, or the session ended: sign in again."),
    "wrong_credentials": (401, "The name or password is wrong."),
    "forbidden": (403, "The caller may not do this."),
    "csrf": (403, "The form token (X-CSRF-Token) is missing or out of date: read the session again."),
    "role": (403, "The caller lacks the role this needs; `owners` names who can grant it, where the caller may see them."),
    "restricted": (403, "Restricted evidence: only who added it, its application's owners or an admin may see it."),
    "not_found": (404, "There is no such resource, page or API operation."),
    "method_not_allowed": (405, "The resource does not take this method."),
    "conflict": (409, "A stale conditional update: someone else changed the resource; `current` is how it is now."),
    "state_conflict": (409, "The action does not fit the resource's current state."),
    "draft_changed": (409, "The package draft changed since it was previewed."),
    "unchanged": (409, "Nothing changed since the last package revision."),
    "incomplete_package": (409, "The package is missing what a build needs."),
    "plan_changed": (409, "The applications changed since the merge preview."),
    "publish_locked": (409, "The release cannot be published in its current state."),
    "idempotency_in_progress": (409, "A request with this `Idempotency-Key` is still being processed: retry later."),
    "too_large": (413, "The request body is over the portal's limit."),
    "unsupported_media_type": (415, "The request body's content type is not one this operation takes."),
    "idempotency_key_reused": (422, "This `Idempotency-Key` was used for a different request."),
    "internal": (500, "The portal failed; the error is in its log under the response's Request-Id."),
    "bad_gateway": (502, "A service the portal depends on failed."),
    "unavailable": (503, "The portal is not ready (starting, migrating, or a dependency is down): retry later."),
    "error": (0, "Any other HTTP error status."),
}
DEFAULT_CODE = {400: "invalid_request", 401: "signed_out", 403: "forbidden", 404: "not_found",
                405: "method_not_allowed", 409: "state_conflict", 413: "too_large", 415: "unsupported_media_type",
                422: "idempotency_key_reused", 500: "internal", 502: "bad_gateway", 503: "unavailable"}
ERRORS_BASE = "/errors/"  # `type` is ERRORS_BASE + code: a URI reference relative to the portal


def code_for(status, code=None) -> str:
    """The problem's code: `code` as given, else the status's default."""
    if code and code not in CODES:
        log.warning("error code %r is not in rest.CODES (api/openapi.yaml ErrorCode)", code)
    return code or DEFAULT_CODE.get(status, "error")


def type_uri(code) -> str:
    return ERRORS_BASE + code


class Problem(Exception):
    def __init__(self, status, detail=None, code=None, errors=None, headers=None, body=None, extra=None):
        super().__init__(detail)
        self.status, self.detail, self.code = status, detail, code
        self.errors, self.headers, self.body = errors, headers or {}, body
        self.extra = extra or {}


def fail(status, detail=None, code=None, errors=None, headers=None, extra=None):
    """Raise the HTTP error `status`: `detail` is the sentence to show, `code` the problem code,
    `extra` more members of the problem (in the spec's Problem)."""
    raise Problem(status, detail, code, errors, headers, extra=extra)


def is_api(path):
    return path.startswith(PREFIX)


def title(status):
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Error"


def default_detail(status):
    try:
        return HTTPStatus(status).description or title(status)
    except ValueError:
        return "The request failed."


def error_response(path, status, detail=None, code=None, errors=None, headers=None, body=None, extra=None):
    if body is not None:  # a view's own error body (the agent API's {"error", ...} shapes)
        if is_api(path) and isinstance(body, dict) and "status" in body:  # a whole problem (revisions.conflict)
            body = dict(body, code=code_for(status, body.get("code") or code))
            body.update(type=type_uri(body["code"]), request_id=request_id())
        return JSONResponse(body, status_code=status, headers=headers)
    if not is_api(path):
        return JSONResponse({"error": detail or default_detail(status)}, status_code=status, headers=headers)
    code = code_for(status, code)
    out: dict[str, Any] = {"type": type_uri(code), "title": title(status), "status": status,
                           "detail": detail or default_detail(status), "instance": path, "code": code,
                           "request_id": request_id()}
    if errors:
        out["errors"] = errors
    out.update(extra or {})
    return JSONResponse(out, status_code=status, headers=headers, media_type="application/problem+json")


def install_errors(app):
    @app.exception_handler(Problem)
    async def problem(request, error: Problem):
        return error_response(request.url.path, error.status, error.detail, error.code, error.errors,
                              error.headers, error.body, error.extra)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, error: StarletteHTTPException):
        detail = error.detail if isinstance(error.detail, str) and error.detail != title(error.status_code) else None
        if error.status_code == 404 and detail is None:
            detail = "There is no such page or API operation."
        return error_response(request.url.path, error.status_code, detail, headers=getattr(error, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def invalid(request, error: RequestValidationError):
        errors = []
        for item in error.errors():
            where = [str(p) for p in item.get("loc", ()) if p not in ("body", "query", "path")]
            errors.append({"field": ".".join(where) or "body", "message": item.get("msg", "invalid")})
        first = errors[0] if errors else {"field": "body", "message": "invalid"}
        return error_response(request.url.path, 400, f"Check “{first['field']}”: {first['message']}.", errors=errors)

    @app.exception_handler(Exception)
    async def crash(request, error: Exception):
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return error_response(request.url.path, 500,
                              f"The portal failed to answer: the error is in its log (request {request_id()}).")


# -- policies -----------------------------------------------------------------------------------


def form_token(state=None) -> str:
    """The session's form token (created on first use): the CSRF token of /api/v1."""
    session = (state or current()).session
    if "form_token" not in session:
        session["form_token"] = secrets.token_hex(32)
    return session["form_token"]


def check_csrf(request: Request):
    if request.method in SAFE or not is_api(request.url.path):
        return
    sent = request.headers.get("X-CSRF-Token", "")
    token = request.session.get("form_token", "")
    if not sent or not token or not hmac.compare_digest(sent, token):
        fail(403, "The form token is missing or out of date (you signed in or out in another tab?): read the session again and retry.", "csrf")


def deny(request: Request):
    fail(403, "This page has no access policy.", "role")


class PolicyRoute(APIRoute):
    """A route whose view has no policy is closed (403) before anything else runs."""

    def __init__(self, path, endpoint, *, dependencies=None, **kwargs):
        if getattr(endpoint, "twinlab_policy", None) is None:
            dependencies = [Depends(deny), *(dependencies or [])]
        super().__init__(path, endpoint, dependencies=dependencies, **kwargs)


class Routes:
    """An APIRouter whose every route names its policy (auth.py): `@routes.get(path, policy)`."""

    def __init__(self):
        self.router = APIRouter(route_class=PolicyRoute)

    def add(self, methods, path, policy, **kwargs):
        if policy is None:
            raise ValueError(f"{path}: every route needs a policy (auth.public, auth.signed_in, auth.require_role, ...)")

        idempotent = kwargs.pop("idempotent", None)

        def decorate(view):
            view.twinlab_policy = policy.mark
            kwargs.setdefault("response_model_exclude_unset", True)
            if "response_model" not in kwargs:
                kwargs["response_model"] = None
            endpoint = view
            if idempotent:  # Idempotency-Key (idempotency.py): "stored" or "native"
                from twinlab2 import idempotency

                endpoint = idempotency.wrap(view, idempotent, kwargs.get("status_code", 200), kwargs["response_model"])
                endpoint.twinlab_policy = policy.mark
                endpoint.twinlab_idempotent = idempotent
            self.router.add_api_route(path, endpoint, methods=methods,
                                      dependencies=[Depends(policy.dependency), *kwargs.pop("dependencies", [])],
                                      **kwargs)
            return view

        return decorate

    def get(self, path, policy, **kw):
        return self.add(["GET"], path, policy, **kw)

    def post(self, path, policy, **kw):
        return self.add(["POST"], path, policy, **kw)

    def put(self, path, policy, **kw):
        return self.add(["PUT"], path, policy, **kw)

    def patch(self, path, policy, **kw):
        return self.add(["PATCH"], path, policy, **kw)

    def delete(self, path, policy, **kw):
        return self.add(["DELETE"], path, policy, **kw)


def policy_of(route):
    return getattr(getattr(route, "endpoint", None), "twinlab_policy", None)


def all_routes(router):
    """Every route of an app or router, through included routers (FastAPI keeps them wrapped)."""
    for route in router.routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from all_routes(inner)
        else:
            yield route


# -- request bodies read before a (threaded) view runs --------------------------------------------


async def raw_body(request: Request) -> bytes:
    return await request.body()


async def json_body(request: Request):
    """The request's JSON, or None (the agent API reads bodies leniently, as it always has)."""
    import json

    data = await request.body()
    try:
        return json.loads(data) if data else None
    except (ValueError, UnicodeDecodeError):
        return None
