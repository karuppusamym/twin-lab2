# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: imports
# renamed to this repository's packages, unused paths cut. Source and changes: docs/UPSTREAM.md.
"""A small client for the Devin API v3 (organisation-scoped paths, service-user bearer key).

GET and DELETE retry 429, 5xx and connection failures with exponential backoff, honouring
Retry-After, at most 5 tries in all. POST retries only 429 (the request was not processed): a
create that got a 5xx, a gateway timeout or no answer may still have created a paid session, and
v3 has no idempotency key, so it is reported (with a hint to check the Devin web app) instead of
repeated.
Errors become `DevinError(status, title, detail)`, read from RFC 9457 problem+json when given.
The key is never logged or shown in a repr.
"""

from __future__ import annotations

import email.utils
import logging
import mimetypes
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.devin.ai"
MAX_TRIES = 5
MAX_WAIT = 60.0
SESSION_PREFIX = "devin-"
UNCERTAIN = "The request may have been processed; check before sending it again"
UNCERTAIN_SESSION = (
    "A Devin session may have been created anyway: check the Devin web app for it before "
    "retrying, or you may pay for two sessions"
)


class DevinError(Exception):
    """An error from the Devin API, or 0 for a request that never got an answer."""

    def __init__(self, status: int, title: str, detail: str | None = None) -> None:
        self.status = status
        self.title = title
        self.detail = detail
        super().__init__(f"{status} {title}" + (f": {detail}" if detail else ""))

    @property
    def transient(self) -> bool:
        return self.status in (0, 429) or self.status >= 500


def session_id(devin_id: str) -> str:
    """The id with its `devin-` prefix, whether or not it was given with one."""
    devin_id = devin_id.strip()
    return devin_id if devin_id.startswith(SESSION_PREFIX) else SESSION_PREFIX + devin_id


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _error(resp: httpx.Response) -> DevinError:
    title = resp.reason_phrase or "HTTP error"
    detail: str | None = None
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        title = str(body.get("title") or title)
        raw = body.get("detail")
        if isinstance(raw, str):
            detail = raw
        elif raw is not None:
            detail = str(raw)
    elif resp.text:
        detail = resp.text[:500]
    return DevinError(resp.status_code, title, detail)


class DevinClient:
    def __init__(
        self,
        api_key: str,
        org_id: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30,
        *,
        max_tries: int = MAX_TRIES,
        backoff: float = 1.0,
        headers: dict[str, str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        if not org_id:
            raise ValueError("org_id is required")
        self.org_id = org_id
        self.base_url = base_url.rstrip("/")
        self.max_tries = max(1, max_tries)
        self.backoff = backoff
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json, application/problem+json",
                "User-Agent": "twinlab-Twin Lab",
                **(headers or {}),
            },
        )

    def __repr__(self) -> str:
        return f"DevinClient(org_id={self.org_id!r}, base_url={self.base_url!r})"

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> DevinClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- transport -------------------------------------------------------------------------------

    def _org(self, path: str) -> str:
        return f"/v3/organizations/{self.org_id}{path}"

    def _request(
        self, method: str, path: str, *, uncertain: str = UNCERTAIN, **kwargs: Any
    ) -> dict:
        """One API call. `uncertain` is said when a POST may or may not have taken effect."""
        safe = method in ("GET", "DELETE")
        for attempt in range(1, self.max_tries + 1):
            try:
                resp = self._http.request(method, path, **kwargs)
            except httpx.HTTPError as exc:
                if not safe:
                    raise DevinError(
                        0, "No answer from the Devin API", f"{self.base_url}: {exc}. {uncertain}"
                    ) from exc
                if attempt == self.max_tries:
                    raise DevinError(
                        0, "Cannot reach the Devin API", f"{self.base_url}: {exc}"
                    ) from exc
                wait = min(MAX_WAIT, self.backoff * 2 ** (attempt - 1))
                log.warning("%s %s failed (%s); retrying in %.1fs", method, path, exc, wait)
                self._sleep(wait)
                continue
            log.debug("%s %s -> %s", method, path, resp.status_code)
            if resp.status_code >= 500 and not safe:
                err = _error(resp)
                err.detail = f"{err.detail}. {uncertain}" if err.detail else uncertain
                raise DevinError(err.status, err.title, err.detail)
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == self.max_tries:
                    raise _error(resp)
                wait = _retry_after(resp.headers.get("Retry-After"))
                if wait is None:
                    wait = self.backoff * 2 ** (attempt - 1)
                wait = min(MAX_WAIT, wait)
                log.warning(
                    "%s %s -> %s; retry %d of %d in %.1fs",
                    method,
                    path,
                    resp.status_code,
                    attempt,
                    self.max_tries - 1,
                    wait,
                )
                self._sleep(wait)
                continue
            if resp.status_code >= 400:
                raise _error(resp)
            if not resp.content:
                return {}
            try:
                body = resp.json()
            except ValueError as exc:
                raise DevinError(resp.status_code, "Unreadable response", resp.text[:500]) from exc
            return body if isinstance(body, dict) else {"items": body}
        raise AssertionError("unreachable")  # pragma: no cover

    # --- endpoints -------------------------------------------------------------------------------

    def upload(self, path: Path) -> dict:
        """Upload a file as an attachment. Returns `{attachment_id, name, url}`."""
        path = Path(path)
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        data = path.read_bytes()
        return self._request(
            "POST", self._org("/attachments"), files={"file": (path.name, data, mime)}
        )

    def create_session(
        self,
        prompt: str,
        *,
        repos: list[str] | None = None,
        attachment_urls: list[str] | None = None,
        title: str | None = None,
        tags: list[str] | None = None,
        max_acu_limit: int | None = None,
    ) -> dict:
        body: dict[str, Any] = {"prompt": prompt}
        for key, value in (
            ("repos", repos),
            ("attachment_urls", attachment_urls),
            ("title", title),
            ("tags", tags),
            ("max_acu_limit", max_acu_limit),
        ):
            if value is not None:
                body[key] = value
        return self._request("POST", self._org("/sessions"), json=body, uncertain=UNCERTAIN_SESSION)

    def get_session(self, devin_id: str) -> dict:
        return self._request("GET", self._org(f"/sessions/{session_id(devin_id)}"))

    def list_sessions(self, first: int = 1) -> dict:
        """One page of sessions. Used by Twin Lab's `devin check` as a cheap authenticated call."""
        return self._request("GET", self._org("/sessions"), params={"first": first})

    def send_message(
        self, devin_id: str, message: str, attachment_urls: list[str] | None = None
    ) -> dict:
        body: dict[str, Any] = {"message": message}
        if attachment_urls:
            body["attachment_urls"] = attachment_urls
        return self._request(
            "POST", self._org(f"/sessions/{session_id(devin_id)}/messages"), json=body
        )

    def terminate(self, devin_id: str, archive: bool = False) -> dict:
        params = {"archive": "true"} if archive else None
        return self._request(
            "DELETE", self._org(f"/sessions/{session_id(devin_id)}"), params=params
        )
