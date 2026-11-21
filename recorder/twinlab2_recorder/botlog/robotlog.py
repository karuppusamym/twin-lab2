# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: imports
# renamed to this repository's packages, unused paths cut. Source and changes: docs/UPSTREAM.md.
"""Robot log parser (Prod Echo I-08): a UiPath Verbose log -> one RunTrace per job.

Accepts an Orchestrator log export (a JSON array, an OData `{"value": [...]}` page, or JSON lines)
and an NLog JSON-lines file. Lines are in the shape documented in Prod Echo's
docs/05-capture-tools.md. Rules:

- The run boundary is a change of jobId, or an "execution started" / "execution ended" message.
- A `Private` activity is emitted `redacted: true` with no values.
- Keys the parser does not understand are listed in `unmapped_fields` (names only), never dropped.
- A line that cannot be read is counted in `bad_lines`; the parser never raises on content.
- Each activity's Executing line is paired with its Closed or Faulted line for `duration_ms`.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from twinlab2_recorder.botlog import uipath
from twinlab2_recorder.botlog.models import RunTrace, Step, StepValues

# Top-level keys, lower-cased, mapped to what they mean. Keys known but not used are kept here so
# they do not count as unmapped.
KEY_ALIASES = {
    "message": "message", "@m": "message", "msg": "message", "rawmessage": "raw",
    "level": "level", "@l": "level",
    "timestamp": "ts", "time": "ts", "@t": "ts", "date": "ts",
    "processname": "process", "processversion": "process_version",
    "jobid": "job", "jobkey": "job",
    "robotname": "robot", "machinename": "machine", "machine": "machine",
    "activityinfo": "activity",
}  # fmt: skip
KNOWN_IGNORED = {
    "logtype", "fingerprint", "windowsidentity", "machineid", "organizationunitid",
    "tenantkey", "initiatedby", "filename", "id", "logger", "totalexecutiontimeinseconds",
    "totalexecutiontime", "queueitemid", "transactionstate", "transactionid", "transactionstatus",
    "processingexceptionreason", "processingexceptiontype", "@odata.context", "hostmachinename",
    "robottype", "organizationunitfullyqualifiedname", "userkey",
}  # fmt: skip
ACTIVITY_KEYS = {"activity", "displayname", "state", "arguments", "variables", "private"}
ACTIVITY_IGNORED = {"workflowfilename", "activityid", "instanceid", "parentid", "id"}
SELECTOR_KEYS = ("Selector", "Target", "selector", "target")
# Activities whose arguments carry a value; when one arrives without arguments it was Private.
VALUE_BEARING = {
    "TypeInto", "TypeSecureText", "SetText", "GetText", "GetFullText", "GetValue",
    "GetAttribute", "SelectItem", "SelectMultipleItems",
}  # fmt: skip
STATES = {
    "executing": "Executing",
    "closed": "Closed",
    "faulted": "Faulted",
    "canceled": "Canceled",
}
STARTED = re.compile(r"execution\s+started", re.I)
ENDED = re.compile(r"execution\s+ended", re.I)


@dataclass
class ParseResult:
    runs: list[RunTrace] = field(default_factory=list)
    bad_lines: int = 0  # across the whole file
    lines: int = 0


# --- reading records ---------------------------------------------------------------------------


_BAD = object()  # stands in for a line that could not be read, so it is charged to its run


def _records(text: str) -> list[Any]:
    """The file's records, in order; unreadable lines appear as `_BAD`."""
    stripped = text.lstrip("﻿ \t\r\n")
    if stripped.startswith("[") or stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except ValueError:
            data = None
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("value"), list):
            return data["value"]
        if isinstance(data, dict):
            return [data]
    records: list[Any] = []
    for line in text.splitlines():
        line = line.strip().rstrip(",")
        if not line or line in ("[", "]"):
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            records.append(_BAD)
    return records


def _ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace(" ", "T", 1)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # Python accepts at most six fractional digits; .NET writes seven.
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    try:
        ts = datetime.fromisoformat(text)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _flatten(record: dict) -> dict:
    """Merge an Orchestrator `RawMessage` (the original line as a string) and NLog property bags."""
    out = dict(record)
    for key in list(out):
        low = key.lower()
        if low == "rawmessage" and isinstance(out[key], str):
            try:
                raw = json.loads(out[key])
            except ValueError:
                continue
            if isinstance(raw, dict):
                del out[key]
                for k, v in raw.items():
                    out.setdefault(k, v)
        elif low in ("properties", "fields", "@properties") and isinstance(out[key], dict):
            bag = out.pop(key)
            for k, v in bag.items():
                out.setdefault(k, v)
    return out


