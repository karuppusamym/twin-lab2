"""Builder-approved packages: the only artifact that may go to the builder.

Two artifact classes are kept apart:

- **restricted evidence** — recordings, their flows and uploaded attachments. Visible only to the
  application's owners (`auth`); uploading one never authorises sharing it;
- **builder-approved package** — a frozen ZIP of reviewed, selected evidence plus synthetic
  examples, after `redact` (see `buildpack.draft`). Builders and build jobs get only these.

Freezing needs the owner's explicit approval of the exact bytes they previewed: the approval
names the package's SHA-256, and `freeze` refuses when the current draft differs. Each freeze is
an immutable revision (rev 1, 2, …) with who approved it, when, the SHA-256, the pre-freeze
report, a change summary and a diff against the previous revision. Revisions are never updated
or deleted (code inserts only; SQLite triggers refuse changes).

The ZIP itself is stored by `put_package` / `open_package` as an `artifacts` object of kind
`builder_package` (local disk or S3), indexed by `brief_packages.artifact_id`; packages stored
before that are read from the old blob column (`artifacts.content`).
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import unicodedata

from twinlab2 import artifacts
from twinlab2 import audit
from twinlab2 import buildpack
from twinlab2 import pack_validator

# The table and its guards are created by migrations.py (6: roles and audit).

DIFF_LIMIT = 200_000


def migrate(db):
    """builder_packages and its immutability guards (migration 6), with every other pending
    migration. brief_packages rows move their blob to the artifact store, so only
    builder_packages is guarded; a package's bytes are checked against its sha256."""
    from twinlab2 import migrations
    migrations.run(db)


# -- storage: the one place to switch to artifacts.put(kind="builder_package") -------------------


def put_package(db, app_id, revision, data: bytes, manifest: str | None = None) -> str:
    """Store the package bytes as a `builder_package` artifact, once per revision and bytes;
    returns the reference `open_package` takes. The revision is the manifest's hash: the same
    manifest packed by a newer portal (BUILD_TASK.md worded differently) is other bytes, kept
    under `<revision>.<their sha-256>`, so an earlier revision's bytes are never served for it."""
    key = revision
    row = db.execute("SELECT archive, artifact_id FROM brief_packages WHERE revision = ?", (key,)).fetchone()
    if row is not None and hashlib.sha256(artifacts.content(db, row, "archive")).hexdigest() !=             hashlib.sha256(data).hexdigest():
        key = f"{revision}.{hashlib.sha256(data).hexdigest()[:16]}"
        row = db.execute("SELECT 1 FROM brief_packages WHERE revision = ?", (key,)).fetchone()
        if row is None:  # brief_packages.revision references briefs: the same brief under this key
            if manifest is None:
                manifest = db.execute("SELECT manifest FROM briefs WHERE revision = ?", (revision,)).fetchone()[0]
            db.execute("INSERT INTO briefs(revision, app_id, manifest) VALUES (?, ?, ?) ON CONFLICT (revision) DO NOTHING",
                       (key, app_id, manifest))
    if row is None:
        ref = artifacts.save(db, artifacts.put(data, kind="builder_package", app_id=app_id,
                                               content_type="application/zip"))
        db.execute("INSERT INTO brief_packages(revision, archive, artifact_id) VALUES (?, ?, ?)",
                   (key, b"", ref.id))
    return f"brief_packages:{key}"


def stored_row(db, storage_ref: str):
    kind, _, key = storage_ref.partition(":")
    if kind != "brief_packages":
        raise LookupError(storage_ref)
    row = db.execute("SELECT archive, artifact_id FROM brief_packages WHERE revision = ?", (key,)).fetchone()
    if row is None:
        raise LookupError(storage_ref)
    return row


def open_package(db, storage_ref: str) -> bytes:
    return artifacts.content(db, stored_row(db, storage_ref), "archive")


# -- names ----------------------------------------------------------------------------------------


def slug(app) -> str:
    """The application's name as a file and folder name, unique per application: lower case,
    accents folded (`Zählerstand` -> `zahlerstand`), letters of other scripts kept, every other
    run of characters one `-`, at most 60 characters, then `-<id>`: `billing-admin-2`,
    `請求-システム-4`. Safe on Windows, macOS and Linux and in a ZIP."""
    kept = []
    for c in unicodedata.normalize("NFKD", str(app["name"] or "")):
        if unicodedata.combining(c) and kept and kept[-1].isascii():
            continue  # an accent on a Latin letter; other scripts keep theirs (ガ stays ガ)
        kept.append(c)
    text = unicodedata.normalize("NFKC", "".join(kept)).casefold()
    text = re.sub(r"[^\w]+|_+", "-", text).strip("-")[:60].strip("-") or "app"
    return f"{text}-{app['id']}"


