# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: imports
# renamed to this repository's packages, unused paths cut. Source and changes: docs/UPSTREAM.md.
"""One timeline from a session folder, and the references a portal may receive.

A session folder (twinlab2_recorder.workstation.session) holds `session.json`, `journal.jsonl` (what the
session itself saw: applications and their levels, presence rows, pages outside the targets),
and a recording per recorder in today's formats (`desktop/`, `web/`). The recorders finalise
their own steps differently (the web recorder renumbers at close, the desktop recorder writes a
step after its batch settles), so the session numbers nothing while it runs: `build()` merges
the three by time, each recording keeping its own order, and writes:

- `session.jsonl`: one row per step: `{"global_seq", "ts", "app", "surface", "level",
  "recording", "seq", "action", "event_id", "channel", "captured_at"}`; an `app-switch` row
  between rows of two applications (`from`, `to`: identity keys, or `presence`); consecutive
  presence rows are one row;
- `portal-outbox.json`: the references (`outbox()`), recorder contract `CONTRACT_VERSION`: the
  session, the applications recorded at full or steps level, each one's flow of filtered labels
  (twinlab2_recorder.workstation.flow; a web step under a target's path prefix keeps that prefix
  in its screen, contract 1.2), the timeline as runs of applications, the completeness manifest
  (twinlab2_recorder.workstation.completeness) and the preflight checks when there were some. No value,
  title, tree, picture, time of a presence row or name of a presence application.

`event_id` (a UUIDv7 given when the event was captured, twinlab2_recorder.workstation.journal) is how an
event is referred to: `global_seq` and `seq` are display orders, computed again at every build
(a recovered or re-merged session numbers differently), and nothing refers to an event by them.
A step recorded before event ids (contract 1.0) has none, and its rows and labels carry none.
"""

from __future__ import annotations

import heapq
import json
from datetime import datetime
from pathlib import Path

from twinlab2_recorder.workstation import flow
from twinlab2_recorder.workstation.levels import FULL, PRESENCE, STEPS, origin_of, web_path

FORMAT = "twinlab-workstation-session/1"
# The recorder contract the references follow (twin-lab2 RECORDER-CONTRACT.md).
CONTRACT_VERSION = "1.2.0"
EVENT_FIELDS = ("event_id", "channel", "captured_at")


def _event(source: dict) -> dict:
    return {k: source[k] for k in EVENT_FIELDS if source.get(k)}


