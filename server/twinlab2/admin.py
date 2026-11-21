"""Operator commands: backup, restore, integrity checks, key rotation, purges and retention.

    python -m twinlab2.admin backup DIR [--database URL] [--no-objects]
    python -m twinlab2.admin restore DIR --database URL [--no-objects]
    python -m twinlab2.admin verify-backup [DIR] [--database URL]
    python -m twinlab2.admin rewrap-local [--database URL]
    python -m twinlab2.admin purge SHA256 --note TEXT [--database URL] [--dry-run]
    python -m twinlab2.admin retention [--apply] [--database URL]

**backup** writes a logical, engine-neutral copy of the catalog into DIR (which must be new or
empty): `catalog/<table>.jsonl` (one JSON row per line, bytes as base64), and with objects
(the default) every stored artifact's bytes under `objects/<sha256>` (sealed when
TWINLAB2_ARTIFACT_KEY is set, so a backup is no less protected than the store). `manifest.json`
lists the schema version, each table's row count and SHA-256, and each object's SHA-256, size
and the artifact ids that use it. For PostgreSQL in production, `pg_dump` or the operator's
backups are the primary copy (docs/operations/restore.md); this command is the portable copy and
the check.

**restore** loads a backup into an *empty* database (refused otherwise): it applies the
migrations up to the backup's schema version's successors, inserts every row, moves identity
counters past them, and (with objects) writes each object back to the configured artifact store,
re-pointing the rows at the new object versions. It then runs verify-backup.

**verify-backup** checks a backup (every table file and object against the manifest) and/or a
database: the schema is current, every artifact row that is not deleted has its object in the
store with its SHA-256 and size, every row that refers to an artifact finds it, every frozen
package's bytes match its approved SHA-256, and (with both) the row counts match the backup's.
Exit status 0 means everything checked out; 1 lists the problems.
"""

from __future__ import annotations

import argparse
import base64
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

from twinlab2 import artifacts
from twinlab2 import db as database
from twinlab2 import migrations

FORMAT = "twinlab2-backup/1"
SKIP_TABLES = {"schema_migrations", "audit_prune_window"}
# (table, column) that refer to artifacts(id)
ARTIFACT_REFS = (("attachments", "artifact_id"), ("brief_packages", "artifact_id"), ("releases", "artifact_id"),
                 ("uploads", "artifact_id"), ("release_artifacts", "artifact_id"))


class Problems(list):
    def add(self, text):
        self.append(text)