@dataclass
class _Line:
    message: str = ""
    level: str | None = None
    ts: datetime | None = None
    process: str | None = None
    process_version: str | None = None
    job: str | None = None
    robot: str | None = None
    machine: str | None = None
    activity: dict | None = None
    unmapped: list[str] = field(default_factory=list)


def _line(record: dict) -> _Line:
    line = _Line()
    for key, value in _flatten(record).items():
        low = key.lower()
        meaning = KEY_ALIASES.get(low)
        if meaning == "message":
            line.message = str(value) if value is not None else ""
        elif meaning == "level":
            line.level = str(value) if value is not None else None
        elif meaning == "ts":
            line.ts = _ts(value)
        elif meaning in ("process", "process_version", "job", "robot", "machine"):
            setattr(line, meaning, str(value) if value not in (None, "") else None)
        elif meaning == "activity":
            if isinstance(value, dict):
                line.activity = value
                for k in value:
                    if k.lower() not in ACTIVITY_KEYS | ACTIVITY_IGNORED:
                        line.unmapped.append(f"activityInfo.{k}")
            elif value is not None:
                line.unmapped.append(key)
        elif low not in KNOWN_IGNORED:
            line.unmapped.append(key)
    return line


# --- selectors ---------------------------------------------------------------------------------


def selector_info(raw: Any) -> dict[str, str] | None:
    """A UiPath selector as logged -> {raw, css, and the last webctrl's attributes}."""
    if isinstance(raw, dict):
        raw = next((raw[k] for k in SELECTOR_KEYS if isinstance(raw.get(k), str)), None)
    if not isinstance(raw, str) or not raw.strip():
        return None
    info: dict[str, str] = {"raw": raw}
    tags = re.findall(r"<(\w+)((?:\s+[\w-]+\s*=\s*'[^']*')*)\s*/?>", raw)
    if not tags:
        info["css"] = raw  # already CSS, as the harness and modern activities may log it
        return info
    try:
        css = uipath.to_css(raw)
    except Exception:  # noqa: BLE001 - a selector we cannot read must not stop the parse
        css = None
    if css:
        info["css"] = css
    for tag, body in tags:
        attrs = dict(re.findall(r"([\w-]+)\s*=\s*'([^']*)'", body))
        if tag.lower() in ("html", "wnd") and "title" in attrs:
            info["title"] = attrs["title"]
        if tag.lower() in ("html", "wnd") and "app" in attrs:
            info["app"] = attrs["app"]
    last = dict(re.findall(r"([\w-]+)\s*=\s*'([^']*)'", tags[-1][1]))
    for k, v in last.items():
        info.setdefault(k, v)
    return info


# --- runs --------------------------------------------------------------------------------------


def _short(activity_type: str | None) -> str:
    if not activity_type:
        return "Unknown"
    return activity_type.rsplit(".", 1)[-1].split("`", 1)[0]


def _run_id(started: datetime | None, key: str) -> str:
    stamp = (started or datetime(1970, 1, 1, tzinfo=timezone.utc)).astimezone(timezone.utc)
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:6]
    return f"run_{stamp.strftime('%Y%m%dT%H%M%SZ')}_{digest}"


