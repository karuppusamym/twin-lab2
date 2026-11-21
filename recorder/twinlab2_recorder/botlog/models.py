# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: imports
# renamed to this repository's packages, unused paths cut. Source and changes: docs/UPSTREAM.md.
"""Contracts for bot evidence: the robot log trace (I-08) and the bot trace (I-09).

Examples live in specs/robot-trace.example.json and specs/bot-trace.example.json. Importing this
module registers both as contract kinds, so Twin Lab's `validate` checks the examples.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from twinlab2_recorder.botlog.base import KINDS, Contract

# --- robot log trace (I-08) --------------------------------------------------------------------


class StepValues(Contract):
    in_: dict[str, Any] = Field(default={}, alias="in")
    out: dict[str, Any] = {}


class Step(Contract):
    """One activity instance: its Executing line paired with its Closed or Faulted line."""

    seq: int = Field(ge=1)
    ts: datetime | None = None  # when it started executing
    end_ts: datetime | None = None  # when it closed or faulted
    activity: str  # short type name, e.g. TypeInto
    activity_type: str | None = None  # as logged, e.g. UiPath.Core.Activities.TypeInto
    display: str | None = None
    state: Literal["Executing", "Closed", "Faulted", "Canceled", "Unknown"] = "Unknown"
    selector: dict[str, str] | None = None  # raw, css, and the last element's attributes
    values: StepValues = StepValues()
    variables: dict[str, Any] = {}
    redacted: bool = False
    redaction_reason: str | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    message: str | None = None  # the fault message, when faulted


class RunTrace(Contract):
    run_id: str
    job_id: str | None = None
    robot: str | None = None
    machine: str | None = None
    process: str | None = None
    process_version: str | None = None
    started: datetime | None = None
    ended: datetime | None = None
    source: str | None = None  # the file the trace was read from
    steps: list[Step] = []
    unmapped_fields: list[str] = []  # keys the parser does not understand; names only
    bad_lines: int = Field(default=0, ge=0)  # lines that could not be read at all
    log_lines: int = Field(default=0, ge=0)  # lines that belong to this run


# --- bot trace (I-09) --------------------------------------------------------------------------

Action = Literal[
    "goto", "type", "click", "read", "select", "wait", "hover", "submit", "query", "request"
]


class TraceStep(Contract):
    seq: int = Field(ge=1)
    ts: datetime | None = None
    app: str | None = None
    screen: str | None = None  # the screen name from the automation analysis, or the path
    path: str | None = None  # the screen's path pattern
    action: Action
    field: str | None = None  # the variable the automation carries, never a value
    selector: str | None = None  # CSS when it can be expressed, else "uipath:<raw>"
    value: Any = None  # typed, selected or read; None when redacted or not logged
    redacted: bool = False
    display: str | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    gap_ms: int | None = Field(default=None, ge=0)  # until the next step: what the bot waited for
    wait_ms: int | None = Field(default=None, ge=0)  # duration + gap
    requests: list[str] = []  # "METHOD /path/pattern" of non-asset requests in the step's window
    dom: str | None = None  # the recording's after-snapshot, when joined with a recording
    matched_by: Literal["selector", "order", "url", "none"] = "none"


class WaitStat(Contract):
    app: str | None = None
    screen: str | None = None
    action: str
    target: str  # field, else selector
    n: int = Field(ge=1)
    p50_ms: int = Field(ge=0)
    p95_ms: int = Field(ge=0)
    max_ms: int = Field(ge=0)
    requests: list[str] = []


class TraceGap(Contract):
    where: str
    what: str


class BotTrace(Contract):
    run_id: str
    process: str | None = None
    automation: str | None = None
    source: Literal["production", "harness", "agent", "recording", "unknown"] = "unknown"
    masked: bool = False
    started: datetime | None = None
    ended: datetime | None = None
    recording: str | None = None
    steps: list[TraceStep] = []
    waits: list[WaitStat] = []
    gaps: list[TraceGap] = []


# --- NetLog (I-03) -----------------------------------------------------------------------------


class NetRequest(Contract):
    source_id: int
    url: str | None = None
    method: str | None = None
    status: int | None = None
    start: datetime | None = None
    end: datetime | None = None
    initiator: str | None = None  # the origin or URL Chrome names as initiator
    initiator_url: str | None = None  # the top-level document request, when derivable
    complete: bool = False  # False when the file ended before the request did


KINDS.update({"robot-trace": RunTrace, "bot-trace": BotTrace})
