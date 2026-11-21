"""The portal database: SQLite for local development and tests, PostgreSQL for a shared deployment.

`connect(target)` takes a `DATABASE_URL` (`sqlite:///path/to/catalog.db`, `postgresql://...`) or
a plain file path (SQLite) and returns a `Connection` that behaves like `sqlite3.Connection` on
both engines:

- SQL is written once with `?` placeholders; on PostgreSQL they become `%s` (and a literal `%`
  becomes `%%`), outside quoted strings and identifiers;
- rows are `Row`s: by position (`row[0]`), by column name (`row["name"]`), `dict(row)`, `keys()`;
- `with connection:` commits on success and rolls back on an exception (it does not close);
- `IntegrityError` catches a constraint violation from either engine;
- `columns(table)`, `tables()` and `foreign_keys(table)` replace `PRAGMA table_info`,
  `sqlite_master` and `PRAGMA foreign_key_list`;
- DDL passed through `ddl()` may use `{id}` (an integer primary key the database assigns) and
  `{blob}` (BLOB / BYTEA).

The SQL itself must stay portable: see "Portable SQL rules" in the README.
"""

from __future__ import annotations

from functools import lru_cache
import hashlib
import os
from pathlib import Path
import sqlite3

from twinlab2.paths import STATE

DEFAULT_PATH = STATE / "catalog.db"

try:  # PostgreSQL support is optional for local development
    import psycopg
    import psycopg.errors
except ImportError:  # pragma: no cover - exercised only without psycopg installed
    psycopg = None

IntegrityError = (sqlite3.IntegrityError,) + ((psycopg.errors.IntegrityError,) if psycopg else ())
Error = (sqlite3.Error,) + ((psycopg.Error,) if psycopg else ())


def from_env():
    """The configured database: `DATABASE_URL`, else the `TWINLAB2_DATABASE` file, else
    .state/catalog.db next to the portal."""
    return os.environ.get("DATABASE_URL") or os.environ.get("TWINLAB2_DATABASE") or str(DEFAULT_PATH)


def engine_of(target):
    """`postgresql` or `sqlite` for a URL or path."""
    text = str(target)
    return "postgresql" if text.startswith(("postgresql://", "postgres://")) else "sqlite"


def sqlite_path(target):
    """The file of a SQLite URL or path."""
    text = str(target)
    if text.startswith("sqlite:///"):
        return Path(text[len("sqlite:///"):])
    if text.startswith("sqlite://"):
        raise ValueError("Use sqlite:///relative/path or sqlite:////absolute/path.")
    return Path(text)


def state_dir(target):
    """Where files that belong with a database live (local artifacts, the dev worker's log): the
    SQLite file's folder, or for PostgreSQL .state/pg-<12 hex of host, port and database> next
    to the portal (one folder per database, so test databases stay apart; short, for Windows
    paths)."""
    text = str(target)
    if engine_of(text) == "sqlite":
        return sqlite_path(text).parent
    from urllib.parse import urlsplit
    parts = urlsplit(text)
    name = f"{parts.hostname or 'local'}:{parts.port or 5432}/{parts.path.strip('/') or 'postgres'}"
    return DEFAULT_PATH.parent / ("pg-" + hashlib.sha256(name.encode()).hexdigest()[:12])


def redacted(target):
    """A URL fit for logs: no password."""
    text = str(target)
    if engine_of(text) != "postgresql" or "@" not in text:
        return text
    scheme, rest = text.split("://", 1)
    credentials, host = rest.rsplit("@", 1)
    return f"{scheme}://{credentials.split(':', 1)[0]}:***@{host}"


class Row:
    """A result row: by index, by column name, `dict(row)`, iteration, `keys()`."""

    __slots__ = ("_values", "_index")

    def __init__(self, index, values):
        self._index = index
        self._values = tuple(values)

    def __getitem__(self, key):
        if isinstance(key, str):
            return self._values[self._index[key]]
        return self._values[key]

    def keys(self):
        return list(self._index)

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __eq__(self, other):
        if isinstance(other, Row):
            return self._values == other._values and self.keys() == other.keys()
        return self._values == other

    def __hash__(self):
        return hash(self._values)

    def __repr__(self):
        return f"Row({dict(zip(self._index, self._values))!r})"


@lru_cache(maxsize=512)
def _index(names):
    index = {}
    for position, name in enumerate(names):
        index.setdefault(name, position)  # like sqlite3.Row: the first column of a name wins
    return index


def _row(description, values):
    return Row(_index(tuple(column[0] for column in description)), values)


@lru_cache(maxsize=2048)
def to_pyformat(sql):
    """`?` placeholders to `%s` and `%` to `%%`, leaving quoted strings and identifiers alone
    (a `%` in them is still doubled: psycopg reads the whole statement)."""
    out, quote = [], None
    for char in sql:
        if quote:
            out.append("%%" if char == "%" else char)
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
            out.append(char)
        elif char == "?":
            out.append("%s")
        elif char == "%":
            out.append("%%")
        else:
            out.append(char)
    return "".join(out)


