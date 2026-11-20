"""Claude build sessions: a clarification conversation on the Messages API, then the Claude Agent
SDK editing and running code in the session's own workspace (providers/runner.py, one isolated
process per session). The model is configuration (CLAUDE_MODEL, default claude-opus-5-5).
"""

from __future__ import annotations

import importlib.util
import os

from providers import base
from providers.clarify import DEFAULT_MODEL
from providers.local_runner import LocalRunnerProvider

MAX_USD = 500.0


class ClaudeProvider(LocalRunnerProvider):
    name = "claude"
    label = "Claude"
    backend = "claude"
    clarify = True
    capabilities = frozenset({base.START, base.READ_EVENTS, base.REPLY, base.INTERRUPT, base.CANCEL,
                              base.COLLECT_RESULTS, base.RECONCILE_START, base.TOOL_APPROVAL, base.ACKNOWLEDGE,
                              base.CLARIFY, base.EXECUTE_CODE})
    budget_unit = cost_unit = "USD"
    default_model = DEFAULT_MODEL
    status_label = "Implemented; locally tested with a fake agent and a fake Messages API; not externally validated"

    def __init__(self, secrets, model=DEFAULT_MODEL, **kwargs):
        super().__init__(secrets, **kwargs)
        self.model = model or DEFAULT_MODEL

    @staticmethod
    def configured_model(stored, env=None):
        env = os.environ if env is None else env
        return env.get("CLAUDE_MODEL") or stored.get("CLAUDE_MODEL") or DEFAULT_MODEL

    @staticmethod
    def api_key(stored, env=None):
        env = os.environ if env is None else env
        return env.get("ANTHROPIC_API_KEY") or stored.get("ANTHROPIC_API_KEY") or ""

    @classmethod
    def problem(cls, stored, env=None):
        """Why a Claude session cannot start here, or None."""
        env = os.environ if env is None else env
        if not cls.api_key(stored, env):
            return "Claude is not configured: an admin sets ANTHROPIC_API_KEY under Setup."
        if importlib.util.find_spec("anthropic") is None:
            return "The anthropic package is not installed: pip install -r requirements.txt."
        if not env.get("TWINLAB2_CLAUDE_AGENT") and importlib.util.find_spec("claude_agent_sdk") is None:
            return "The Claude Agent SDK is not installed on this machine: pip install -r requirements-providers.txt."
        return None

    @classmethod
    def from_settings(cls, stored, github_values, ctx):
        problem = cls.problem(stored)
        if problem:
            raise base.NotConfigured(problem)
        return cls({"anthropic_api_key": cls.api_key(stored), "github_token": github_values.get("token")},
                   model=cls.configured_model(stored), github_api=github_values.get("api"),
                   anthropic_base_url=os.environ.get("ANTHROPIC_BASE_URL") or None)

    @classmethod
    def check_budget(cls, value):
        try:
            budget = float(str(value).strip())
        except ValueError:
            raise ValueError("Give the budget in US dollars, e.g. 10 or 2.5.") from None
        if not 0.5 <= budget <= MAX_USD:
            raise ValueError(f"Give a Claude budget between 0.5 and {MAX_USD:g} USD.")
        return budget
