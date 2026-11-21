"""The portal's request layer on ASGI (FastAPI): the request's state, errors, policies, CSRF.

- State: every request gets a `State` (its database connection, opened on first use and closed
  when the response is sent, the signed-in user, and the request). `g` and `db()` read the
  current request's state from any thread the server runs the view in.
- Errors: `fail(status, detail, code)` raises a `Problem`. Under /api/v1 every error is
  `application/problem+json` (RFC 9457): `type`, `title`, `status`, `detail` (the sentence to
  show), and `code` where the web app acts on it; a request body that is not in the contract's
  shape is 400 with per-field `errors`. Outside /api/v1 (the recorder's agent API) an error is
  `{"error": detail}`, as the recorder has always read it.
- Policies (auth.py decides): every route is added with one (`Routes.get(path, policy)`), and a
  route without one answers 403: a route added later without a decision is closed, not open.
- CSRF: every /api/v1 request other than GET, HEAD and OPTIONS carries `X-CSRF-Token`, the
  session's form token (`GET /api/v1/session` hands it out); otherwise 403 `csrf`.
"""

from __future__ import annotations

from contextvars import ContextVar
import hmac
from http import HTTPStatus
import logging
import secrets
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


# -- the request's state ------------------------------------------------------------------------


class State:
    """What a request carries: its connection (lazy), who is signed in, the Request."""

    def __init__(self, target, settings=None):
        self.target = target
        self.settings = settings
        self.request: Request | None = None
        self.user: str | None = None
        self.is_admin = False
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
        try:
            await self.app(scope, receive, send)
        finally:
            if extra is not None:
                extra()
            state.close()
            _state.reset(token)


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


class Problem(Exception):
    def __init__(self, status, detail=None, code=None, errors=None, headers=None, body=None):
        super().__init__(detail)
        self.status, self.detail, self.code = status, detail, code
        self.errors, self.headers, self.body = errors, headers or {}, body


def fail(status, detail=None, code=None, errors=None, headers=None):
    """Raise the HTTP error `status`: `detail` is the sentence to show, `code` the problem code."""
    raise Problem(status, detail, code, errors, headers)


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


def error_response(path, status, detail=None, code=None, errors=None, headers=None, body=None):
    if body is not None:  # a view's own error body (the agent API's {"error", ...} shapes)
        return JSONResponse(body, status_code=status, headers=headers)
    if not is_api(path):
        return JSONResponse({"error": detail or default_detail(status)}, status_code=status, headers=headers)
    out: dict[str, Any] = {"type": "about:blank", "title": title(status), "status": status,
                           "detail": detail or default_detail(status), "instance": path}
    if code is None and status == 401:
        code = "signed_out"
    if code:
        out["code"] = code
    if errors:
        out["errors"] = errors
    return JSONResponse(out, status_code=status, headers=headers, media_type="application/problem+json")


def install_errors(app):
    @app.exception_handler(Problem)
    async def problem(request, error: Problem):
        return error_response(request.url.path, error.status, error.detail, error.code, error.errors,
                              error.headers, error.body)

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
        return error_response(request.url.path, 500, "The portal failed to answer: the error is in its log.")


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
        fail(403, "Reload the page: the form token is missing or out of date.", "csrf")


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

        def decorate(view):
            view.twinlab_policy = policy.mark
            kwargs.setdefault("response_model_exclude_unset", True)
            if "response_model" not in kwargs:
                kwargs["response_model"] = None
            self.router.add_api_route(path, view, methods=methods,
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