def _t(ts: object) -> float:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def read_jsonl(path: Path) -> list[dict]:
    """Rows of a JSON Lines file; a last line cut short by a crash is left out."""
    if not path.is_file():
        return []
    rows = []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            if i == len(lines) - 1:
                break
            raise
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _streams(folder: Path) -> tuple[list[dict], dict]:
    meta = json.loads((folder / "session.json").read_text(encoding="utf-8"))
    journal = read_jsonl(folder / "journal.jsonl")
    apps: dict[str, dict] = dict(meta.get("apps") or {})
    pid_key: dict[int, str] = {}
    web_targets = {t["origin"]: t for t in meta.get("targets") or [] if t.get("origin")}
    levels = {o: t.get("level", FULL) for o, t in web_targets.items()}
    rows: list[list[tuple]] = [[], [], [], []]
    for i, j in enumerate(journal):
        kind = j.get("kind")
        if kind == "app":
            apps.setdefault(
                j["key"], {k: v for k, v in j.items() if k not in ("kind", "ts", "pid")}
            )
            if isinstance(j.get("pid"), int):
                pid_key[j["pid"]] = j["key"]
        elif kind in ("presence", "outside"):
            rows[0].append((_t(j.get("ts")), 0, i, {"ts": j.get("ts"), "app": PRESENCE,
                                                    "surface": None, "level": PRESENCE,
                                                    "recording": None, "seq": None,
                                                    "action": "presence", **_event(j)}))  # fmt: skip
    last_desk = None
    by_exe = {
        str(a.get("exe") or "").lower(): k for k, a in apps.items() if a.get("surface") == "desktop"
    }
    for k, s in enumerate(read_jsonl(folder / "desktop" / "steps.jsonl")):
        # A window that closed, or a message box read after it went, has no process any more:
        # it is the application's whose step came before it.
        key = (pid_key.get(s.get("pid")) or by_exe.get(str(s.get("process") or "").lower())
               or last_desk or next(iter(by_exe.values()), None))  # fmt: skip
        if key is None:
            continue
        last_desk = key
        level = (apps.get(key) or {}).get("level") or s.get("level") or FULL
        rows[1].append((_t(s.get("ts")), 1, k, {"ts": s.get("ts"), "app": key, "surface": "desktop",
                                                "level": level, "recording": "desktop",
                                                "seq": s.get("seq"), "action": s.get("action"),
                                                **_event(s), "_step": s}))  # fmt: skip
    last_web = None
    parts = [folder / "web"] + sorted(
        (p for p in folder.glob("web-*") if p.name[4:].isdigit()), key=lambda p: int(p.name[4:])
    )
    web_steps = [dict(s, _part=p.name) for p in parts for s in read_jsonl(p / "steps.jsonl")]
    for k, s in enumerate(web_steps):
        origin = origin_of(str(s.get("url") or "")) or origin_of(str(s.get("origin") or ""))
        if not origin:
            if str(s.get("action") or "").startswith("tab-") and last_web:
                origin = last_web
            else:
                continue  # the blank page the browser starts on: setup, no application's
        last_web = origin
        key = f"web:{origin}"
        level = levels.get(origin, FULL)
        if key not in apps:
            t = web_targets.get(origin) or {}
            apps[key] = {"key": key, "surface": "web", "origin": origin, "level": level,
                         "display": t.get("name") or origin.split("://", 1)[-1]}  # fmt: skip
            if isinstance(t.get("app_id"), int):
                apps[key]["app_id"] = t["app_id"]
        rows[2].append((_t(s.get("ts")), 2, k, {"ts": s.get("ts"), "app": key, "surface": "web",
                                                "level": level, "recording": s["_part"],
                                                "seq": s.get("seq"), "action": s.get("action"),
                                                **_event(s), "_step": dict(s, surface="web")}))  # fmt: skip
    for k, s in enumerate(read_jsonl(folder / "mainframe" / "steps.jsonl")):
        key = s.get("app")
        if not key:
            continue
        if key not in apps:
            apps[key] = {"key": key, "surface": "mainframe", "level": FULL, "display": key[10:]}
        row = {"ts": s.get("ts"), "app": key, "surface": "mainframe",
               "level": apps[key].get("level") or FULL, "recording": "mainframe",
               "seq": s.get("seq"), "action": s.get("action"), **_event(s), "_step": s}  # fmt: skip
        rows[3].append((_t(s.get("ts")), 3, k, row))
    merged = [r[3] for r in heapq.merge(*rows, key=lambda r: (r[0], r[1], r[2]))]
    meta["apps"] = apps
    return merged, meta


