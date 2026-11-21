"""The recorder releases this portal offers (GET /api/agent/update; routes_registry).

An administrator puts the MSI and a `latest.json` beside it in the folder
`TWINLAB2_RECORDER_RELEASES` names (on OpenShift a mounted volume or a ConfigMap-fed folder):

    {"version": "0.3.0", "file": "TwinLabRecorder-0.3.0-x64.msi",
     "sha256": "<the MSI's SHA-256, hex>", "notes": "What changed", "published_at": "2026-11-21T10:00:00Z"}

The portal checks the file before it offers it: the name is a recorder MSI's, it is in the
folder, and its SHA-256 is the one `latest.json` announces (worked out once per file size and
time). A release that does not check out is not offered (and logged), so a half-copied MSI never
reaches a recorder. Signing is the office's: the recorder checks the MSI's Authenticode signature
against its machine policy (twinlab2_recorder.updater).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)
FILE_RE = re.compile(r"TwinLabRecorder-\d+\.\d+\.\d+(-x64)?\.msi")
VERSION_RE = re.compile(r"\d+\.\d+\.\d+")
_checked: dict[tuple, str] = {}


def folder() -> Path | None:
    value = os.environ.get("TWINLAB2_RECORDER_RELEASES", "").strip()
    return Path(value) if value else None


def version_key(text: str | None) -> tuple[int, ...] | None:
    return tuple(int(x) for x in text.split(".")) if isinstance(text, str) and VERSION_RE.fullmatch(text) else None


def _sha256(path: Path) -> str:
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    if key not in _checked:
        digest = hashlib.sha256()
        with path.open("rb") as f:
            while chunk := f.read(1024 * 1024):
                digest.update(chunk)
        _checked[key] = digest.hexdigest()
    return _checked[key]


def latest() -> dict | None:
    """The offered release: {version, file, path, size, sha256, notes, published_at}, or None."""
    base = folder()
    if base is None or not (base / "latest.json").is_file():
        return None
    try:
        doc = json.loads((base / "latest.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        log.warning("recorder releases: %s/latest.json cannot be read", base)
        return None
    if not isinstance(doc, dict) or not version_key(doc.get("version")):
        log.warning("recorder releases: latest.json has no version x.y.z")
        return None
    name = doc.get("file")
    if not isinstance(name, str) or not FILE_RE.fullmatch(name) or not (base / name).is_file():
        log.warning("recorder releases: latest.json names no recorder MSI in the folder")
        return None
    path = base / name
    announced = str(doc.get("sha256") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", announced) or _sha256(path) != announced:
        log.warning("recorder releases: %s does not have the SHA-256 latest.json announces: not offered", name)
        return None
    published = doc.get("published_at") if isinstance(doc.get("published_at"), str) else None
    return {"version": doc["version"], "file": name, "path": path, "size": path.stat().st_size,
            "sha256": announced, "notes": str(doc.get("notes") or "")[:2000], "published_at": published}  # fmt: skip


def answer(current: str | None) -> dict:
    """The body of GET /api/agent/update (RecorderUpdate)."""
    rel = latest()
    if rel is None:
        return {"current": current or None, "update_available": False, "latest": None}
    have, offered = version_key(current), version_key(rel["version"])
    public = {k: rel[k] for k in ("version", "file", "size", "sha256", "notes", "published_at")}
    public["url"] = f"/api/agent/update/{rel['file']}"
    return {"current": current or None, "update_available": bool(have and offered > have), "latest": public}