def _encode(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$b64": base64.b64encode(bytes(value)).decode("ascii")}
    return value


def _decode(value):
    if isinstance(value, dict) and set(value) == {"$b64"}:
        return base64.b64decode(value["$b64"])
    return value


# -- backup ---------------------------------------------------------------------------------------


def backup(target, out_dir, objects=True, store=None, out=print) -> dict:
    """Write a backup of the catalog at `target` (and its artifact objects) into `out_dir`."""
    out_dir = Path(out_dir)
    if out_dir.exists() and any(out_dir.iterdir()):
        raise RuntimeError(f"{out_dir} is not empty: back up into a new folder.")
    (out_dir / "catalog").mkdir(parents=True, exist_ok=True)
    store = store or artifacts.from_env(target)
    manifest = {"format": FORMAT, "created_at": artifacts.now(), "source": database.redacted(target),
                "tables": {}, "objects": {}, "sealed": False}
    with closing(database.connect(target)) as db:
        versions = migrations.applied(db)
        manifest["schema_version"] = max(versions) if versions else 0
        tables = migrations._ordered(db, db.tables() - SKIP_TABLES)
        manifest["table_order"] = tables
        for table in tables:
            columns = db.columns(table)
            # ids ascending: a row that refers to an earlier row of its table (builds.previous_build)
            # is restored after it
            order = '"id"' if "id" in columns else "rowid" if db.engine == "sqlite" else ", ".join(
                f'"{c}"' for c in columns)
            digest, count = hashlib.sha256(), 0
            path = out_dir / "catalog" / f"{table}.jsonl"
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                cursor = db.execute(f'SELECT {", ".join(chr(34) + c + chr(34) for c in columns)} FROM "{table}" '
                                    f"ORDER BY {order}")
                for row in cursor:
                    line = json.dumps([_encode(v) for v in row], ensure_ascii=False, separators=(",", ":")) + "\n"
                    handle.write(line)
                    digest.update(line.encode("utf-8"))
                    count += 1
            manifest["tables"][table] = {"columns": columns, "rows": count, "sha256": digest.hexdigest()}
        db.commit()
        if objects:
            keyring = getattr(store, "keyring", None)
            manifest["sealed"] = keyring is not None
            (out_dir / "objects").mkdir(exist_ok=True)
            rows = db.execute("SELECT id FROM artifacts WHERE deleted_at IS NULL ORDER BY id").fetchall()
            for (artifact_id,) in rows:
                ref = artifacts.load(db, artifact_id)
                entry = manifest["objects"].setdefault(ref.sha256, {"size": ref.size, "artifacts": []})
                entry["artifacts"].append(artifact_id)
                path = out_dir / "objects" / ref.sha256
                if path.exists():
                    continue
                _write_object(ref, store, path, keyring)
                entry["stored_sha256"] = _sha_file(path)
            db.commit()
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")
    out(f"Backed up {sum(t['rows'] for t in manifest['tables'].values())} rows in {len(manifest['tables'])} tables"
        f" and {len(manifest['objects'])} objects to {out_dir}.")
    return manifest


def _write_object(ref, store, path, keyring):
    """The object's plaintext, checked against its SHA-256, written to `path` (sealed with the
    store's keyring when it has one)."""
    digest, size = hashlib.sha256(), 0
    handle, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as out, artifacts.open(ref, store) as stream:
            while chunk := stream.read(artifacts.CHUNK):
                digest.update(chunk)
                size += len(chunk)
                out.write(chunk)
        if digest.hexdigest() != ref.sha256 or size != ref.size:
            raise artifacts.ChecksumMismatch(ref.sha256, digest.hexdigest())
        if keyring is not None:
            from twinlab2 import crypto

            crypto.seal_file(tmp, path, keyring)
        else:
            os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _sha_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(artifacts.CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _open_object(path, keyring):
    from twinlab2 import crypto

    return crypto.open_file(path, keyring)


# -- restore --------------------------------------------------------------------------------------


def restore(backup_dir, target, objects=True, store=None, out=print) -> Problems:
    """Load a backup into the empty database at `target`; returns verify's problems."""
    backup_dir = Path(backup_dir)
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != FORMAT:
        raise RuntimeError(f"{backup_dir} is not a TwinLab2 backup ({manifest.get('format')!r}).")
    if manifest["schema_version"] > migrations.latest():
        raise RuntimeError(f"The backup is at schema {manifest['schema_version']}; this portal knows only "
                           f"{migrations.latest()}: restore with the portal version that made it.")
    problems = verify_backup_dir(backup_dir, store, manifest)
    if problems:
        raise RuntimeError("The backup does not verify: " + "; ".join(problems[:5]))
    store = store or artifacts.from_env(target)
    if database.engine_of(str(target)) == "sqlite":
        database.sqlite_path(str(target)).parent.mkdir(parents=True, exist_ok=True)
    with closing(database.connect(target)) as db:
        migrations.run(db)
        busy = [t for t in manifest["table_order"] if t in db.tables()
                and db.execute(f'SELECT 1 FROM "{t}" LIMIT 1').fetchone()]
        db.commit()
        if busy:
            raise RuntimeError("The database is not empty (" + ", ".join(sorted(busy)) + "): restore into a new one.")
        types = {}
        with db:
            if db.engine == "sqlite":
                db.execute("PRAGMA defer_foreign_keys = ON")
            for table in manifest["table_order"]:
                if table not in db.tables():
                    raise RuntimeError(f"This portal has no table {table}.")
                info = manifest["tables"][table]
                have = set(db.columns(table))
                columns = [c for c in info["columns"] if c in have]
                keep = [i for i, c in enumerate(info["columns"]) if c in have]
                types = migrations._types(db, table)
                names = ", ".join(f'"{c}"' for c in columns)
                insert = f'INSERT INTO "{table}" ({names}) VALUES ({", ".join("?" for _ in columns)})'
                batch = []
                with open(backup_dir / "catalog" / f"{table}.jsonl", encoding="utf-8") as handle:
                    for line in handle:
                        values = json.loads(line)
                        batch.append([migrations._coerce(_decode(values[i]), types.get(info["columns"][i]))
                                      for i in keep])
                        if len(batch) >= 500:
                            db.executemany(insert, batch)
                            batch = []
                if batch:
                    db.executemany(insert, batch)
            if db.engine == "postgresql":
                for table in manifest["table_order"]:
                    if "id" not in db.columns(table):
                        continue
                    sequence = db.execute("SELECT pg_get_serial_sequence(?, 'id')", (table,)).fetchone()[0]
                    if sequence:
                        db.execute(f'SELECT setval(?, COALESCE((SELECT MAX(id) FROM "{table}"), 1), '
                                   f'(SELECT MAX(id) FROM "{table}") IS NOT NULL)', (sequence,))
        out(f"Restored {sum(t['rows'] for t in manifest['tables'].values())} rows into {database.redacted(target)}.")
        if objects and manifest["objects"]:
            restore_objects(db, backup_dir, manifest, store, out)
    return verify(target, store, backup_dir=backup_dir, manifest=manifest)


def restore_objects(db, backup_dir, manifest, store, out=print):
    """Write every object of the backup to `store` and point its artifact rows at the copy."""
    keyring = getattr(store, "keyring", None)
    restored = 0
    for sha, entry in manifest["objects"].items():
        path = Path(backup_dir) / "objects" / sha
        for artifact_id in entry["artifacts"]:
            ref = artifacts.load(db, artifact_id, include_deleted=True)
            with _open_object(path, keyring) as stream:
                new = artifacts.put(stream, kind=ref.kind, app_id=ref.app_id, content_type=ref.content_type,
                                    sha256=ref.sha256, target=store)
            with db:
                db.execute("UPDATE artifacts SET backend = ?, bucket = ?, object_key = ?, version_id = ? WHERE id = ?",
                           (new.backend, new.bucket, new.key, new.version_id, artifact_id))
            restored += 1
    out(f"Restored {restored} artifact objects into the {store.backend} store.")


# -- verify ---------------------------------------------------------------------------------------


def verify_backup_dir(backup_dir, store=None, manifest=None) -> Problems:
    """A backup's files against its manifest: each table file's SHA-256 and row count, each
    object's stored SHA-256 and (when it can be read) its plaintext SHA-256 and size."""
    backup_dir = Path(backup_dir)
    problems = Problems()
    manifest = manifest or json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    for table, info in manifest["tables"].items():
        path = backup_dir / "catalog" / f"{table}.jsonl"
        if not path.is_file():
            problems.add(f"backup: {table}.jsonl is missing")
            continue
        digest, count = hashlib.sha256(), 0
        with open(path, "rb") as handle:
            for line in handle:
                digest.update(line)
                count += 1
        if digest.hexdigest() != info["sha256"] or count != info["rows"]:
            problems.add(f"backup: {table}.jsonl does not match the manifest ({count} rows, expected {info['rows']})")
    keyring = getattr(store, "keyring", None) if store is not None else _env_keyring()
    for sha, entry in manifest["objects"].items():
        path = backup_dir / "objects" / sha
        if not path.is_file():
            problems.add(f"backup: object {sha[:12]}… is missing")
            continue
        if entry.get("stored_sha256") and _sha_file(path) != entry["stored_sha256"]:
            problems.add(f"backup: object {sha[:12]}… changed since the backup")
            continue
        try:
            digest, size = hashlib.sha256(), 0
            with _open_object(path, keyring) as stream:
                while chunk := stream.read(artifacts.CHUNK):
                    digest.update(chunk)
                    size += len(chunk)
            if digest.hexdigest() != sha or size != entry["size"]:
                problems.add(f"backup: object {sha[:12]}… does not match its SHA-256")
        except Exception as error:  # noqa: BLE001 - a sealed object without its key, or damage
            problems.add(f"backup: object {sha[:12]}… cannot be read ({error})")
    return problems


def _env_keyring():
    from twinlab2 import crypto

    try:
        return crypto.Keyring.from_env()
    except crypto.SealError:
        return None


def verify(target, store=None, backup_dir=None, manifest=None) -> Problems:
    """The database at `target` against the artifact store (and the backup's row counts)."""
    problems = Problems()
    if backup_dir is not None:
        manifest = manifest or json.loads((Path(backup_dir) / "manifest.json").read_text(encoding="utf-8"))
        problems.extend(verify_backup_dir(backup_dir, store, manifest))
    if target is None:
        return problems
    store = store or artifacts.from_env(target)
    with closing(database.connect(target)) as db:
        versions = migrations.applied(db)
        at = max(versions) if versions else 0
        if at != migrations.latest():
            problems.add(f"database: at schema {at}, this portal needs {migrations.latest()}")
        tables = db.tables()
        if manifest is not None:
            for table, info in manifest["tables"].items():
                found = db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] if table in tables else None
                if found != info["rows"]:
                    problems.add(f"database: {table} has {found} rows, the backup {info['rows']}")
        rows = db.execute("SELECT id, deleted_at FROM artifacts ORDER BY id").fetchall()
        live = set()
        for row in rows:
            if row["deleted_at"]:
                continue
            ref = artifacts.load(db, row["id"])
            live.add(row["id"])
            if ref.backend != store.backend:
                problems.add(f"artifact {ref.id}: in the {ref.backend} backend, the portal uses {store.backend}")
            elif not artifacts.verify(ref, store):
                problems.add(f"artifact {ref.id} ({ref.kind}): its object is missing or does not match "
                             f"sha256 {ref.sha256[:12]}…")
        known = {r["id"] for r in rows}
        for table, column in ARTIFACT_REFS:
            if table not in tables or column not in db.columns(table):
                continue
            for (value,) in db.execute(f'SELECT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL'):
                if value not in known:
                    problems.add(f"{table}.{column} = {value}: no such artifact")
        from twinlab2 import packages

        for row in db.execute("SELECT app_id, rev, sha256, storage_ref FROM builder_packages ORDER BY id").fetchall():
            try:
                stored = packages.stored_row(db, row["storage_ref"])
            except LookupError:
                problems.add(f"frozen package {row['app_id']}:{row['rev']}: its stored row is missing")
                continue
            if stored["artifact_id"] is not None and stored["artifact_id"] not in live:
                continue  # deleted by retention or a purge: its tombstone says so
            try:
                data = packages.open_package(db, row["storage_ref"])
            except Exception as error:  # noqa: BLE001
                problems.add(f"frozen package {row['app_id']}:{row['rev']}: cannot be read ({type(error).__name__})")
                continue
            if hashlib.sha256(data).hexdigest() != row["sha256"]:
                problems.add(f"frozen package {row['app_id']}:{row['rev']}: does not match its approved SHA-256")
        db.commit()
    return problems


# -- key rotation, purge, retention ------------------------------------------------------------------


def rewrap_local(target, store=None, out=print) -> dict:
    """Seal every local artifact file and re-wrap its data key with the current key."""
    from twinlab2 import crypto

    store = store or artifacts.from_env(target)
    if store.backend != "local" or store.keyring is None:
        raise RuntimeError("rewrap-local needs the local artifact backend with TWINLAB2_ARTIFACT_KEY set.")
    counts = {"sealed": 0, "rewrapped": 0, "current": 0, "missing": 0}
    with closing(database.connect(target)) as db:
        for (artifact_id,) in db.execute("SELECT id FROM artifacts WHERE backend = 'local' AND deleted_at IS NULL "
                                         "ORDER BY id").fetchall():
            ref = artifacts.load(db, artifact_id)
            path = store.file_of(ref)
            if not path.is_file():
                counts["missing"] += 1
                continue
            counts[crypto.rewrap(path, store.keyring)] += 1
        db.commit()
    out(", ".join(f"{k}: {v}" for k, v in counts.items()))
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m twinlab2.admin", description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    url_help = "a DATABASE_URL or SQLite path (default: DATABASE_URL / TWINLAB2_DATABASE)"
    c = commands.add_parser("backup", help="write a logical backup with the artifact objects")
    c.add_argument("dir")
    c.add_argument("--database", default=None, help=url_help)
    c.add_argument("--no-objects", action="store_true", help="the catalog only (objects are backed up elsewhere)")
    c = commands.add_parser("restore", help="load a backup into an empty database")
    c.add_argument("dir")
    c.add_argument("--database", required=True, help="the new, empty database")
    c.add_argument("--no-objects", action="store_true", help="the rows only (the store already has the objects)")
    c = commands.add_parser("verify-backup", help="check a backup and/or a database against the artifact store")
    c.add_argument("dir", nargs="?")
    c.add_argument("--database", default=None, help=url_help)
    c = commands.add_parser("rewrap-local", help="seal and re-wrap local artifact files with the current key")
    c.add_argument("--database", default=None, help=url_help)
    c = commands.add_parser("purge", help="delete every stored copy of one file, by its SHA-256")
    c.add_argument("sha256")
    c.add_argument("--note", required=True, help="why (kept in the tombstone and the audit history)")
    c.add_argument("--actor", default=os.environ.get("USER") or os.environ.get("USERNAME") or "admin-cli")
    c.add_argument("--dry-run", action="store_true", help="only list where it is")
    c.add_argument("--database", default=None, help=url_help)
    c = commands.add_parser("retention", help="what the retention policy would delete (or --apply it)")
    c.add_argument("--apply", action="store_true")
    c.add_argument("--database", default=None, help=url_help)
    args = parser.parse_args(argv)
    target = args.database or database.from_env()
    try:
        if args.command == "backup":
            backup(target, args.dir, objects=not args.no_objects)
            return 0
        if args.command == "restore":
            problems = restore(args.dir, target, objects=not args.no_objects)
        elif args.command == "verify-backup":
            if not args.dir and not args.database and not os.environ.get("DATABASE_URL"):
                target = database.from_env()
            problems = verify(target, backup_dir=args.dir)
        elif args.command == "rewrap-local":
            rewrap_local(target)
            return 0
        elif args.command == "purge":
            from twinlab2 import lifecycle

            artifacts.configure(artifacts.from_env(target))
            with closing(database.connect(target)) as db:
                migrations.run(db)
                if args.dry_run:
                    print(json.dumps(lifecycle.find_sha256(db, args.sha256), indent=1, default=str))
                    return 0
                with db:
                    print(json.dumps(lifecycle.purge_sha256(db, args.sha256, args.actor, args.note), indent=1))
            return 0
        else:  # retention
            from twinlab2 import lifecycle

            artifacts.configure(artifacts.from_env(target))
            with closing(database.connect(target)) as db:
                migrations.run(db)
                print(json.dumps(lifecycle.apply_retention(db, "admin-cli", dry_run=not args.apply), indent=1))
            return 0
    except (RuntimeError, artifacts.ArtifactError, *database.Error) as error:
        print(f"{args.command} failed: {error}", file=sys.stderr)
        return 1
    for problem in problems:
        print(problem)
    print("OK: everything checked out." if not problems else f"{len(problems)} problems.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
