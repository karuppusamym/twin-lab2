# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: imports
# renamed to this repository's packages, unused paths cut. Source and changes: docs/UPSTREAM.md.
"""The UiPath robot's own log, read beside a session: which activity ran when, and whether each
UI activity left a recorded step.

UiPath writes `%LOCALAPPDATA%\\UiPath\\Logs\\<date>_Execution.log` on the robot's PC; at the
*Verbose* logging level (UiPath Assistant: Settings > Logging level) every activity is logged as
it starts and closes. A line is `HH:MM:SS.ffff Level {json}`, the JSON in the shape
twinlab2_recorder.botlog.robotlog reads. At the end of a session the lines written while it ran are read
and two files are written beside the recording:

- `robot/steps.jsonl`: one row per activity: `{"seq", "ts", "end_ts", "activity", "display",
  "state", "app", "surface", "duration_ms"}`. Never a value, an argument or a variable: the
  display name is the developer's, with every token holding a digit masked; `app` is the
  selector's executable or the browser page's origin, `surface` web, desktop or mainframe;
- `robot/coverage.json`: each UI activity (typing, clicking, selecting, a terminal key...) and
  whether a step of a target application was recorded while it ran (from 0.5 s before it started
  to 2 s after it closed): `covered`, `gaps` (activity, display, time). A gap says what the
  recording missed: a Simulate click, an application that was not a target, a terminal that did
  not go through the proxy.
  `links` names, for each covered activity, the recorded events by their `event_id`.

Nothing of this goes to the portal except the counts (`timeline.outbox`).
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

from twinlab2_recorder.workstation.levels import origin_of

LINE = re.compile(r"^\s*\d{1,2}:\d{2}:\d{2}(?:\.\d+)?\s+\w+\s+(\{.*\})\s*$")
DIGIT_TOKEN = re.compile(r"\S*\d\S*")
UI_ACTIVITIES = re.compile(
    r"(TypeInto|Type|Click|DoubleClick|SelectItem|Check|SetText|SendHotkey|Hover|Navigate|GoTo|"
    r"SetValue|Terminal|SendKeys|SendControlKey|SetField|MoveCursor|NClick|NTypeInto|NCheck|"
    r"NSelectItem|NKeyboardShortcuts|NGoToUrl)",
    re.I,
)  # fmt: skip
SLACK_BEFORE, SLACK_AFTER = 0.5, 2.0


def default_log_dir() -> Path | None:
    local = os.environ.get("LOCALAPPDATA")
    return Path(local) / "UiPath" / "Logs" if local else None


def _json_lines(text: str) -> str:
    """The NLog text layout (`time level {json}`) as JSON lines; JSON lines stay as they are."""
    out = []
    for line in text.splitlines():
        m = LINE.match(line)
        if m:
            out.append(m.group(1))
        elif line.strip().startswith("{"):
            out.append(line.strip())
    return "\n".join(out)


def _t(value) -> float | None:
    if isinstance(value, datetime):
        return value.timestamp()
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    from datetime import UTC

    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _mask(text: str | None) -> str | None:
    if not text:
        return None
    return DIGIT_TOKEN.sub(lambda m: re.sub(r"\d", "9", m.group(0)), str(text))[:80]


def read(started: str, ended: str, logs: list[Path] | None = None) -> list[dict]:
    """Activities logged between `started` and `ended` (ISO times), from the given log files or
    from the robot's log folder (files changed since the session started)."""
    from twinlab2_recorder.botlog import robotlog

    t0, t1 = _t(started), _t(ended)
    if t0 is None or t1 is None:
        return []
    if logs is None:
        folder = default_log_dir()
        logs = []
        if folder is not None and folder.is_dir():
            logs = [p for p in folder.glob("*Execution*.log") if p.stat().st_mtime >= t0 - 60]
    rows: list[dict] = []
    for path in sorted(logs):
        try:
            text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        for run in robotlog.parse_text(_json_lines(text), str(path)).runs:
            for step in run.steps:
                ts = _t(step.ts)
                if ts is None or not (t0 - 1 <= ts <= t1 + 1):
                    continue
                sel = step.selector or {}
                app = None
                surface = None
                if "Terminal" in (step.activity_type or step.activity or ""):
                    surface = "mainframe"
                elif (
                    sel.get("app")
                    and str(sel["app"]).lower().endswith((".exe",))
                    and "html" not in sel.get("raw", "")[:8]
                ):
                    app, surface = str(sel["app"]).lower(), "desktop"
                elif sel.get("raw", "").lstrip().startswith("<html"):
                    surface = "web"
                    m = re.search(r"url='([^']*)'", sel["raw"])
                    app = origin_of(m.group(1)) if m else None
                rows.append({"ts": _iso(ts), "end_ts": _iso(_t(step.end_ts)), "activity": step.activity,
                             "display": _mask(step.display), "state": step.state, "app": app,
                             "surface": surface, "duration_ms": step.duration_ms})  # fmt: skip
    rows.sort(key=lambda r: r["ts"] or "")
    for n, r in enumerate(rows, 1):
        r["seq"] = n
    return rows


def coverage(activities: list[dict], recorded: list[dict]) -> dict:
    """Each UI activity, and whether a recorded step of a target fell while it ran."""
    timed = [(t, r.get("event_id")) for r in recorded if (t := _t(r.get("ts"))) is not None]
    covered, gaps, links = 0, [], []
    ui = [a for a in activities if UI_ACTIVITIES.search(a.get("activity") or "")]
    for a in ui:
        start = _t(a.get("ts"))
        end = _t(a.get("end_ts")) or start
        if start is None:
            continue
        lo, hi = start - SLACK_BEFORE, (end or start) + SLACK_AFTER
        hits = [eid for t, eid in timed if lo <= t <= hi]
        if hits:
            covered += 1
            ids = [e for e in hits if e]
            if ids:  # the recorded events by their permanent id, never by position
                links.append({"seq": a.get("seq"), "event_ids": ids})
        else:
            gaps.append(
                {k: a.get(k) for k in ("seq", "activity", "display", "ts", "surface", "app")}
            )
    return {"activities": len(activities), "ui_activities": len(ui), "covered": covered,
            "gaps": gaps, "links": links}  # fmt: skip


def attach(folder: Path, logs: list[Path] | None = None) -> dict | None:
    """Read the robot's log for a finished session folder; write robot/steps.jsonl and
    robot/coverage.json. None when the robot logged no activity in that time."""
    folder = Path(folder)
    meta = json.loads((folder / "session.json").read_text(encoding="utf-8"))
    rows = read(meta.get("started") or "", meta.get("ended") or "", logs)
    if not rows:
        return None
    out = folder / "robot"
    out.mkdir(exist_ok=True)
    (out / "steps.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    recorded = []
    for line in (
        (folder / "session.jsonl").read_text(encoding="utf-8").splitlines()
        if (folder / "session.jsonl").is_file()
        else []
    ):
        row = json.loads(line)
        if row.get("app") not in (None, "presence") and row.get("action") != "app-switch":
            recorded.append(row)
    cov = coverage(rows, recorded)
    (out / "coverage.json").write_text(json.dumps(cov, indent=1), encoding="utf-8")
    return cov