class _Run:
    def __init__(self, job: str | None, index: int, source: str):
        self.job = job
        self.index = index
        self.source = source
        self.steps: list[Step] = []
        self.open: list[Step] = []
        self.unmapped: dict[str, None] = {}
        self.first: _Line | None = None
        self.started: datetime | None = None
        self.ended: datetime | None = None
        self.bad = 0
        self.lines = 0
        self.closed = False

    def add(self, line: _Line) -> None:
        self.lines += 1
        self.first = self.first or line
        for name in line.unmapped:
            self.unmapped.setdefault(name, None)
        if line.ts:
            self.started = self.started or line.ts
            self.ended = line.ts
        if line.activity is not None:
            self.activity(line)

    def activity(self, line: _Line) -> None:
        info = {k.lower(): v for k, v in line.activity.items()}
        activity_type = info.get("activity") if isinstance(info.get("activity"), str) else None
        display = info.get("displayname") if isinstance(info.get("displayname"), str) else None
        state = STATES.get(str(info.get("state") or "").lower(), "Unknown")
        args = info.get("arguments")
        variables = info.get("variables")
        short = _short(activity_type)
        private = info.get("private") is True or str(info.get("private")).lower() == "true"
        if not private and args is None and short in VALUE_BEARING:
            private = True  # Private activities arrive without arguments
            reason = "Private (no arguments logged)"
        else:
            reason = "Private"
        args = args if isinstance(args, dict) else {}
        variables = variables if isinstance(variables, dict) else {}

        selector = None
        for k in SELECTOR_KEYS:
            if k in args:
                selector = selector_info(args.get(k))
                break
        if selector is None:
            for k in SELECTOR_KEYS:
                if k.lower() in info:
                    selector = selector_info(info[k.lower()])
                    break
        values = {k: v for k, v in args.items() if k not in SELECTOR_KEYS}

        if state in ("Executing", "Unknown"):
            step = Step(
                seq=len(self.steps) + 1,
                ts=line.ts,
                activity=short,
                activity_type=activity_type,
                display=display,
                state=state,
                selector=None if private else selector,
                values=StepValues() if private else StepValues(**{"in": values}),
                variables={} if private else dict(variables),
                redacted=private,
                redaction_reason=reason if private else None,
            )
            self.steps.append(step)
            if state == "Executing":
                self.open.append(step)
            return
        step = self._match(short, display)
        if step is None:
            # A close without an open (the log started mid-activity): keep it as its own step.
            step = Step(
                seq=len(self.steps) + 1,
                ts=line.ts,
                activity=short,
                activity_type=activity_type,
                display=display,
                redacted=private,
                redaction_reason=reason if private else None,
            )
            self.steps.append(step)
        else:
            self.open.remove(step)
        step.state = state
        step.end_ts = line.ts
        if step.ts and line.ts and line.ts >= step.ts:
            step.duration_ms = int((line.ts - step.ts).total_seconds() * 1000)
        if state == "Faulted":
            step.message = line.message or None
        if private or step.redacted:
            step.redacted = True
            step.redaction_reason = step.redaction_reason or reason
            step.values = StepValues()
            step.variables = {}
            step.selector = None
            return
        if step.selector is None and selector is not None:
            step.selector = selector
        out = {k: v for k, v in values.items() if step.values.in_.get(k, object()) != v}
        step.values = StepValues(**{"in": step.values.in_, "out": {**step.values.out, **out}})
        step.variables = {**step.variables, **variables}

    def _match(self, short: str, display: str | None) -> Step | None:
        for step in reversed(self.open):
            if step.activity == short and step.display == display:
                return step
        return None

    def trace(self) -> RunTrace:
        first = self.first or _Line()
        key = self.job or f"{self.source}#{self.index}"
        return RunTrace(
            run_id=_run_id(self.started, key),
            job_id=self.job,
            robot=first.robot,
            machine=first.machine,
            process=first.process,
            process_version=first.process_version,
            started=self.started,
            ended=self.ended,
            source=self.source,
            steps=self.steps,
            unmapped_fields=sorted(self.unmapped),
            bad_lines=self.bad,
            log_lines=self.lines,
        )


def parse_text(text: str, source: str = "<text>") -> ParseResult:
    records = _records(text)
    result = ParseResult(lines=len(records))
    runs: list[_Run] = []
    current: _Run | None = None
    pending_bad = 0  # unreadable lines before any run are charged to the first run
    for record in records:
        if not isinstance(record, dict):
            result.bad_lines += 1
            if current is None:
                pending_bad += 1
            else:
                current.bad += 1
            continue
        try:
            line = _line(record)
        except Exception:  # noqa: BLE001 - never crash on content
            result.bad_lines += 1
            if current is None:
                pending_bad += 1
            else:
                current.bad += 1
            continue
        starts = bool(STARTED.search(line.message)) and line.activity is None
        new_job = (
            line.job is not None and current is not None and current.job not in (None, line.job)
        )
        if current is None or current.closed or new_job or starts and current.lines:
            current = _Run(line.job, len(runs), source)
            runs.append(current)
        if current.job is None and line.job:
            current.job = line.job
        current.add(line)
        if pending_bad:
            current.bad += pending_bad
            pending_bad = 0
        if ENDED.search(line.message) and line.activity is None:
            current.closed = True
    if pending_bad and runs:
        runs[-1].bad += pending_bad
    result.runs = [r.trace() for r in runs if r.steps or r.lines]
    return result


def parse_report(path: Path) -> ParseResult:
    path = Path(path)
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", "replace")
    return parse_text(text, str(path))


def parse(path: Path) -> list[RunTrace]:
    """An Orchestrator export or NLog JSON-lines file -> one RunTrace per job run."""
    return parse_report(path).runs
