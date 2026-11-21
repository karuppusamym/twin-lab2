# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: imports
# renamed to this repository's packages, unused paths cut. Source and changes: docs/UPSTREAM.md.
"""Durable fidelity results (replication gap R11): what a replay compared, and whether that
still holds.

A replay's report carries a `fingerprint`:

- `recording`: its id, folder, source origin, capture time and a digest of `steps.jsonl` and every
  snapshot under `dom/`;
- `files`: digests of the bindings, acceptance, baseline and visual files used (the visual digest
  covers the baseline images it names), or None when there was none;
- `browser` (name, version) and `viewport`;
- `app` (added when the report is kept for an app): conformance's app facts (description, brief,
  schema, build time, database choice), the image id when Docker knows it, and the commit.

`assess` compares a kept report with what exists now, like `conformance.assess`: any change makes
it `stale` ("out of date: <reason> — replay again"), and readiness does not count it. A report
kept before this was tracked has no fingerprint: `legacy`, "checked before this was tracked".
"""

from __future__ import annotations

import hashlib
import subprocess
from datetime import UTC, datetime
from pathlib import Path

VERSION = 1
FILES = ("bindings", "acceptance", "baseline", "visual")


def recording_digest(path: Path) -> str | None:
    """steps.jsonl and every snapshot (dom/*), in name order."""
    path = Path(path)
    steps = path / "steps.jsonl"
    if not steps.exists():
        return None
    h = hashlib.sha256(steps.read_bytes())
    dom = path / "dom"
    if dom.is_dir():
        for f in sorted(dom.rglob("*")):
            if f.is_file():
                h.update(f.relative_to(path).as_posix().encode())
                h.update(f.read_bytes())
    return h.hexdigest()[:12]


def file_digest(path: Path | None) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:12] if path else None
    except OSError:
        return None


def file_digests(rec_path: Path, used: dict[str, Path | None]) -> dict[str, dict]:
    from twinlab2.replay import visual as vis

    out = {}
    for name in FILES:
        p = used.get(name)
        if name == "visual":
            spec = vis.load(p) if p is not None and Path(p).exists() else None
            d = vis.digest(spec, rec_path, vis.pictures_of(rec_path)) if spec else None
        else:
            d = file_digest(p) if p is not None and Path(p).exists() else None
        out[name] = {"path": str(Path(p).resolve()) if p is not None else None, "digest": d}
    return out


def for_run(rec, used: dict[str, Path | None], browser: dict | None, viewport: dict | None) -> dict:
    """The fingerprint of a replay, as it ran (the app part is added when it is kept)."""
    from twinlab2.replay.baseline import origin_of

    source = next((o for o in (origin_of(s.url) for s in rec.steps) if o), None)
    captured = next((s.ts for s in rec.steps if s.ts), None)
    return {
        "version": VERSION,
        "replayed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "recording": {
            "id": rec.id,
            "path": str(Path(rec.path).resolve()),
            "digest": recording_digest(rec.path),
            "source_origin": source,
            "captured_at": captured.isoformat(timespec="seconds") if captured else None,
            "steps": len(rec.steps),
        },
        "files": file_digests(rec.path, used),
        "browser": browser,
        "viewport": viewport,
    }


def commit(root: Path) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "--short=12", "HEAD"],
                             capture_output=True, text=True, timeout=5, check=False)  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def add_app(fp: dict, root: Path, app_id: str, image: str | None = None) -> dict:
    """The replica the replay ran against: conformance's app facts, image, commit."""
    from twinlab2_contract.upstream import NotAbsorbed
    raise NotAbsorbed("Twin Lab's conformance")  # cut from the copy: docs/UPSTREAM.md


def assess(root: Path, app_id: str, report, facts=None, image: str | None = None):
    """Is a kept replay report still true of what exists now? A `conformance.Freshness` whose
    verdict is the report's scoped status (or its status, for older reports)."""
    from twinlab2_contract.upstream import NotAbsorbed
    raise NotAbsorbed("Twin Lab's conformance")  # cut from the copy: docs/UPSTREAM.md


def describe(fp: dict | None) -> str:
    """One line: what was compared."""
    if not isinstance(fp, dict):
        return "Compared: not recorded (a report from before this was tracked)"
    rec = fp.get("recording") or {}
    files = fp.get("files") or {}
    bits = [f"recording {rec.get('id')} {rec.get('digest')} (captured {rec.get('captured_at')}, "
            f"source {rec.get('source_origin')})"]  # fmt: skip
    bits += [f"{n} {(files.get(n) or {}).get('digest') or 'none'}" for n in FILES]
    b = fp.get("browser") or {}
    if b:
        bits.append(f"{b.get('name')} {b.get('version')}")
    v = fp.get("viewport") or {}
    if v:
        bits.append(f"viewport {v.get('width')}x{v.get('height')}")
    app = fp.get("app") or {}
    if app:
        bits.append(f"app {app.get('id')} commit {app.get('commit') or '?'} image "
                    f"{app.get('image') or 'unknown'} database {app.get('database')}")  # fmt: skip
    return "Compared: " + "; ".join(bits)
