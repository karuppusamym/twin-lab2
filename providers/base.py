"""The build provider interface: what the session worker (builds.py) asks of an integration.

A *provider* is an integration (Devin, Claude, Copilot); a *model* is configuration inside it.
Each adapter declares the capabilities it has; the portal shows only the controls a provider
supports and never pretends a missing one works.

The worker owns the safety rules, so every adapter gets the same treatment:

- a start that may have happened (no answer, a 5xx, a worker that died mid-call) is reported as
  `ProviderError(uncertain=True)` and the worker looks for it (`find_started`) before it ever
  starts again;
- a reply that got no clear answer is *Delivery uncertain* and is never resent automatically;
- a provider finishing is *Ready for verification*, never Tested.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# capabilities
START = "start"
READ_EVENTS = "read_events"
REPLY = "reply"
INTERRUPT = "interrupt"
CANCEL = "cancel"
COLLECT_RESULTS = "collect_results"
RECONCILE_START = "reconcile_start"   # find a start whose answer was lost
TOOL_APPROVAL = "tool_approval"       # asks before running tools; answered with Approve / Deny
ACKNOWLEDGE = "acknowledge"           # confirms each reply it received (an event per message)
CLARIFY = "clarify"                   # a clarification conversation before code is written
EXECUTE_CODE = "execute_code"

CAPABILITY_LABELS = {
    START: "start", READ_EVENTS: "read events", REPLY: "reply", INTERRUPT: "interrupt", CANCEL: "cancel",
    COLLECT_RESULTS: "collect results (branch, pull request, commit, cost)",
    RECONCILE_START: "find a start whose answer was lost", TOOL_APPROVAL: "tool approvals",
    ACKNOWLEDGE: "acknowledges replies", CLARIFY: "clarification before coding", EXECUTE_CODE: "edits and runs code",
}

# session states an adapter can report (builds.py adds queued / starting / checking_started / cancelled)
RUNNING, WAITING, READY, FAILED = "running", "waiting_for_answer", "ready_for_verification", "failed"


class ProviderError(Exception):
    """A provider call that did not succeed.

    status:    the HTTP status when there was one (0: no answer)
    uncertain: the call may still have taken effect (a start may have created a paid session,
               a reply may have arrived): never repeated blindly
    transient: worth trying again later (rate limit, 5xx on a read)
    auth:      the credentials were refused
    gone:      the provider session no longer exists
    """

    def __init__(self, message, *, status=0, uncertain=False, transient=False, auth=False, gone=False,
                 rate_limited=False, detail=""):
        super().__init__(message)
        self.status, self.uncertain, self.transient = status, uncertain, transient
        self.auth, self.gone, self.rate_limited, self.detail = auth, gone, rate_limited, detail


class NotConfigured(ProviderError):
    """The provider cannot be used yet: a key, a package or a feature flag is missing."""


@dataclass
class SessionSpec:
    """Everything one build session starts from. The archive is the owner-approved package's
    exact bytes (checked against `pack_sha256` before the spec is made)."""
    build_id: int
    app_id: int
    app_name: str
    prompt: str
    archive: bytes
    pack_sha256: str
    revision: str
    repo: str
    base_branch: str
    base_sha: str
    branch: str
    workspace: Path
    model: str
    budget: float
    budget_unit: str
    tag: str
    title: str
    attempt: int = 1


@dataclass
class Started:
    session_id: str
    url: str | None = None
    status: str | None = None
    detail: str | None = None
    created_at: str = ""
    state: dict = field(default_factory=dict)


@dataclass
class Event:
    """Something the provider said or did, stored once per session under `event_id`.

    kind: clarification (a question), tool_approval (asks to run a tool: data has tool and input),
          status (progress worth showing), message (other text), ack (data.message_id was received),
          uncertain (data.message_id may not have arrived), failed (data.message_id was refused)
    """
    event_id: str
    kind: str
    text: str = ""
    data: dict = field(default_factory=dict)


@dataclass
class Snapshot:
    """One read of a session: its state, what happened since `cursor`, and its results."""
    state: str
    status: str | None = None
    detail: str | None = None
    url: str | None = None
    events: list = field(default_factory=list)
    cursor: str | None = None
    questions: list = field(default_factory=list)  # what it is waiting on now, newest last
    cost: float | None = None
    branch: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    head_sha: str | None = None
    note: str | None = None
    state_update: dict | None = None


class Provider:
    """The adapter interface. Every method that talks to the provider raises ProviderError."""

    name = ""
    label = ""
    capabilities: frozenset = frozenset()
    budget_unit = ""
    cost_unit = ""
    default_model = ""
    status_label = "Implemented"  # README / VERIFICATION wording: Implemented, Locally tested, Externally validated

    def has(self, capability):
        return capability in self.capabilities

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- budget ------------------------------------------------------------------------------------

    @classmethod
    def check_budget(cls, value):
        """The budget as a number in `budget_unit`, or ValueError with a sentence."""
        raise NotImplementedError

    # -- the session -------------------------------------------------------------------------------

    def prepare(self, spec: SessionSpec, state: dict) -> dict:
        """Work that is safe to repeat before the start (an upload): state to keep."""
        return {}

    def start(self, spec: SessionSpec, state: dict) -> Started:
        raise NotImplementedError

    def find_started(self, spec: SessionSpec, state: dict) -> list:
        """[Started] for sessions an uncertain start may have created (RECONCILE_START)."""
        return []

    def read(self, build: dict) -> Snapshot:
        raise NotImplementedError

    def reply(self, build: dict, message: dict) -> None:
        raise NotImplementedError

    def interrupt(self, build: dict) -> None:
        raise ProviderError(f"{self.label} sessions cannot be interrupted.")

    def cancel(self, build: dict, spec: SessionSpec | None = None) -> list:
        """Stop the session (and any an uncertain start made); the ids stopped."""
        raise NotImplementedError

    def explain(self, error: ProviderError) -> str:
        return f"{self.label}: {error}"