def download_name(app, row) -> str:
    """The file name of a frozen revision `row` of the application:
    `<slug>-rev<N>-<first 12 of the content revision>.zip`."""
    return f"{slug(app)}-rev{row['rev']}-{row['revision'][:12]}.zip"


# -- revisions ----------------------------------------------------------------------------------


def revisions(db, app_id):
    return db.execute("SELECT * FROM builder_packages WHERE app_id = ? ORDER BY rev DESC", (app_id,)).fetchall()


def get(db, app_id, rev):
    return db.execute("SELECT * FROM builder_packages WHERE app_id = ? AND rev = ?", (app_id, rev)).fetchone()


def latest(db, app_id):
    return db.execute("SELECT * FROM builder_packages WHERE app_id = ? ORDER BY rev DESC LIMIT 1",
                      (app_id,)).fetchone()


def by_revision(db, app_id, revision):
    return db.execute("SELECT * FROM builder_packages WHERE app_id = ? AND revision = ? ORDER BY rev DESC LIMIT 1",
                      (app_id, revision)).fetchone()


def changes(old_files: dict, new_files: dict, old_manifest: dict | None, new_manifest: dict):
    """(summary lines, unified diff text) from the previous revision to the new one."""
    lines = []
    if old_manifest is None:
        lines.append("First revision.")
    else:
        def names(manifest):
            return {s["name"]: s for s in manifest.get("scenarios", [])}

        old_s, new_s = names(old_manifest), names(new_manifest)
        for name in sorted(new_s.keys() - old_s.keys()):
            lines.append(f"Scenario added: {name}")
        for name in sorted(old_s.keys() - new_s.keys()):
            lines.append(f"Scenario removed: {name}")
        for name in sorted(old_s.keys() & new_s.keys()):
            if old_s[name] != new_s[name]:
                lines.append(f"Scenario changed: {name}")

        def events(manifest):
            return {f"{r['recording_id']}#{e['seq']}" for r in manifest.get("evidence", []) for e in r["events"]}

        added, removed = events(new_manifest) - events(old_manifest), events(old_manifest) - events(new_manifest)
        if added:
            lines.append(f"{len(added)} recorded event{'s' if len(added) != 1 else ''} added")
        if removed:
            lines.append(f"{len(removed)} recorded event{'s' if len(removed) != 1 else ''} removed")
        for path in sorted(new_files.keys() - old_files.keys()):
            lines.append(f"File added: {path}")
        for path in sorted(old_files.keys() - new_files.keys()):
            lines.append(f"File removed: {path}")
        for path in sorted(old_files.keys() & new_files.keys()):
            if old_files[path] != new_files[path] and path not in ("evidence.json", "BUILD_TASK.md"):
                lines.append(f"File changed: {path}")
        if not lines:
            lines.append("Only the revision references changed.")
    diff = []
    for path in sorted(old_files.keys() | new_files.keys()):
        before, after = old_files.get(path), new_files.get(path)
        if before == after:
            continue
        if buildpack.is_text(path):
            a = before.decode("utf-8", "replace").splitlines(keepends=True) if before is not None else []
            b = after.decode("utf-8", "replace").splitlines(keepends=True) if after is not None else []
            diff.extend(difflib.unified_diff(a, b, f"a/{path}" if before is not None else "/dev/null",
                                             f"b/{path}" if after is not None else "/dev/null"))
        else:
            diff.append(f"Binary file {path} {'added' if before is None else 'removed' if after is None else 'changed'}\n")
    text = "".join(diff)
    if len(text) > DIFF_LIMIT:
        text = text[:DIFF_LIMIT] + "\n… diff truncated\n"
    return lines, text


class DraftChanged(ValueError):
    """The draft is not the one the owner approved (it changed since the preview)."""


class Unchanged(ValueError):
    """The draft is the latest frozen revision: nothing to freeze."""


class Incomplete(ValueError):
    """The draft misses critical items of the completeness spec and the owner gave no reason to
    freeze it anyway. `validation` is pack_validator's result."""

    def __init__(self, message, validation):
        super().__init__(message)
        self.validation = validation


OVERRIDE_MIN = 10  # characters of a written reason, at least


def override_reason(text):
    """The owner's reason for freezing over critical gaps, cleaned, or '' when there is none."""
    text = (text or "").strip()[:2000]
    return text if len(text) >= OVERRIDE_MIN else ""


