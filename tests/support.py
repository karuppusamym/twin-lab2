"""The database a test runs on.

By default every test gets a new SQLite file in its temporary directory. With
`TWINLAB2_TEST_DATABASE_URL=postgresql://user:password@host:port/postgres` every test gets a new,
empty PostgreSQL database on that server instead (created from that URL's database, dropped when
the test run ends), so the whole suite runs on PostgreSQL:

    docker run -d --name twinlab2-pg -e POSTGRES_PASSWORD=twinlab2 -p 55432:5432 postgres:16
    TWINLAB2_TEST_DATABASE_URL=postgresql://postgres:twinlab2@localhost:55432/postgres pytest

Tests of catalogs made by older portals stay on SQLite: those catalogs only exist as SQLite files.
"""

import atexit
import os
from pathlib import Path
import uuid
from urllib.parse import urlsplit, urlunsplit

from db import connect  # noqa: F401 - tests open the database with it

URL = os.environ.get("TWINLAB2_TEST_DATABASE_URL", "").strip()
ENGINE = "postgresql" if URL else "sqlite"
_created = []


def _admin():
    import psycopg
    return psycopg.connect(URL, autocommit=True)


def new_database(directory):
    """A new, empty database for one test: a SQLite file in `directory`, or a PostgreSQL URL."""
    if not URL:
        return Path(directory) / "catalog.db"
    name = "twinlab2_test_" + uuid.uuid4().hex[:16]
    with _admin() as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    _created.append(name)
    parts = urlsplit(URL)
    return urlunsplit((parts.scheme, parts.netloc, "/" + name, parts.query, ""))


@atexit.register
def _drop():
    if not _created:
        return
    import shutil
    from db import state_dir

    parts = urlsplit(URL)
    with _admin() as admin:
        for name in _created:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            # the database's local artifact files (db.state_dir)
            url = urlunsplit((parts.scheme, parts.netloc, "/" + name, parts.query, ""))
            shutil.rmtree(state_dir(url), ignore_errors=True)
