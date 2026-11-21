"""Devin for TwinLab2 builds: Twin Lab's Devin client, as copied into `twinlab2.devinapi`.

The client (`twinlab2.devinapi.client`: attachment upload, session creation, polling, messages,
termination, and recognising a create that may have happened after a timeout or a 5xx), the
settings loader (`twinlab2.devinapi.config`) and the Devin-status mapping
(`twinlab2.devinapi.jobs.map_status`) are Twin Lab's, copied at 90bdd91 (docs/UPSTREAM.md).

What TwinLab2 adds on top, in a subclass, are two reads that client lacks:

- `find_sessions(tag)`: the sessions carrying an idempotency tag. The Devin v3 API has no
  idempotency key on create, so a build tags its session and, after an uncertain create, looks
  for that tag before it ever creates again.
- `list_messages(session)`: the session's messages, to show Devin's questions.

Both are GET requests, retried by the client's transport like any other read.
"""

from __future__ import annotations

import os
from pathlib import Path
import re


from twinlab2 import paths

_loaded = None


class DevinUnavailable(Exception):
    """The Devin client cannot be imported (httpx is not installed)."""


def _modules():
    """The `twinlab2.devinapi` modules (client, config, jobs), imported once per process."""
    global _loaded
    if _loaded is not None:
        return _loaded
    try:
        from twinlab2.devinapi import client, config, jobs
    except ImportError as exc:  # httpx missing
        raise DevinUnavailable(f"The Devin client cannot be imported: {exc}") from exc
    _loaded = (client, config, jobs)
    return _loaded


def client_module():
    return _modules()[0]


def error_class():
    """The client's `DevinError`."""
    return client_module().DevinError


def map_status(status, detail):
    """Twin Lab's mapping of a Devin status, in TwinLab2's build states."""
    state, note = _modules()[2].map_status(status, detail)
    return {"finished": "ready_for_verification", "waiting_for_user": "waiting_for_answer"}.get(state, state), note


def settings(secrets=None, env=None):
    """{api_key, org_id, base_url, sources}: Twin Lab's loader (environment, then the Git-ignored
    twinlab.local.yaml), then the secrets saved under Setup for what is still missing."""
    config = _modules()[1].load_config(paths.ROOT, os.environ if env is None else env)
    secrets = secrets or {}
    out = {"api_key": config.api_key, "org_id": config.org_id, "base_url": config.base_url,
           "sources": dict(config.sources)}
    for key, name in (("api_key", "DEVIN_API_KEY"), ("org_id", "DEVIN_ORG_ID"), ("base_url", "DEVIN_BASE_URL")):
        if name not in out["sources"] and secrets.get(name):
            out[key] = secrets[name].rstrip("/") if key == "base_url" else secrets[name]
            out["sources"][name] = "Setup"
    return out


def make_client(values, timeout=30.0, **kwargs):
    """A `BuildDevin` for `settings()` values. ValueError names what is missing."""
    missing = [n for k, n in (("api_key", "DEVIN_API_KEY"), ("org_id", "DEVIN_ORG_ID")) if not values.get(k)]
    if missing:
        raise ValueError("Devin is not configured: set " + " and ".join(missing) + " under Setup.")
    cls = _subclass()
    return cls(values["api_key"], values["org_id"], values.get("base_url") or "https://api.devin.ai",
               timeout=timeout, **kwargs)


_cls = None


def _subclass():
    global _cls
    if _cls is None:
        base = client_module().DevinClient

        class BuildDevin(base):
            def find_sessions(self, tag, page_size=100, max_pages=20):
                """Every session tagged `tag`. Passes `tags` as a filter and still checks each
                session's tags, so a server that ignores the filter gives the same answer."""
                found, after = [], None
                for _ in range(max_pages):
                    params = {"first": page_size, "tags": tag}
                    if after:
                        params["after"] = after
                    page = self._request("GET", self._org("/sessions"), params=params)
                    found += [s for s in page.get("items") or [] if tag in (s.get("tags") or [])]
                    after = page.get("end_cursor") or page.get("next_cursor")
                    if not page.get("has_next_page") or not after:
                        break
                return found

            def list_messages(self, devin_id):
                """The session's messages, oldest first; [] when the API offers no such list."""
                DevinErr = client_module().DevinError
                try:
                    body = self._request("GET", self._org(f"/sessions/{client_module().session_id(devin_id)}/messages"))
                except DevinErr as exc:
                    if exc.status in (404, 405):
                        return []
                    raise
                return list(body.get("items") or body.get("messages") or [])

        _cls = BuildDevin
    return _cls


def uncertain(error):
    """True when a failed create may still have made a (paid) session: no answer at all, or a
    5xx. The client marks exactly these with its UNCERTAIN_SESSION wording."""
    module = client_module()
    return error.status == 0 or error.status >= 500 or module.UNCERTAIN_SESSION in (error.detail or "")


def explain(error):
    """A sentence for the build page."""
    if error.status in (401, 403):
        return (f"The Devin API refused the key ({error.status} {error.title}): it may have expired or been "
                "revoked, or it lacks access to this organisation. Update DEVIN_API_KEY under Setup, then Retry.")
    if error.status == 429:
        return "The Devin API is rate limiting requests; the build will try again shortly."
    return f"Devin: {error}"


PR_URL = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)/?$")


def pull_requests(session, repo=None):
    """[(repo, number, url)] of the session's pull requests, those on `repo` when given."""
    out = []
    for item in session.get("pull_requests") or []:
        match = PR_URL.match(str(item.get("pr_url") or ""))
        if match and (repo is None or match.group(1).lower() == repo.lower()):
            out.append((match.group(1), int(match.group(2)), match.group(0)))
    return out


def questions(messages):
    """Devin's messages since the last message a person sent: what it is waiting on."""
    def from_person(m):
        source = str(m.get("source") or m.get("type") or m.get("origin") or m.get("role") or "").lower()
        return source in ("user", "user_message", "human", "api")

    pending = []
    for m in messages:
        if from_person(m):
            pending = []
        else:
            text = str(m.get("message") or m.get("text") or m.get("content") or "").strip()
            if text:
                pending.append(text[:4000])
    return pending
