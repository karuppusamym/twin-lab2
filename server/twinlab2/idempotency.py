"""`Idempotency-Key` on the create-type POSTs of /api/v1 (api/openapi.yaml: IdempotencyKey).

A client that may send a request twice (a retry after a timeout, a double click) names it with a
key, one per intended action. The portal keeps the key with the caller, for KEEP_HOURS:

- the same key again with the same request (method, path, body) → the first outcome again, with
  `Idempotent-Replayed: true`; nothing is created twice;
- the same key with a different request → 422 `idempotency_key_reused`;
- the same key while the first request is still running → 409 `idempotency_in_progress`;
- a request that failed (any error status) keeps nothing: the key can be sent again.

Two kinds of route use it (`Routes.add(..., idempotent=...)`):

- "stored": the first response's body is kept and sent again as it was;
- "native": the operation already deduplicates by its body's `request_key` (the build sessions:
  builds.py). `request_key` stays accepted as an alias of the header (both given must match, else
  400 `idempotency_key_mismatch`); the header's key becomes the request_key, and a replay answers
  from the resource as it is now (`created: false`).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import functools
import hashlib
import json
import re

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from twinlab2.rest import current, db, fail, g

HEADER = "Idempotency-Key"
REPLAYED = "Idempotent-Replayed"
KEEP_HOURS = 24
STALE_SECONDS = 300  # a "pending" key older than this was left by a crashed request: taken over
_KEY = re.compile(r"[\x21-\x7e]{1,255}")


def _now():
    return datetime.now(timezone.utc)


def _stamp(when):
    return when.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def key_of(body_key=None):
    """The request's idempotency key: the header, else the body's `request_key` (the alias)."""
    raw = g.request.headers.get(HEADER)
    header = raw.strip() if raw is not None else None
    if header is not None and not _KEY.fullmatch(header):
        fail(400, f"“{HEADER}” must be 1 to 255 visible ASCII characters.",
             errors=[{"field": HEADER, "message": "invalid"}])
    body_key = (body_key or "").strip() or None
    if header and body_key and header != body_key:
        fail(400, f"“{HEADER}” and “request_key” differ: send one key.", "idempotency_key_mismatch",
             errors=[{"field": "request_key", "message": f"differs from {HEADER}"}])
    return header or body_key


def _models(kwargs):
    return [v for v in kwargs.values() if isinstance(v, BaseModel)]


def fingerprint(method, path, kwargs):
    """What makes two requests "the same": method, path and body, without the key itself."""
    parts = {}
    for name, value in sorted(kwargs.items()):
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="json", exclude={"request_key"})
        parts[name] = jsonable_encoder(value)
    text = json.dumps([method, path, parts], sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def claim(user, key, print_):
    """None when this request is the key's first (it is now "pending"), else the stored row."""
    now = _now()
    connection = db()
    with connection:
        connection.execute("DELETE FROM idempotency_keys WHERE created_at < ?",
                           (_stamp(now - timedelta(hours=KEEP_HOURS)),))
        row = connection.execute("SELECT * FROM idempotency_keys WHERE user_name = ? AND idem_key = ?",
                                 (user, key)).fetchone()
        if row is None:
            try:
                connection.execute(
                    "INSERT INTO idempotency_keys (user_name, idem_key, fingerprint, state, created_at) "
                    "VALUES (?, ?, ?, 'pending', ?)", (user, key, print_, _stamp(now)))
            except Exception as error:  # a concurrent first request inserted it
                if "unique" not in str(error).lower() and "duplicate" not in str(error).lower():
                    raise
                fail(409, "A request with this Idempotency-Key is still being processed: try again shortly.",
                     "idempotency_in_progress", headers={"Retry-After": "1"})
            return None
        if row["fingerprint"] != print_:
            fail(422, "This Idempotency-Key was already used for a different request: use a new key.",
                 "idempotency_key_reused")
        if row["state"] == "pending":
            if row["created_at"] > _stamp(now - timedelta(seconds=STALE_SECONDS)):
                fail(409, "A request with this Idempotency-Key is still being processed: try again shortly.",
                     "idempotency_in_progress", headers={"Retry-After": "1"})
            connection.execute("UPDATE idempotency_keys SET created_at = ? WHERE user_name = ? AND idem_key = ?",
                               (_stamp(now), user, key))
            return None
        return row


def _finish(user, key, status=None, body=None):
    with db():
        db().execute("UPDATE idempotency_keys SET state = 'done', status = ?, body = ? WHERE user_name = ? AND idem_key = ?",
                     (status, body, user, key))


def _release(user, key):
    try:
        with db():
            db().execute("DELETE FROM idempotency_keys WHERE user_name = ? AND idem_key = ? AND state = 'pending'",
                         (user, key))
    except Exception:  # noqa: BLE001 - the error being answered matters more; the key goes stale
        pass


def _encode(result, response_model):
    if response_model is not None and isinstance(response_model, type) and issubclass(response_model, BaseModel):
        return response_model.model_validate(result).model_dump(mode="json", exclude_unset=True)
    return jsonable_encoder(result)


def wrap(view, kind, status_code=200, response_model=None):
    """The view with Idempotency-Key handling ("stored" or "native", see the module)."""
    if kind not in ("stored", "native"):
        raise ValueError(f"idempotent must be 'stored' or 'native', not {kind!r}")

    @functools.wraps(view)
    def run(*args, **kwargs):
        models = _models(kwargs)
        body_key = next((getattr(m, "request_key", None) for m in models if hasattr(m, "request_key")), None)
        key = key_of(body_key)
        if not key:
            return view(*args, **kwargs)
        if kind == "native":
            for model in models:
                if hasattr(model, "request_key"):
                    model.request_key = key
        request = g.request
        user = g.get("user") or "-"
        row = claim(user, key, fingerprint(request.method, request.url.path, kwargs))
        if row is not None and kind == "stored":
            return JSONResponse(json.loads(row["body"]), status_code=row["status"], headers={REPLAYED: "true"})
        if row is not None:  # native: the operation finds what the key made, and answers created: false
            current().response_headers[REPLAYED] = "true"
            return view(*args, **kwargs)
        try:
            result = view(*args, **kwargs)
        except BaseException:
            _release(user, key)
            raise
        if kind == "native":
            _finish(user, key)
            return result
        if hasattr(result, "status_code") and hasattr(result, "body"):  # a view's own Response: not kept
            _release(user, key)
            return result
        body = _encode(result, response_model)
        _finish(user, key, status_code, json.dumps(body))
        return JSONResponse(body, status_code=status_code)

    return run

