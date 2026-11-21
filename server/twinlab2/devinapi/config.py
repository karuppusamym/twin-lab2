# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: imports
# renamed to this repository's packages, unused paths cut. Source and changes: docs/UPSTREAM.md.
"""Devin settings: environment variables first, then `twinlab.local.yaml` at the repository root.

`twinlab.local.yaml` is Git-ignored and holds the office's own values. A variable set in the
environment wins over the same key in the file. The key is never printed: `DevinConfig` masks it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import yaml

LOCAL_FILE = "twinlab.local.yaml"
DEFAULT_BASE_URL = "https://api.devin.ai"
KEYS = ("DEVIN_API_KEY", "DEVIN_ORG_ID", "DEVIN_BASE_URL", "GITHUB_REPO", "STATE_DIR")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class DevinConfig:
    api_key: str | None = field(default=None, repr=False)
    org_id: str | None = None
    base_url: str = DEFAULT_BASE_URL
    github_repo: str | None = None
    state_dir: Path = Path(".lab-state")
    local_file: Path | None = None
    sources: dict[str, str] = field(default_factory=dict)  # key -> "environment" or file path

    @property
    def configured(self) -> bool:
        """True only when both an API key and an organisation id are set."""
        return bool(self.api_key) and bool(self.org_id)

    @property
    def missing(self) -> list[str]:
        return [
            k for k, v in (("DEVIN_API_KEY", self.api_key), ("DEVIN_ORG_ID", self.org_id)) if not v
        ]

    @property
    def masked_key(self) -> str:
        if not self.api_key:
            return "(not set)"
        return f"{self.api_key[:4]}...{self.api_key[-2:]}" if len(self.api_key) > 10 else "****"

    def __repr__(self) -> str:
        return (
            f"DevinConfig(api_key={self.masked_key!r}, org_id={self.org_id!r}, "
            f"base_url={self.base_url!r}, github_repo={self.github_repo!r}, "
            f"state_dir={str(self.state_dir)!r})"
        )


def _read_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"{path}: cannot read: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping of KEY: value at the top level")
    out: dict[str, str] = {}
    for key, value in data.items():
        name = str(key).upper()
        if name in KEYS and value not in (None, ""):
            out[name] = str(value).strip()
    return out


def load_config(repo_root: Path, env: Mapping[str, str] | None = None) -> DevinConfig:
    """Settings for the Devin hand-off. `env` defaults to `os.environ` (tests pass their own)."""
    env = os.environ if env is None else env
    local = Path(repo_root) / LOCAL_FILE
    from_file = _read_file(local)
    values: dict[str, str] = {}
    sources: dict[str, str] = {}
    for key in KEYS:
        if env.get(key, "").strip():
            values[key] = env[key].strip()
            sources[key] = "environment"
        elif key in from_file:
            values[key] = from_file[key]
            sources[key] = str(local)
    state = Path(values.get("STATE_DIR", ".lab-state"))
    if not state.is_absolute():
        state = Path(repo_root) / state
    return DevinConfig(
        api_key=values.get("DEVIN_API_KEY"),
        org_id=values.get("DEVIN_ORG_ID"),
        base_url=values.get("DEVIN_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
        github_repo=values.get("GITHUB_REPO"),
        state_dir=state,
        local_file=local if local.is_file() else None,
        sources=sources,
    )
