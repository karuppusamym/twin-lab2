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

The ZIP itself is stored by `put_package` / `open_package`: today in the catalog's
`brief_packages` table; switch these two functions to `artifacts.put(kind="builder_package")` /
`artifacts.open` once the artifact store is on main.
"""

from __future__ import annotations

import difflib
import json

import audit
import buildpack

SCHEMA = """
CREATE TABLE IF NOT EXISTS builder_packages (
    id INTEGER PRIMARY KEY, app_id INTEGER NOT NULL REFERENCES apps(id), rev INTEGER NOT NULL,
    revision TEXT NOT NULL REFERENCES briefs(revision), sha256 TEXT NOT NULL, size INTEGER NOT NULL,
    storage_ref TEXT NOT NULL, approved_by TEXT NOT NULL, approved_at TEXT NOT NULL,
    summary TEXT NOT NULL, diff TEXT NOT NULL, report TEXT NOT NULL,
    UNIQUE (app_id, rev)
);
"""

SQLITE_GUARDS = """
CREATE TRIGGER IF NOT EXISTS builder_packages_no_update BEFORE UPDATE ON builder_packages
BEGIN SELECT RAISE(ABORT, 'a frozen builder package is immutable'); END;
CREATE TRIGGER IF NOT EXISTS builder_packages_no_delete BEFORE DELETE ON builder_packages
BEGIN SELECT RAISE(ABORT, 'a frozen builder package is immutable'); END;
CREATE TRIGGER IF NOT EXISTS brief_packages_no_update BEFORE UPDATE ON brief_packages
BEGIN SELECT RAISE(ABORT, 'a stored build package is immutable'); END;
"""

DIFF_LIMIT = 200_000


def migrate(db):
    db.executescript(SCHEMA)
    if audit.is_sqlite(db):
        db.executescript(SQLITE_GUARDS)


# -- storage: the one place to switch to artifacts.put(kind="builder_package") -------------------


def put_package(db, app_id, revision, data: bytes) -> str:
    """Store the package bytes; returns the reference `open_package` takes."""
    db.execute("INSERT OR IGNORE INTO brief_packages VALUES (?, ?)", (revision, data))
    return f"brief_packages:{revision}"


def open_package(db, storage_ref: str) -> bytes:
    kind, _, key = storage_ref.partition(":")
    if kind != "brief_packages":
        raise LookupError(storage_ref)
    row = db.execute("SELECT archive FROM brief_packages WHERE revision = ?", (key,)).fetchone()
    if row is None:
        raise LookupError(storage_ref)
    return bytes(row[0])


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


def freeze(db, app_id, user, approved_sha256, note, version_path):
    """Freeze the application's current draft as the next revision. The caller has checked the
    owner role; `approved_sha256` is the SHA-256 the owner approved on the preview. ValueError
    when the draft changed since, or nothing changed since the latest revision."""
    current = buildpack.draft(db, app_id, version_path)
    if (approved_sha256 or "").strip().lower() != current.sha256:
        raise ValueError("The package changed since you previewed it: review the preview again and approve it.")
    previous = latest(db, app_id)
    if previous is not None and previous["sha256"] == current.sha256:
        raise ValueError(f"Nothing changed since revision {previous['rev']}: it is still the latest package.")
    old_files, old_manifest = {}, None
    if previous is not None:
        old_files = buildpack.read(open_package(db, previous["storage_ref"]))
        old_manifest = json.loads(old_files.get("evidence.json", b"{}"))
    lines, diff = changes(old_files, current.files, old_manifest, json.loads(current.manifest))
    note = (note or "").strip()[:2000]
    summary = "\n".join(([note] if note else []) + lines)
    rev = (previous["rev"] if previous else 0) + 1
    at = audit.now()
    db.execute("INSERT OR IGNORE INTO briefs VALUES (?, ?, ?)", (current.revision, app_id, current.manifest))
    ref = put_package(db, app_id, current.revision, current.package)
    db.execute("""INSERT INTO builder_packages(app_id, rev, revision, sha256, size, storage_ref, approved_by,
                  approved_at, summary, diff, report) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
               (app_id, rev, current.revision, current.sha256, len(current.package), ref, user, at,
                summary, diff, json.dumps(current.report, ensure_ascii=False)))
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "frozen_packs" in tables:  # the index Build with Devin (builds.py) references
        if db.execute("SELECT 1 FROM frozen_packs WHERE revision = ?", (current.revision,)).fetchone() is None:
            db.execute("INSERT INTO frozen_packs (revision, app_id, sha256, frozen_by, frozen_at) VALUES (?, ?, ?, ?, ?)",
                       (current.revision, app_id, current.sha256, user, at))
    audit.record(db, user, "package.freeze", "builder_package", f"{app_id}/rev{rev}", app_id=app_id,
                 rev=rev, sha256=current.sha256, revision=current.revision, summary=summary,
                 report=current.summary())
    return rev
