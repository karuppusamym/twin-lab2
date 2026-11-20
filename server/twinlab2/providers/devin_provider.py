"""Devin build sessions: the pinned Twin Lab Devin client (devin.py) behind the provider interface.

Behaviour carried over from Build with Devin unchanged: the pack is uploaded as a Devin
attachment before the create, the create carries the session's idempotency tag, an uncertain
create is looked up by that tag (never repeated blindly), a cancelled session is terminated,
and a reply that got no clear answer is reported as uncertain, never resent.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import devin
import github
from providers import base

MAX_ACU_BUDGET = 1000


class DevinProvider(base.Provider):
    name = "devin"
    label = "Devin"
    capabilities = frozenset({base.START, base.READ_EVENTS, base.REPLY, base.CANCEL, base.COLLECT_RESULTS,
                              base.RECONCILE_START, base.EXECUTE_CODE})
    budget_unit = cost_unit = "ACU"
    default_model = ""  # Devin chooses its own model
    status_label = "Locally tested (fake Devin API); the pinned client is Twin Lab's"

    def __init__(self, settings, timeout=30.0, github_factory=None):
        self._client = devin.make_client(settings, timeout=timeout)  # ValueError names what is missing
        self._github = github_factory

    def close(self):
        self._client.close()

    @classmethod
    def check_budget(cls, value):
        try:
            budget = int(str(value).strip())
        except ValueError:
            raise ValueError("Give the ACU budget as a whole number.") from None
        if not 1 <= budget <= MAX_ACU_BUDGET:
            raise ValueError(f"Give an ACU budget between 1 and {MAX_ACU_BUDGET}.")
        return budget

    def _wrap(self, exc, uncertain_create=False):
        status = getattr(exc, "status", 0) or 0
        return base.ProviderError(
            devin.explain(exc), status=status, detail=str(exc),
            uncertain=uncertain_create and devin.uncertain(exc),
            transient=bool(getattr(exc, "transient", False)) or status == 429 or status >= 500 or status == 0,
            auth=status in (401, 403), gone=status == 404, rate_limited=status == 429)

    def explain(self, error):
        return str(error)

    # -- start --------------------------------------------------------------------------------------

    def prepare(self, spec, state):
        if state.get("attachment_url"):
            return {}
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / f"twinlab2-build-pack-{spec.revision[:12]}.zip"
            path.write_bytes(spec.archive)
            try:
                uploaded = self._client.upload(path)  # repeating an upload is harmless: no session, no cost
            except devin.error_class() as exc:
                raise self._wrap(exc) from exc
        return {"attachment_url": uploaded.get("url")}

    def start(self, spec, state):
        try:
            session = self._client.create_session(
                spec.prompt, repos=[spec.repo], attachment_urls=[state["attachment_url"]], title=spec.title,
                tags=[spec.tag, "twinlab2", f"twinlab2-app-{spec.app_id}"], max_acu_limit=int(spec.budget))
        except devin.error_class() as exc:
            raise self._wrap(exc, uncertain_create=True) from exc
        return self._started(session)

    def _started(self, session):
        return base.Started(session_id=devin.client_module().session_id(str(session.get("session_id") or "")),
                            url=session.get("url"), status=session.get("status"),
                            detail=session.get("status_detail"), created_at=str(session.get("created_at") or ""))

    def find_started(self, spec, state):
        try:
            return [self._started(s) for s in self._client.find_sessions(spec.tag)]
        except devin.error_class() as exc:
            raise self._wrap(exc) from exc

    # -- reading ------------------------------------------------------------------------------------

    def read(self, build):
        DevinError = devin.error_class()
        sid = build["provider_session_id"]
        try:
            session = self._client.get_session(sid)
        except DevinError as exc:
            raise self._wrap(exc) from exc
        state, note = devin.map_status(session.get("status"), session.get("status_detail"))
        snap = base.Snapshot(state=state, status=session.get("status"), detail=session.get("status_detail"),
                             url=session.get("url"), cost=session.get("acus_consumed"), note=note)
        prs = devin.pull_requests(session, build["repo"])
        if prs:
            _, number, url = max(prs, key=lambda p: p[1])
            snap.pr_number, snap.pr_url = number, url
            try:
                pull = self._github().pull(build["repo"], number)
                snap.head_sha, snap.branch = pull["head_sha"], pull.get("head_ref")
            except github.GitHubError as exc:
                snap.note = f"Pull request #{number}: {exc}"
        elif state == base.READY:
            snap.note = f"Devin finished without opening a pull request on {build['repo']}."
        if state == base.WAITING:
            try:
                messages = self._client.list_messages(sid)
            except DevinError as exc:
                snap.note = f"Could not read Devin's messages: {devin.explain(exc)}"
                messages = []
            snap.events, snap.questions = events_of(messages)
            if not snap.questions:
                snap.questions = ["Devin is waiting for an answer: open the session to see what it needs."]
        return snap

    def reply(self, build, message):
        try:
            self._client.send_message(build["provider_session_id"], message["body"])
        except devin.error_class() as exc:
            raise self._wrap(exc, uncertain_create=True) from exc  # uncertain: no answer or a 5xx

    def cancel(self, build, spec=None):
        DevinError = devin.error_class()
        try:
            if build["provider_session_id"]:
                ids = [build["provider_session_id"]]
            elif build["create_token"]:
                ids = [devin.client_module().session_id(str(s["session_id"]))
                       for s in self._client.find_sessions(build["idempotency_tag"])]
            else:
                ids = []
            for sid in ids:
                try:
                    self._client.terminate(sid)
                except DevinError as exc:
                    if exc.status != 404:
                        raise
        except DevinError as exc:
            raise self._wrap(exc) from exc
        return ids


def _from_person(m):
    source = str(m.get("source") or m.get("type") or m.get("origin") or m.get("role") or "").lower()
    return source in ("user", "user_message", "human", "api")


def events_of(messages):
    """(events, pending questions): every Devin message as an event keyed by its position (the
    list only grows), and Devin's messages since the last one a person sent."""
    events, pending = [], []
    for index, m in enumerate(messages):
        text = str(m.get("message") or m.get("text") or m.get("content") or "").strip()[:4000]
        if _from_person(m):
            pending = []
            continue
        if text:
            key = str(m.get("event_id") or m.get("id") or f"devin-message-{index}")
            events.append(base.Event(event_id=key, kind="clarification", text=text))
            pending.append(text)
    return events, pending
