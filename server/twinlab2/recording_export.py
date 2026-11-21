"""One download for every application a recording covers: their builder-approved packages.

Plain functions over a database connection and the caller's user name: no web framework, so
any route layer (Flask today, FastAPI next) can serve it.

A workstation recording can cover many applications (web, desktop, mainframe); each has its own
builder package (`packages`, `buildpack`). `export(db, recording_id, user)` builds one ZIP:

    manifest.json
    <app slug>/<app slug>-rev<N>-<revision12>.zip    one folder per application, its latest
                                                    frozen package, the stored bytes as approved

Only builder-approved (frozen) packages go in, and only of applications on which `user` holds
the builder role or higher (owner). Every other application the recording covers is listed in
the manifest with its name, slug and kind and no content: `not_available` when the user is not
its builder, `no_package` when nothing is frozen for it yet. A package whose stored bytes do not
match their approved SHA-256 is never exported (`ValueError`).

The archive is deterministic (sorted names, fixed ZIP times, sorted JSON without times): the same
catalog state and user give the same bytes. The caller checks that the user may see the
recording at all and records the download (`package.download` per included package), as the
single-package download does.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

from twinlab2 import buildpack
from twinlab2 import packages

FORMAT = "twinlab2-recording-export/1"
KINDS = ("web", "desktop", "mainframe")
BUILDS = ("builder", "owner")  # the roles that are given builder packages
INCLUDED, NOT_AVAILABLE, NO_PACKAGE = "included", "not_available", "no_package"
NOTES = {INCLUDED: "The latest builder-approved package.",
         NOT_AVAILABLE: "Not available to you: you are not a builder of this application.",
         NO_PACKAGE: "No builder-approved package is frozen for this application yet."}


@dataclass
class Export:
    filename: str  # `<recording id>-packages-<sha12>.zip`
    data: bytes  # the ZIP
    manifest: dict  # manifest.json as a dict
    sha256: str  # of `data`
    included: list  # [(app_id, rev)] of the packages in the ZIP, for the caller's audit records


def kind_of(db, app):
    """web, desktop or mainframe: the application's surface, else that of its first identity."""
    if app["surface"] in KINDS:
        return app["surface"]
    row = db.execute("SELECT key FROM app_identities WHERE app_id = ? ORDER BY key LIMIT 1", (app["id"],)).fetchone()
    if row is None:
        return "desktop"
    return "web" if row[0].startswith("web:") else "mainframe" if row[0].startswith("mainframe:") else "desktop"


def is_builder(db, app_id, user):
    """Whether `user` holds builder or owner on the application (`app_roles`, as auth.ROLES ranks
    them). Read here rather than through `auth`, so the module needs no web framework."""
    if not user:
        return False
    row = db.execute("SELECT role FROM app_roles WHERE app_id = ? AND user_name = ?", (app_id, user)).fetchone()
    return row is not None and row[0] in BUILDS


def export(db, recording_id: str, user: str) -> Export:
    """The packages of the applications `recording_id` covers, for `user` (see the module).
    LookupError when there is no such recording; ValueError when a stored package does not match
    its approved SHA-256."""
    recording = db.execute("SELECT id, name FROM recordings WHERE id = ?", (recording_id,)).fetchone()
    if recording is None:
        raise LookupError(recording_id)
    steps = {}
    for row in db.execute("SELECT app_id, flow FROM recording_apps WHERE recording_id = ?", (recording_id,)):
        steps[row["app_id"]] = steps.get(row["app_id"], 0) + len(json.loads(row["flow"]))
    for row in db.execute("SELECT app_id FROM evidence WHERE recording_id = ?", (recording_id,)):
        steps.setdefault(row["app_id"], 0)
    entries, files, included = [], {}, []
    for app_id in sorted(steps):
        app = db.execute("SELECT id, name, surface FROM apps WHERE id = ?", (app_id,)).fetchone()
        if app is None:
            continue
        slug = packages.slug(app)
        entry = {"app_id": app_id, "name": app["name"], "slug": slug, "kind": kind_of(db, app)}
        builder = is_builder(db, app_id, user)
        row = packages.latest(db, app_id) if builder else None
        if not builder:
            entry["status"] = NOT_AVAILABLE
        elif row is None:
            entry.update(status=NO_PACKAGE, step_count=steps[app_id])
        else:
            data = packages.open_package(db, row["storage_ref"])
            if hashlib.sha256(data).hexdigest() != row["sha256"]:
                raise ValueError(f"The stored package of application {app_id} (revision {row['rev']}) "
                                 "does not match its approved SHA-256.")
            path = f"{slug}/{packages.download_name(app, row)}"
            files[path] = data
            included.append((app_id, row["rev"]))
            manifest_files = buildpack.read(data)
            evidence = json.loads(manifest_files.get("evidence.json", b"{}"))
            entry.update(status=INCLUDED, path=path, package_revision=row["rev"], revision=row["revision"],
                         sha256=row["sha256"], size=len(data), approved_by=row["approved_by"],
                         approved_at=row["approved_at"], step_count=steps[app_id],
                         includes_this_recording=any(r.get("recording_id") == recording_id
                                                     for r in evidence.get("evidence", [])))
        entry["note"] = NOTES[entry["status"]]
        entries.append(entry)
    entries.sort(key=lambda e: e["slug"])
    manifest = {"format": FORMAT, "recording": {"id": recording["id"], "name": recording["name"]},
                "applications": entries,
                "counts": {status: sum(1 for e in entries if e["status"] == status)
                           for status in (INCLUDED, NOT_AVAILABLE, NO_PACKAGE)}}
    text = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    data = buildpack.archive({"manifest.json": text.encode("utf-8"), **dict(sorted(files.items()))})
    digest = hashlib.sha256(data).hexdigest()
    return Export(f"{packages.slug({'id': 'packages', 'name': recording_id})}-{digest[:12]}.zip", data, manifest,
                  digest, included)