class Cursor:
    """The result of `Connection.execute`, as sqlite3's cursor."""

    def __init__(self, cursor, engine):
        self._cursor = cursor
        self._engine = engine

    def _wrap(self, values):
        return None if values is None else _row(self._cursor.description, values)

    def fetchone(self):
        if self._cursor.description is None:
            return None
        return self._wrap(self._cursor.fetchone())

    def fetchall(self):
        if self._cursor.description is None:
            return []
        return [_row(self._cursor.description, values) for values in self._cursor.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def lastrowid(self):
        if self._engine != "sqlite":
            raise AttributeError("lastrowid is SQLite only: add RETURNING id and read the row.")
        return self._cursor.lastrowid

    @property
    def description(self):
        return self._cursor.description


class _StringAgg:
    """string_agg(value, separator) for SQLite builds older than 3.44."""

    def __init__(self):
        self.parts, self.separator = [], ","

    def step(self, value, separator):
        if value is not None:
            self.parts.append(str(value))
            self.separator = separator

    def finalize(self):
        return self.separator.join(self.parts) if self.parts else None


class Connection:
    """One database connection with sqlite3's interface, on SQLite or PostgreSQL."""

    def __init__(self, raw, engine, target):
        self.raw = raw
        self.engine = engine
        self.target = target

    # -- statements ----------------------------------------------------------------------------

    def execute(self, sql, params=()):
        if self.engine == "sqlite":
            return Cursor(self.raw.execute(sql, params), "sqlite")
        cursor = self.raw.cursor()
        cursor.execute(to_pyformat(sql), tuple(params))
        return Cursor(cursor, self.engine)

    def executemany(self, sql, rows):
        if self.engine == "sqlite":
            return Cursor(self.raw.executemany(sql, rows), "sqlite")
        cursor = self.raw.cursor()
        cursor.executemany(to_pyformat(sql), [tuple(row) for row in rows])
        return Cursor(cursor, self.engine)

    def executescript(self, script):
        """Several statements without parameters (SQLite: commits first, like sqlite3)."""
        if self.engine == "sqlite":
            return self.raw.executescript(script)
        return self.raw.execute(script)

    def ddl(self, sql):
        """Run one DDL statement, with `{id}` and `{blob}` for this engine."""
        if self.engine == "sqlite":
            sql = sql.replace("{id}", "INTEGER PRIMARY KEY").replace("{blob}", "BLOB")
        else:
            sql = sql.replace("{id}", "INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY")
            sql = sql.replace("{blob}", "BYTEA")
        return self.raw.execute(sql)

    # -- transactions --------------------------------------------------------------------------

    def begin(self):
        """Start a transaction now (SQLite: take the write lock now; PostgreSQL: the next
        statement starts one anyway)."""
        if self.engine == "sqlite" and not self.raw.in_transaction:
            self.raw.execute("BEGIN IMMEDIATE")

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        if kind is None:
            self.commit()
        else:
            self.rollback()
        return False

    # -- the schema ----------------------------------------------------------------------------

    def tables(self):
        """The names of the application's tables."""
        if self.engine == "sqlite":
            rows = self.raw.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
            return {row[0] for row in rows}
        rows = self.execute("""SELECT table_name FROM information_schema.tables
                               WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'""")
        return {row[0] for row in rows}

    def columns(self, table):
        """The column names of a table, in order (empty when there is no such table)."""
        if self.engine == "sqlite":
            return [row[1] for row in self.raw.execute(f'PRAGMA table_info("{table}")')]
        rows = self.execute("""SELECT column_name FROM information_schema.columns
                               WHERE table_schema = current_schema() AND table_name = ?
                               ORDER BY ordinal_position""", (table,))
        return [row[0] for row in rows]

    def foreign_keys(self, table):
        """The table's foreign keys as dicts {"from", "table", "to"} (one per column)."""
        if self.engine == "sqlite":
            return [{"from": row[3], "table": row[2], "to": row[4]}
                    for row in self.raw.execute(f'PRAGMA foreign_key_list("{table}")')]
        rows = self.execute("""
            SELECT a.attname, ref.relname, b.attname
            FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            JOIN pg_class ref ON ref.oid = c.confrelid
            CROSS JOIN LATERAL unnest(c.conkey, c.confkey) AS k(col, refcol)
            JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.col
            JOIN pg_attribute b ON b.attrelid = c.confrelid AND b.attnum = k.refcol
            WHERE c.contype = 'f' AND t.relname = ? AND n.nspname = current_schema()
            ORDER BY c.conname, a.attnum
        """, (table,))
        return [{"from": row[0], "table": row[1], "to": row[2]} for row in rows]


def connect(target=None):
    """A connection to `target` (a URL or a SQLite path; default `from_env()`)."""
    target = str(target if target is not None else from_env())
    if engine_of(target) == "postgresql":
        if psycopg is None:
            raise RuntimeError("PostgreSQL needs psycopg: pip install 'psycopg[binary]'.")
        raw = psycopg.connect(target, autocommit=False)
        return Connection(raw, "postgresql", target)
    path = sqlite_path(target)
    # the worker and the portal share the file; a web request may use its connection from more
    # than one thread of the server (one at a time), so it is not tied to the thread that opened it
    raw = sqlite3.connect(path, timeout=30, check_same_thread=False)
    raw.execute("PRAGMA foreign_keys = ON")
    if sqlite3.sqlite_version_info < (3, 44, 0):
        raw.create_aggregate("string_agg", 2, _StringAgg)
    return Connection(raw, "sqlite", target)