def build(folder: Path) -> dict:
    """Write `session.jsonl` and `portal-outbox.json`; return the outbox."""
    folder = Path(folder)
    merged, meta = _streams(folder)
    out: list[dict] = []
    prev = None
    for row in merged:
        if row["app"] == PRESENCE and prev == PRESENCE:
            continue
        if prev is not None and row["app"] != prev:
            out.append({"ts": row["ts"], "action": "app-switch", "from": prev, "to": row["app"]})
        out.append(row)
        prev = row["app"]
    for n, row in enumerate(out, 1):
        row["global_seq"] = n
    with (folder / "session.jsonl").open("w", encoding="utf-8") as f:
        for row in out:
            f.write(json.dumps({k: v for k, v in row.items() if not k.startswith("_")},
                               ensure_ascii=False) + "\n")  # fmt: skip
    box = outbox(meta, out)
    from twinlab2_recorder.workstation import completeness

    box["completeness"] = completeness.manifest(folder, meta)
    pre = completeness.preflight(meta)
    if pre is not None:
        box["preflight"] = pre
    cov = folder / "robot" / "coverage.json"
    if cov.is_file():  # the robot's own log was read beside the session: its counts only
        c = json.loads(cov.read_text(encoding="utf-8"))
        box["robot"] = {"activities": c.get("activities", 0), "ui_activities": c.get("ui_activities", 0),
                        "covered": c.get("covered", 0), "gaps": len(c.get("gaps") or [])}  # fmt: skip
    (folder / "portal-outbox.json").write_text(json.dumps(box, indent=1, ensure_ascii=False),
                                               encoding="utf-8")  # fmt: skip
    return box


def read_references(path: Path) -> dict:
    """A `portal-outbox.json` of any contract 1.x: one written before 1.1.0 reads as
    `contract_version` 1.0.0 with no completeness manifest (what it missed is not known)."""
    box = json.loads(Path(path).read_text(encoding="utf-8"))
    box.setdefault("contract_version", "1.0.0")
    box.setdefault("completeness", None)
    return box


APP_FACTS = ("key", "surface", "level", "display", "exe", "company", "product", "version",
             "origin", "toolkit", "app_id")  # fmt: skip


def target_prefixes(meta: dict) -> dict[str, list[str]]:
    """{origin: the path prefixes the session's web targets name on it} (flow.route_template)."""
    out: dict[str, set[str]] = {}
    for t in meta.get("targets") or []:
        if not isinstance(t, dict):
            continue
        origin = origin_of(str(t.get("origin") or "")) or origin_of(str(t.get("key") or "")[4:])
        path = web_path(t.get("path")) if t.get("path") else None
        if origin and path:
            out.setdefault(origin, set()).add(path)
    return {o: sorted(p) for o, p in out.items()}


def outbox(meta: dict, rows: list[dict]) -> dict:
    flows: dict[str, list[dict]] = {}
    prefixes = target_prefixes(meta)
    for row in rows:
        step = row.get("_step")
        if step is None or row.get("level") not in (FULL, STEPS):
            continue
        on = (prefixes.get(origin_of(str(step.get("url") or ""))) or ()) if row.get("surface") == "web" else ()
        label = flow.label_step(step, row["global_seq"], (meta.get("apps") or {}).get(row["app"]), on)
        flows.setdefault(row["app"], []).append(label | _event(row))
    apps = []
    for key, facts in (meta.get("apps") or {}).items():
        if facts.get("level") not in (FULL, STEPS) or key not in flows:
            continue
        apps.append({k: facts.get(k) for k in APP_FACTS if facts.get(k) is not None} | {"key": key})
    sent = {a["key"] for a in apps}
    timeline: list[dict] = []
    for row in rows:
        if row.get("action") == "app-switch":
            continue
        app = row["app"] if row.get("level") in (FULL, STEPS) and row["app"] in sent else PRESENCE
        if timeline and timeline[-1]["app"] == app:
            timeline[-1]["steps"] += 1
        else:
            timeline.append({"app": app, "steps": 1})
    session = {k: meta.get(k) for k in ("id", "name", "started", "ended", "state", "recorder",
                                        "twinlab_commit", "automation_id", "portal_session_id")
               if meta.get(k) is not None}  # fmt: skip
    session["location"] = {"kind": "local", "path": f"recordings/{meta.get('id')}"}
    session["targets"] = [{k: t[k] for k in ("key", "exe", "origin", "path", "level", "app_id") if k in t}
                          for t in meta.get("targets") or []]  # fmt: skip
    return {"contract_version": CONTRACT_VERSION, "format": "twinlab-references/1",
            "session": session, "apps": apps, "flows": flows, "timeline": timeline}  # fmt: skip
