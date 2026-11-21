# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: only the
# definitions named below were taken from their module. Source and changes: docs/UPSTREAM.md.
"""The base of the robot-log contracts, from Twin Lab's `models` module (`Contract`). `KINDS`
stays empty here: TwinLab2 reads robot logs, it does not load Twin Lab's YAML contracts."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Contract(BaseModel):
    """Base for all contracts: unknown keys are errors, not silently ignored."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


KINDS: dict[str, type[Contract]] = {}
