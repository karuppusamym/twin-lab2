"""Versioned schema migrations for the portal database, and the SQLite → PostgreSQL copy.

Each migration has a number, a name and a list of steps; `run(connection)` applies, in order,
every migration not yet recorded in `schema_migrations`, each in its own transaction (on
PostgreSQL under an advisory lock, so several workers or pods starting at once apply it once).
A step is a DDL string (see `db.Connection.ddl`: `{id}`, `{blob}`) or a function of the
connection. Never edit a migration that has shipped: add the next number.

Migration 1 is the baseline: the schema as it stood before numbered migrations, written so that
it also brings a catalog made by an older portal (tables created ad hoc, columns added later) up
to date.

    python migrations.py status   [--database URL]
    python migrations.py migrate  [--database URL]
    python migrations.py copy SOURCE DEST   # e.g. .state/catalog.db postgresql://…
"""

from __future__ import annotations

import argparse
import sys

import db as database

LOCK = 72_401_913  # pg_advisory_xact_lock key for migrations


# -- 1: baseline -----------------------------------------------------------------------------------

BASELINE_TABLES = [
    """CREATE TABLE IF NOT EXISTS apps (
        id {id}, name TEXT NOT NULL, git_url TEXT NOT NULL, requirements TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS recordings (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, events TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS evidence (
        app_id INTEGER NOT NULL REFERENCES apps(id),
        recording_id TEXT NOT NULL REFERENCES recordings(id),
        selection TEXT NOT NULL, notes TEXT NOT NULL,
        PRIMARY KEY (app_id, recording_id)
    )""",
    """CREATE TABLE IF NOT EXISTS attachments (
        id {id}, app_id INTEGER NOT NULL REFERENCES apps(id),
        title TEXT NOT NULL, kind TEXT NOT NULL, extension TEXT NOT NULL, content {blob} NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS briefs (
        revision TEXT PRIMARY KEY, app_id INTEGER NOT NULL REFERENCES apps(id), manifest TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS brief_packages (
        revision TEXT PRIMARY KEY REFERENCES briefs(revision), archive {blob} NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS releases (
        id {id}, app_id INTEGER NOT NULL REFERENCES apps(id),
        version TEXT NOT NULL, revision TEXT NOT NULL REFERENCES briefs(revision),
        commit_sha TEXT NOT NULL, archive {blob} NOT NULL, archive_sha TEXT NOT NULL,
        UNIQUE (app_id, version)
    )""",
    """CREATE TABLE IF NOT EXISTS deployments (
        release_id INTEGER NOT NULL REFERENCES releases(id), environment TEXT NOT NULL,
        url TEXT NOT NULL, username TEXT NOT NULL, secret_ref TEXT NOT NULL,
        PRIMARY KEY (release_id, environment)
    )""",
    """CREATE TABLE IF NOT EXISTS release_tests (
        release_id INTEGER PRIMARY KEY REFERENCES releases(id), commit_sha TEXT NOT NULL,
        platform TEXT NOT NULL, launch TEXT NOT NULL, reset_result TEXT NOT NULL,
        reset_notes TEXT NOT NULL, results TEXT NOT NULL, tested_by TEXT NOT NULL, tested_at TEXT NOT NULL
    )""",
    # registry: machines, sessions, references, automations
    """CREATE TABLE IF NOT EXISTS app_identities (
        key TEXT PRIMARY KEY, app_id INTEGER NOT NULL REFERENCES apps(id), surface TEXT NOT NULL,
        facts TEXT NOT NULL DEFAULT '{}'
    )""",
    """CREATE TABLE IF NOT EXISTS machines (
        id {id}, name TEXT NOT NULL, token_sha TEXT NOT NULL UNIQUE,
        paired_by TEXT NOT NULL, paired_at TEXT NOT NULL, last_seen TEXT, revoked_at TEXT,
        recorder TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS pairing_codes (
        code_sha TEXT PRIMARY KEY, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL, used_at TEXT, machine_id INTEGER
    )""",
    "CREATE TABLE IF NOT EXISTS pairing_failures (at TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS automations (
        id {id}, name TEXT NOT NULL UNIQUE, kind TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS automation_apps (
        automation_id INTEGER NOT NULL REFERENCES automations(id),
        app_id INTEGER NOT NULL REFERENCES apps(id), PRIMARY KEY (automation_id, app_id)
    )""",
    """CREATE TABLE IF NOT EXISTS capture_sessions (
        id {id}, name TEXT NOT NULL, machine_id INTEGER NOT NULL REFERENCES machines(id),
        targets TEXT NOT NULL, automation_id INTEGER REFERENCES automations(id),
        created_by TEXT NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL,
        recording_id TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS session_refs (
        recording_id TEXT PRIMARY KEY REFERENCES recordings(id), session_key TEXT NOT NULL UNIQUE,
        machine_id INTEGER REFERENCES machines(id), capture_session_id INTEGER,
        automation_id INTEGER REFERENCES automations(id), payload TEXT NOT NULL,
        received_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS recording_apps (
        recording_id TEXT NOT NULL REFERENCES recordings(id), app_id INTEGER NOT NULL REFERENCES apps(id),
        key TEXT NOT NULL, level TEXT NOT NULL, flow TEXT NOT NULL,
        PRIMARY KEY (recording_id, app_id, key)
    )""",
    """CREATE TABLE IF NOT EXISTS recording_contracts (
        recording_id TEXT PRIMARY KEY REFERENCES recordings(id), contract_version TEXT NOT NULL,
        recorder TEXT NOT NULL, recorder_version TEXT NOT NULL, twinlab_commit TEXT,
        received_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS machine_contracts (
        machine_id INTEGER PRIMARY KEY REFERENCES machines(id), contract_version TEXT NOT NULL,
        recorder TEXT NOT NULL, updated_at TEXT NOT NULL
    )""",
    # scenarios
    """CREATE TABLE IF NOT EXISTS scenarios (
        id {id}, app_id INTEGER NOT NULL REFERENCES apps(id),
        position INTEGER NOT NULL DEFAULT 0, name TEXT NOT NULL,
        starting TEXT NOT NULL DEFAULT '', actions TEXT NOT NULL DEFAULT '',
        events TEXT NOT NULL DEFAULT '[]', expected TEXT NOT NULL DEFAULT '',
        failures TEXT NOT NULL DEFAULT '', examples TEXT NOT NULL DEFAULT '',
        questions TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT ''
    )""",
]

# Columns older portals added after a table was first created, in the order they were added.
BASELINE_COLUMNS = [
    # release states and deployment verification: an existing release is Packaged, an existing
    # deployment an unverified registered URL
    ("releases", "state", "TEXT NOT NULL DEFAULT 'packaged'"),
    ("releases", "published_at", "TEXT"),
    ("deployments", "verification", "TEXT NOT NULL DEFAULT 'unverified'"),
    ("deployments", "observed_commit", "TEXT NOT NULL DEFAULT ''"),
    ("deployments", "checked_at", "TEXT"),
    # registry: application status, surface and how its web addresses match (`path`: longest
    # path prefix wins; `host`: the owner's override, the addresses stand for their whole host)
    ("apps", "status", "TEXT NOT NULL DEFAULT 'known'"),
    ("apps", "surface", "TEXT NOT NULL DEFAULT ''"),
    ("apps", "url_match", "TEXT NOT NULL DEFAULT 'path'"),
    # links stored before `reviewed` existed count as reviewed: a resend only suggests events
    ("evidence", "reviewed", "INTEGER NOT NULL DEFAULT 1"),
    ("evidence", "suggested", "TEXT NOT NULL DEFAULT '[]'"),
    # the catalog card
    ("apps", "owner", "TEXT NOT NULL DEFAULT ''"),
    ("apps", "purpose", "TEXT NOT NULL DEFAULT ''"),
]


def add_columns(columns):
    """A step adding each (table, column, definition) the table does not have yet."""
    def step(connection):
        for table, column, definition in columns:
            if column not in connection.columns(table):
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    return step


# -- 2: release verification (checks of the exact built commit, gate results, reports) -------------

RELEASE_VERIFICATION = [
    """CREATE TABLE IF NOT EXISTS commit_checks (
        repository TEXT NOT NULL, commit_sha TEXT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL,
        conclusion TEXT NOT NULL, url TEXT NOT NULL, completed_at TEXT, fetched_at TEXT NOT NULL,
        PRIMARY KEY (repository, commit_sha, kind, name)
    )""",
    """CREATE TABLE IF NOT EXISTS release_verifications (
        release_id INTEGER PRIMARY KEY REFERENCES releases(id), commit_sha TEXT NOT NULL,
        repository TEXT NOT NULL, observed_head TEXT NOT NULL, status TEXT NOT NULL,
        reasons TEXT NOT NULL, checked_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS release_artifacts (
        id {id}, release_id INTEGER NOT NULL REFERENCES releases(id),
        kind TEXT NOT NULL, sha256 TEXT NOT NULL, content {blob} NOT NULL, created_at TEXT NOT NULL
    )""",
    # IF NOT EXISTS / missing-only: a catalog may have these from the release-verification
    # portal, which created them before numbered migrations
    add_columns([
        ("apps", "required_checks", "TEXT NOT NULL DEFAULT ''"),
        ("apps", "target_platform", "TEXT NOT NULL DEFAULT ''"),
        ("apps", "windows_check_pattern", "TEXT NOT NULL DEFAULT ''"),
        ("apps", "lab_url", "TEXT NOT NULL DEFAULT ''"),
        ("apps", "launch", "TEXT NOT NULL DEFAULT ''"),
        ("releases", "repository", "TEXT NOT NULL DEFAULT ''"),
        ("releases", "pr_number", "INTEGER"),
        ("releases", "source_ref", "TEXT NOT NULL DEFAULT ''"),
    ]),
]


# -- 3: durable build jobs, Devin builds bound to a frozen pack ---------------------------------------

BUILD_JOBS = [
    """CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY, type TEXT NOT NULL, state TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0, lease_until TEXT, lease_owner TEXT,
        payload TEXT NOT NULL, result TEXT, error TEXT, dedupe_key TEXT UNIQUE,
        run_after TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS jobs_claimable ON jobs (state, run_after)",
    """CREATE TABLE IF NOT EXISTS workers (
        id TEXT PRIMARY KEY, host TEXT NOT NULL, pid INTEGER NOT NULL,
        started_at TEXT NOT NULL, seen_at TEXT NOT NULL, current_job TEXT, stopped_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_by TEXT NOT NULL, updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS github_repos (
        repo TEXT PRIMARY KEY, default_branch TEXT NOT NULL, private INTEGER NOT NULL DEFAULT 0,
        connected_by TEXT NOT NULL, connected_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS frozen_packs (
        revision TEXT PRIMARY KEY REFERENCES briefs(revision), app_id INTEGER NOT NULL REFERENCES apps(id),
        sha256 TEXT NOT NULL, frozen_by TEXT NOT NULL, frozen_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS builds (
        id {id}, app_id INTEGER NOT NULL REFERENCES apps(id),
        revision TEXT NOT NULL REFERENCES frozen_packs(revision), pack_sha256 TEXT NOT NULL,
        repo TEXT NOT NULL, base_branch TEXT NOT NULL, base_sha TEXT NOT NULL, attempt INTEGER NOT NULL,
        previous_build INTEGER REFERENCES builds(id), previous_revision TEXT, change_summary TEXT NOT NULL,
        acu_budget INTEGER NOT NULL, state TEXT NOT NULL, active_app INTEGER UNIQUE,
        idempotency_tag TEXT NOT NULL UNIQUE, create_token TEXT, create_calls INTEGER NOT NULL DEFAULT 0,
        reconcile_checks INTEGER NOT NULL DEFAULT 0, attachment_url TEXT,
        devin_session_id TEXT, devin_url TEXT, devin_status TEXT, devin_status_detail TEXT, acus_consumed REAL,
        pr_number INTEGER, pr_url TEXT, head_sha TEXT, questions TEXT NOT NULL DEFAULT '[]',
        error TEXT, note TEXT, requested_by TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS build_messages (
        id {id}, build_id INTEGER NOT NULL REFERENCES builds(id), author TEXT NOT NULL,
        body TEXT NOT NULL, state TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""",
]


# -- 4: recording completeness (recorder contract 1.1) ---------------------------------------------

RECORDING_DETAILS = [
    """CREATE TABLE IF NOT EXISTS recording_completeness (
        recording_id TEXT PRIMARY KEY REFERENCES recordings(id), completeness TEXT, preflight TEXT,
        accepted_by TEXT, accepted_at TEXT, updated_at TEXT NOT NULL
    )""",
]


# -- 5: artifact storage (refs, not bytes) and resumable recording uploads -----------------------------

ARTIFACTS = [
    """CREATE TABLE IF NOT EXISTS artifacts (
        id {id}, backend TEXT NOT NULL, bucket TEXT NOT NULL, object_key TEXT NOT NULL,
        version_id TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
        content_type TEXT NOT NULL, kind TEXT NOT NULL, app_id INTEGER, created_at TEXT NOT NULL,
        UNIQUE (backend, bucket, object_key, version_id)
    )""",
    add_columns([(table, "artifact_id", "INTEGER REFERENCES artifacts(id)")
                 for table in ("attachments", "brief_packages", "releases")]),
    """CREATE TABLE IF NOT EXISTS uploads (
        id TEXT PRIMARY KEY, machine_id INTEGER NOT NULL REFERENCES machines(id),
        recording_id TEXT NOT NULL, session_id TEXT NOT NULL, contract_version TEXT NOT NULL,
        total_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, content_type TEXT NOT NULL,
        mode TEXT NOT NULL, chunk_size INTEGER NOT NULL, backend TEXT NOT NULL, object_key TEXT NOT NULL,
        multipart_id TEXT NOT NULL DEFAULT '', object_version TEXT NOT NULL DEFAULT '',
        state TEXT NOT NULL, received_bytes INTEGER NOT NULL DEFAULT 0, last_error TEXT,
        token_sha TEXT NOT NULL, token_expires TEXT NOT NULL, artifact_id INTEGER REFERENCES artifacts(id),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS uploads_recording ON uploads(recording_id)",
]


# -- 6: roles, audit history, frozen builder packages, evidence sharing ---------------------------------


def append_only(table, message):
    """A step refusing UPDATE and DELETE on `table`: SQLite triggers, or a PostgreSQL trigger."""
    def step(connection):
        if connection.engine == "sqlite":
            for event in ("UPDATE", "DELETE"):
                connection.ddl(f"""CREATE TRIGGER IF NOT EXISTS {table}_no_{event.lower()} BEFORE {event} ON {table}
                                   BEGIN SELECT RAISE(ABORT, '{message}'); END""")
        else:
            connection.ddl(f"""CREATE OR REPLACE FUNCTION {table}_refuse() RETURNS trigger LANGUAGE plpgsql AS
                               $$ BEGIN RAISE EXCEPTION '{message}' USING ERRCODE = 'integrity_constraint_violation';
                               END $$""")
            connection.ddl(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
            connection.ddl(f"""CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table}
                               FOR EACH ROW EXECUTE FUNCTION {table}_refuse()""")
    return step


def drop_sqlite_trigger(name):
    def step(connection):
        if connection.engine == "sqlite":
            connection.ddl(f"DROP TRIGGER IF EXISTS {name}")
    return step


ROLES_AUDIT = [
    """CREATE TABLE IF NOT EXISTS users (
        name TEXT PRIMARY KEY, password_hash TEXT NOT NULL DEFAULT '', is_admin INTEGER NOT NULL DEFAULT 0,
        created_by TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS app_roles (
        app_id INTEGER NOT NULL REFERENCES apps(id), user_name TEXT NOT NULL, role TEXT NOT NULL,
        granted_by TEXT NOT NULL, granted_at TEXT NOT NULL,
        PRIMARY KEY (app_id, user_name)
    )""",
    # app_id is a plain column, not a foreign key: an application's history outlives a merge
    """CREATE TABLE IF NOT EXISTS audit_log (
        id {id}, at TEXT NOT NULL, actor TEXT NOT NULL, app_id INTEGER,
        action TEXT NOT NULL, object_type TEXT NOT NULL, object_id TEXT NOT NULL, details TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS audit_log_app ON audit_log(app_id, id)",
    append_only("audit_log", "audit_log is append-only"),
    """CREATE TABLE IF NOT EXISTS builder_packages (
        id {id}, app_id INTEGER NOT NULL REFERENCES apps(id), rev INTEGER NOT NULL,
        revision TEXT NOT NULL REFERENCES briefs(revision), sha256 TEXT NOT NULL, size INTEGER NOT NULL,
        storage_ref TEXT NOT NULL, approved_by TEXT NOT NULL, approved_at TEXT NOT NULL,
        summary TEXT NOT NULL, diff TEXT NOT NULL, report TEXT NOT NULL,
        UNIQUE (app_id, rev)
    )""",
    drop_sqlite_trigger("brief_packages_no_update"),  # an earlier draft guarded brief_packages
    append_only("builder_packages", "a frozen builder package is immutable"),
    """CREATE TABLE IF NOT EXISTS recording_uploads (
        recording_id TEXT PRIMARY KEY REFERENCES recordings(id), added_by TEXT NOT NULL, added_at TEXT NOT NULL
    )""",
    # uploaded evidence is restricted until the owner marks it reviewed or synthetic; files
    # attached before this column existed are restricted too: uploading never meant sharing
    add_columns([("attachments", "share", "TEXT NOT NULL DEFAULT 'restricted'"),
                 ("attachments", "added_by", "TEXT NOT NULL DEFAULT ''")]),
]


# -- the list ----------------------------------------------------------------------------------------
# (version, name, steps). Append only.

MIGRATIONS = [
    (1, "baseline", [*BASELINE_TABLES, add_columns(BASELINE_COLUMNS)]),
    (2, "release verification", RELEASE_VERIFICATION),
    (3, "build jobs", BUILD_JOBS),
    (4, "recording details", RECORDING_DETAILS),
    (5, "artifacts and uploads", ARTIFACTS),
    (6, "roles and audit", ROLES_AUDIT),
]


def _table(connection):
    connection.ddl("""CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL
    )""")
    connection.commit()


def applied(connection):
    _table(connection)
    versions = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
    connection.commit()
    return versions


def latest():
    return MIGRATIONS[-1][0]


def run(connection):
    """Apply every pending migration; returns the versions applied."""
    from registry import now

    done = []
    for version, name, steps in MIGRATIONS:
        if version in applied(connection):
            continue
        connection.begin()
        try:
            if connection.engine == "postgresql":
                connection.execute("SELECT pg_advisory_xact_lock(?)", (LOCK,))
            if connection.execute("SELECT 1 FROM schema_migrations WHERE version = ?", (version,)).fetchone():
                connection.rollback()  # another process applied it meanwhile
                continue
            for step in steps:
                step(connection) if callable(step) else connection.ddl(step)
            connection.execute("INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                               (version, name, now()))
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        done.append(version)
    return done


# -- SQLite → PostgreSQL -------------------------------------------------------------------------------


def _ordered(connection, tables):
    """Tables with every table a foreign key refers to before it."""
    after = {table: {fk["table"] for fk in connection.foreign_keys(table)} & tables - {table} for table in tables}
    ordered = []
    while after:
        ready = sorted(table for table, needs in after.items() if not needs)
        if not ready:
            raise RuntimeError("Foreign keys form a cycle: " + ", ".join(sorted(after)))
        for table in ready:
            ordered.append(table)
            del after[table]
        for needs in after.values():
            needs.difference_update(ready)
    return ordered


def copy(source, dest, batch=500, out=print):
    """Copy every row of the portal at `source` into the empty portal at `dest` (both are
    migrated to the latest version first), then move dest's identity counters past the copied
    ids. Refuses when dest already has rows. Returns {table: rows}."""
    src, dst = database.connect(source), database.connect(dest)
    try:
        run(src)
        run(dst)
        tables = (src.tables() & dst.tables()) - {"schema_migrations"}
        missing = src.tables() - dst.tables() - {"schema_migrations"}
        if missing:
            raise RuntimeError("The destination has no table " + ", ".join(sorted(missing)) + ".")
        busy = [table for table in tables if dst.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone()]
        dst.commit()
        if busy:
            raise RuntimeError("The destination is not empty (" + ", ".join(sorted(busy)) + "): copy into a new database.")
        counts = {}
        with dst:
            for table in _ordered(src, tables):
                columns = [c for c in src.columns(table) if c in set(dst.columns(table))]
                types = _types(dst, table)
                names = ", ".join(f'"{c}"' for c in columns)
                insert = f'INSERT INTO "{table}" ({names}) VALUES ({", ".join("?" for _ in columns)})'
                order = " ORDER BY rowid" if src.engine == "sqlite" else ""
                rows = src.execute(f'SELECT {names} FROM "{table}"{order}').fetchall()
                for start in range(0, len(rows), batch):
                    dst.executemany(insert, [[_coerce(value, types.get(column))
                                              for column, value in zip(columns, row)]
                                             for row in rows[start:start + batch]])
                counts[table] = len(rows)
                out(f"{table}: {len(rows)}")
            if dst.engine == "postgresql":
                for table in tables:
                    if "id" not in dst.columns(table):
                        continue
                    sequence = dst.execute("SELECT pg_get_serial_sequence(?, 'id')", (table,)).fetchone()[0]
                    if sequence:  # the next id the database assigns follows the copied ones
                        dst.execute(f'SELECT setval(?, COALESCE((SELECT MAX(id) FROM "{table}"), 1), '
                                    f'(SELECT MAX(id) FROM "{table}") IS NOT NULL)', (sequence,))
        for table, count in counts.items():
            found = dst.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            if found != count:
                raise RuntimeError(f"{table}: copied {count} rows but the destination has {found}.")
        dst.commit()
        local = dst.execute("SELECT COUNT(*) FROM artifacts WHERE backend = 'local'").fetchone()[0] \
            if "artifacts" in tables else 0
        dst.commit()
        if local:  # the rows were copied, the files they refer to were not
            out(f"{local} artifacts are files in {database.state_dir(source) / 'artifacts'}: give the new portal "
                f"TWINLAB2_ARTIFACT_DIR pointing at that folder (or a copy of it).")
        return counts
    finally:
        src.close()
        dst.close()


def _types(connection, table):
    if connection.engine != "postgresql":
        return {}
    rows = connection.execute("""SELECT column_name, data_type FROM information_schema.columns
                                 WHERE table_schema = current_schema() AND table_name = ?""", (table,))
    return {row[0]: row[1] for row in rows}


def _coerce(value, kind):
    """A SQLite value (whose type follows the value, not the column) for a PostgreSQL column."""
    if value is None or kind is None:
        return value
    if kind == "bytea":
        return bytes(value, "utf-8") if isinstance(value, str) else bytes(value)
    if kind == "text" and not isinstance(value, str):
        return value.decode("utf-8") if isinstance(value, bytes) else str(value)
    if kind in ("integer", "bigint", "smallint") and isinstance(value, str):
        return int(value)
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "migrate"):
        command = commands.add_parser(name)
        command.add_argument("--database", default=None, help="URL or SQLite path (default: DATABASE_URL)")
    command = commands.add_parser("copy", help="Copy a portal into a new, empty database.")
    command.add_argument("source", help="the portal to copy, e.g. .state/catalog.db")
    command.add_argument("dest", help="the new database, e.g. postgresql://user:pass@host/twinlab2")
    args = parser.parse_args(argv)
    if args.command == "copy":
        try:
            counts = copy(args.source, args.dest)
        except (RuntimeError, *database.Error) as error:
            parser.exit(1, f"Copy failed: {error}\n")
        print(f"Copied {sum(counts.values())} rows in {len(counts)} tables to {database.redacted(args.dest)}.")
        return
    connection = database.connect(args.database)
    try:
        if args.command == "migrate":
            done = run(connection)
            print("Applied " + (", ".join(map(str, done)) if done else "nothing") + f"; at version {latest()}.")
        else:
            have = applied(connection)
            for version, name, _ in MIGRATIONS:
                print(f"{version:>4} {name:<40} {'applied' if version in have else 'pending'}")
    finally:
        connection.close()


if __name__ == "__main__":
    sys.exit(main())
