"""GitHub Copilot build sessions (behind TWINLAB2_ENABLE_COPILOT=1): the Copilot SDK
(`github-copilot-sdk`: sessions, events, tool permission requests, resume) in the session's own
workspace, through the same isolated runner as Claude (providers/runner.py).

**Not validated**: authentication (a Copilot-entitled GitHub token, COPILOT_GITHUB_TOKEN) and
deployment (the Copilot CLI the SDK drives, on the worker machine) have never been exercised
against GitHub. The tests use a fake agent. The budget is recorded but not enforced: the SDK
adapter has no spending limit to pass it to.
"""

from __future__ import annotations

import importlib.util
import os

from providers import base
from providers.local_runner import LocalRunnerProvider

MAX_REQUESTS = 1000


class CopilotProvider(LocalRunnerProvider):
    name = "copilot"
    label = "GitHub Copilot"
    backend = "copilot"
    clarify = False
    capabilities = frozenset({base.START, base.READ_EVENTS, base.REPLY, base.INTERRUPT, base.CANCEL,
                              base.COLLECT_RESULTS, base.RECONCILE_START, base.TOOL_APPROVAL, base.ACKNOWLEDGE,
                              base.EXECUTE_CODE})
    budget_unit = cost_unit = "premium requests"
    default_model = ""
    status_label = "Implemented behind a feature flag; authentication and deployment not validated"

    def __init__(self, secrets, model="", **kwargs):
        super().__init__(secrets, **kwargs)
        self.model = model

    @staticmethod
    def configured_model(stored, env=None):
        env = os.environ if env is None else env
        return env.get("COPILOT_MODEL") or stored.get("COPILOT_MODEL") or ""

    @staticmethod
    def token(stored, env=None):
        env = os.environ if env is None else env
        return env.get("COPILOT_GITHUB_TOKEN") or stored.get("COPILOT_GITHUB_TOKEN") or ""

    @classmethod
    def problem(cls, stored, env=None):
        env = os.environ if env is None else env
        if env.get("TWINLAB2_ENABLE_COPILOT") != "1":
            return "Copilot sessions are off: set TWINLAB2_ENABLE_COPILOT=1 (not validated)."
        if not cls.token(stored, env):
            return "Copilot is not configured: an admin sets COPILOT_GITHUB_TOKEN under Setup (not validated)."
        if not env.get("TWINLAB2_COPILOT_AGENT") and importlib.util.find_spec("copilot") is None:
            return "The Copilot SDK is not installed on this machine: pip install -r requirements-providers.txt."
        return None

    @classmethod
    def from_settings(cls, stored, github_values, ctx):
        problem = cls.problem(stored)
        if problem:
            raise base.NotConfigured(problem)
        return cls({"copilot_token": cls.token(stored), "github_token": github_values.get("token")},
                   model=cls.configured_model(stored), github_api=github_values.get("api"))

    @classmethod
    def check_budget(cls, value):
        try:
            budget = int(float(str(value).strip()))
        except ValueError:
            raise ValueError("Give the budget as a number of premium requests.") from None
        if not 1 <= budget <= MAX_REQUESTS:
            raise ValueError(f"Give a Copilot budget between 1 and {MAX_REQUESTS} premium requests.")
        return budget
