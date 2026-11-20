"""Build providers: the integrations a build session can run on (see providers/base.py).

`catalog(settings)` lists each provider with whether it can be used here and why not;
`make(name, ...)` builds the adapter for a job. Copilot is behind TWINLAB2_ENABLE_COPILOT.
"""

from __future__ import annotations

import os

from providers.base import NotConfigured, ProviderError  # noqa: F401 - re-exported

NAMES = ("devin", "claude", "copilot")
LABELS = {"devin": "Devin", "claude": "Claude", "copilot": "GitHub Copilot"}
BUDGET_UNITS = {"devin": "ACU", "claude": "USD", "copilot": "premium requests"}


_extra = {}  # name -> adapter class registered at run time (tests register fake providers)


def register(cls):
    """Make another adapter class selectable (tests: a fake provider). Returns the class."""
    _extra[cls.name] = cls
    LABELS[cls.name] = cls.label
    BUDGET_UNITS[cls.name] = cls.budget_unit
    return cls


def names(env=None):
    return [name for name in (*NAMES, *_extra) if enabled(name, env)]


def enabled(name, env=None):
    env = os.environ if env is None else env
    if name in _extra:
        return True
    if name == "copilot":
        return env.get("TWINLAB2_ENABLE_COPILOT", "") == "1"
    return name in NAMES


def adapter_class(name):
    if name in _extra:
        return _extra[name]
    if name == "devin":
        from providers.devin_provider import DevinProvider
        return DevinProvider
    if name == "claude":
        from providers.claude_provider import ClaudeProvider
        return ClaudeProvider
    if name == "copilot":
        from providers.copilot_provider import CopilotProvider
        return CopilotProvider
    raise NotConfigured(f"Unknown build provider {name!r}.")


def capabilities(name):
    try:
        return adapter_class(name).capabilities
    except (ImportError, NotConfigured):
        return frozenset()
