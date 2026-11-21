"""Approved visual baselines: one picture per screen, recorded state, viewport, theme and scale.

A screenshot count says nothing about which screens it shows: three pictures of the invoice list
cover neither the invoice form nor its error state. The owner therefore approves each baseline
for one specific state (`needed`: every recorded state a scenario's linked event lands on), at
the viewport, theme and scale the picture was taken at, from one of:

- the recorder's own pictures (the recorder masks them before writing: excluded windows painted
  over, see docs/RECORDER-CONTRACT.md), where the recording folder is on this portal
  (`TWINLAB2_RECORDINGS`, the folder Verify replays from), one per recorded event;
- the application's Screen attachments the owner marked reviewed or synthetic.

Either way the picture goes through `redact.attachment` (image metadata stripped) before it is
stored, and enters the package as `baselines/<id>.png` with its description in evidence.json
(`visual_baselines`). The package validator computes visual coverage per state from these.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from twinlab2 import artifacts
from twinlab2 import readiness_cache
from twinlab2 import redact
from twinlab2_contract import package


def needed(db, app_id) -> list[dict]:
    """[{screen, state, number, events}] of the recorded states the scenarios' linked events land on."""
    from twinlab2 import buildpack, scenarios

    linked = {ref.split("#", 1)[1] for s in scenarios.load(db, app_id) for ref in s["events"] if "#" in ref}
    out = []
    for sc in buildpack.screen_structure(db, app_id):
        for number, st in enumerate(sc["states"], 1):
            events = [e for e in st.get("events") or [] if e in linked]
            if events:
                out.append({"screen": sc["screen"], "state": st["id"], "number": number, "events": events})
    return out


def _recording_folders(recording_id):
    from twinlab2.independent import TwinLabChecks

    runner = TwinLabChecks()
    for surface in ("web", "desktop", "mainframe"):
        folder = runner.recording(recording_id, surface)
        if folder is not None:
            yield folder


def recorder_pictures(recording_id) -> dict:
    """{event id: picture file} of the recorder's pictures in a recording folder on this portal."""
    out = {}
    for folder in _recording_folders(recording_id):
        try:
            lines = (folder / "steps.jsonl").read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                step = json.loads(line)
            except ValueError:
                continue
            pic = step.get("screenshot")
            if isinstance(pic, str) and step.get("event_id"):
                path = (folder / pic).resolve()
                if folder.resolve() in path.parents and path.is_file() and path.suffix.lower() == ".png":
                    out[step["event_id"]] = path
    return out


def candidates(db, app_id) -> list[dict]:
    """The pictures an owner may approve: per needed state, the recorder's pictures of its events,
    and every reviewed or synthetic Screen attachment (any state)."""
    out = []
    states = needed(db, app_id)
    recordings = [r["recording_id"] for r in db.execute(
        "SELECT recording_id FROM evidence WHERE app_id = ? ORDER BY recording_id", (app_id,))]
    for rec in recordings:
        pictures = recorder_pictures(rec)
        for st in states:
            for event in st["events"]:
                if event in pictures:
                    out.append({"source": "recording", "recording_id": rec, "event_id": event,
                                "screen": st["screen"], "state": st["state"],
                                "label": f"Recorder picture after event {event[:8]}… ({Path(pictures[event]).name})"})
    for row in db.execute("SELECT id, title, extension, share FROM attachments WHERE app_id = ? AND kind = 'Screen' "
                          "ORDER BY id", (app_id,)):
        share = row["share"] if "share" in row.keys() else "restricted"
        if share in ("reviewed", "synthetic") and row["extension"] in redact.IMAGE_EXTENSIONS:
            out.append({"source": "attachment", "attachment_id": row["id"], "screen": None, "state": None,
                        "label": f"Attachment {row['id']}: {redact.redact_text(row['title'])}"})
    return out