def freeze(db, app_id, user, approved_sha256, note, version_path, override=None):
    """Freeze the application's current draft as the next revision. The caller has checked the
    owner role; `approved_sha256` is the SHA-256 the owner approved on the preview. ValueError
    when the draft changed since, or nothing changed since the latest revision; `Incomplete` when
    critical items are missing (pack_validator) and `override` gives no written reason.

    With critical gaps and a reason, the draft carries the reason and those gaps ("Known gaps" in
    BUILD_TASK.md, `known_gaps` in evidence.json): the owner previews the draft with the same
    reason (`buildpack.draft(..., known_gaps=reason)`) and approves exactly those bytes. The
    override is audited as `package.freeze_override` and kept on the revision."""
    reason = override_reason(override)
    current = buildpack.draft(db, app_id, version_path, known_gaps=reason or None)
    if (approved_sha256 or "").strip().lower() != current.sha256:
        raise DraftChanged("The package changed since you previewed it: review the preview again and approve it.")
    previous = latest(db, app_id)
    if previous is not None and previous["sha256"] == current.sha256:
        raise Unchanged(f"Nothing changed since revision {previous['rev']}: it is still the latest package.")
    gaps = pack_validator.critical(current.validation)
    known = None
    if gaps:
        if not reason:
            names = "; ".join(g["title"] for g in gaps[:6]) + (" …" if len(gaps) > 6 else "")
            raise Incomplete(f"The package misses {len(gaps)} critical item{'s' if len(gaps) != 1 else ''}: {names}. "
                             f"Add them, or freeze it anyway with a written reason (at least {OVERRIDE_MIN} characters).",
                             current.validation)
        known = {"reason": reason, "gaps": [{"item": g["item"], "title": g["title"]} for g in gaps]}
    old_files, old_manifest = {}, None
    if previous is not None:
        old_files = buildpack.read(open_package(db, previous["storage_ref"]))
        old_manifest = json.loads(old_files.get("evidence.json", b"{}"))
    lines, diff = changes(old_files, current.files, old_manifest, json.loads(current.manifest))
    note = (note or "").strip()[:2000]
    if known:
        lines.insert(0, f"Frozen with {len(known['gaps'])} known critical gap{'s' if len(known['gaps']) != 1 else ''}: "
                        f"{reason}")
    summary = "\n".join(([note] if note else []) + lines)
    rev = (previous["rev"] if previous else 0) + 1
    at = audit.now()
    db.execute("INSERT INTO briefs(revision, app_id, manifest) VALUES (?, ?, ?) ON CONFLICT (revision) DO NOTHING",
               (current.revision, app_id, current.manifest))
    ref = put_package(db, app_id, current.revision, current.package, current.manifest)
    db.execute("""INSERT INTO builder_packages(app_id, rev, revision, sha256, size, storage_ref, approved_by,
                  approved_at, summary, diff, report, completeness, known_gaps) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
               (app_id, rev, current.revision, current.sha256, len(current.package), ref, user, at,
                summary, diff, json.dumps(current.report, ensure_ascii=False),
                json.dumps(current.validation, ensure_ascii=False, sort_keys=True),
                json.dumps(known, ensure_ascii=False, sort_keys=True) if known else ""))
    # frozen_packs: the index Build with Devin (builds.py) references
    if db.execute("SELECT 1 FROM frozen_packs WHERE revision = ?", (current.revision,)).fetchone() is None:
        db.execute("INSERT INTO frozen_packs (revision, app_id, sha256, frozen_by, frozen_at) VALUES (?, ?, ?, ?, ?)",
                   (current.revision, app_id, current.sha256, user, at))
    audit.record(db, user, "package.freeze", "builder_package", f"{app_id}/rev{rev}", app_id=app_id,
                 rev=rev, sha256=current.sha256, revision=current.revision, summary=summary,
                 report=current.summary(), completeness=current.validation["score"],
                 passed=current.validation["passed"])
    if known:
        audit.record(db, user, "package.freeze_override", "builder_package", f"{app_id}/rev{rev}", app_id=app_id,
                     rev=rev, sha256=current.sha256, reason=reason, gaps=[g["item"] for g in known["gaps"]])
    return rev


def completeness_of(row):
    """(validation, known gaps) stored with a revision; (None, None) before they were kept."""
    keys = row.keys()
    validation = json.loads(row["completeness"]) if "completeness" in keys and row["completeness"] else None
    known = json.loads(row["known_gaps"]) if "known_gaps" in keys and row["known_gaps"] else None
    return validation, known