def picture(db, app_id, source) -> bytes:
    """The bytes of a candidate picture, metadata stripped; LookupError when it is not one."""
    if source.get("attachment_id") is not None:
        row = db.execute("SELECT * FROM attachments WHERE id = ? AND app_id = ? AND kind = 'Screen'",
                         (source["attachment_id"], app_id)).fetchone()
        if row is None:
            raise LookupError("no such Screen attachment")
        share = row["share"] if "share" in row.keys() else "restricted"
        content, _outcome, reasons = redact.attachment(artifacts.content(db, row, "content"), row["extension"], share)
        if content is None:
            raise LookupError("; ".join(reasons) or "the attachment cannot enter a package")
        return content
    event, rec = source.get("event_id"), source.get("recording_id")
    linked = db.execute("SELECT selection FROM evidence WHERE app_id = ? AND recording_id = ?", (app_id, rec)).fetchone()
    if linked is None or event not in json.loads(linked["selection"]):
        raise LookupError("that event is not selected for this application")
    path = recorder_pictures(rec).get(event)
    if path is None:
        raise LookupError("the recorder's picture of that event is not on this portal")
    content, _outcome, reasons = redact.attachment(path.read_bytes(), ".png", "reviewed")
    if content is None:
        raise LookupError("; ".join(reasons))
    return content


def approve(db, app_id, body, user, now) -> dict:
    """Approve (or replace) the baseline of one screen state at one viewport, theme and scale.
    ValueError for an incomplete description, LookupError for a picture that is not a candidate."""
    b = {"screen": body.get("screen"), "state": body.get("state"), "viewport": str(body.get("viewport") or ""),
         "theme": body.get("theme"), "scale": str(body.get("scale") or "")}
    problems = package.check_baseline(b)
    if problems:
        raise ValueError("To approve a baseline, " + "; ".join(problems) + ".")
    if not any(n["screen"] == b["screen"] and n["state"] == b["state"] for n in needed(db, app_id)):
        raise ValueError("That screen state is not one the scenarios reach.")
    source = body.get("source") or {}
    content = picture(db, app_id, source)
    kind = "attachment" if source.get("attachment_id") is not None else "recording"
    with db:
        readiness_cache.forget(db, app_id)
        db.execute("DELETE FROM visual_baselines WHERE app_id = ? AND screen = ? AND state_id = ? AND viewport = ? "
                   "AND theme = ? AND scale = ?", (app_id, b["screen"], b["state"], b["viewport"], b["theme"], b["scale"]))
        new = db.execute(
            "INSERT INTO visual_baselines(app_id, screen, state_id, viewport, theme, scale, source, attachment_id, "
            "recording_id, event_id, content, sha256, approved_by, approved_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (app_id, b["screen"], b["state"], b["viewport"], b["theme"], b["scale"], kind, source.get("attachment_id"),
             source.get("recording_id"), source.get("event_id"), content, hashlib.sha256(content).hexdigest(),
             user, now)).fetchone()[0]
    return next(x for x in approved(db, app_id) if x["id"] == new)


def remove(db, app_id, baseline_id):
    with db:
        readiness_cache.forget(db, app_id)
        if not db.execute("DELETE FROM visual_baselines WHERE id = ? AND app_id = ?", (baseline_id, app_id)).rowcount:
            raise LookupError(baseline_id)


def approved(db, app_id) -> list[dict]:
    return [{"id": r["id"], "screen": r["screen"], "state": r["state_id"], "viewport": r["viewport"],
             "theme": r["theme"], "scale": r["scale"], "source": r["source"], "attachment_id": r["attachment_id"],
             "recording_id": r["recording_id"], "event_id": r["event_id"], "sha256": r["sha256"],
             "approved_by": r["approved_by"], "approved_at": r["approved_at"]}
            for r in db.execute("SELECT * FROM visual_baselines WHERE app_id = ? ORDER BY screen, state_id, viewport, "
                                "theme, scale", (app_id,))]


def for_pack(db, app_id) -> tuple[list[dict], dict]:
    """(evidence.json `visual_baselines`, {path: bytes}) of the approved baselines."""
    entries, files = [], {}
    for r in db.execute("SELECT * FROM visual_baselines WHERE app_id = ? ORDER BY screen, state_id, viewport, theme, "
                        "scale", (app_id,)):
        path = f"baselines/{r['id']}.png"
        files[path] = bytes(r["content"])
        entries.append({"screen": r["screen"], "state": r["state_id"], "viewport": r["viewport"], "theme": r["theme"],
                        "scale": r["scale"], "path": path, "sha256": r["sha256"], "source": r["source"]})
    return entries, files
