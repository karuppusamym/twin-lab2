"""The database layer (db.py), numbered migrations and the SQLite → PostgreSQL copy."""

from contextlib import closing
from pathlib import Path
import tempfile
import unittest

from twinlab2 import db
from twinlab2 import migrations
from twinlab2.portal import create_app
from support import ENGINE, Client, connect, new_database


class TranslationTest(unittest.TestCase):
    def test_placeholders_and_percent_outside_quotes(self):
        self.assertEqual(db.to_pyformat("SELECT * FROM t WHERE a = ? AND b LIKE 'x%' AND c = '?'"),
                         "SELECT * FROM t WHERE a = %s AND b LIKE 'x%%' AND c = '?'")
        self.assertEqual(db.to_pyformat('SELECT "q?" FROM t WHERE n % 2 = ?'), 'SELECT "q?" FROM t WHERE n %% 2 = %s')

    def test_url_and_path(self):
        self.assertEqual(db.engine_of("postgresql://u:p@h/d"), "postgresql")
        self.assertEqual(db.engine_of(Path("x.db")), "sqlite")
        self.assertEqual(db.sqlite_path("sqlite:///.state/c.db"), Path(".state/c.db"))
        self.assertEqual(db.redacted("postgresql://u:secret@h:5432/d"), "postgresql://u:***@h:5432/d")


class LayerTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.database = new_database(self.work.name)
        create_app(self.database)

    def test_rows_returning_conflicts_and_transactions(self):
        with closing(connect(self.database)) as c:
            with c:
                first = c.execute("INSERT INTO apps(name, git_url, requirements) VALUES (?, '', '') RETURNING id",
                                  ("A 100% app",)).fetchone()[0]
            row = c.execute("SELECT id, name FROM apps WHERE name LIKE '%100%'").fetchone()
            self.assertEqual((row[0], row["name"], dict(row), row.keys()),
                             (first, "A 100% app", {"id": first, "name": "A 100% app"}, ["id", "name"]))
            self.assertEqual(row, (first, "A 100% app"))
            with c:
                c.execute("INSERT INTO recordings(id, name, events) VALUES ('r', 'R', '[]')")
                again = c.execute("INSERT INTO recordings(id, name, events) VALUES ('r', 'S', '[]') "
                                  "ON CONFLICT DO NOTHING RETURNING id").fetchone()
                self.assertIsNone(again)
                c.execute("INSERT INTO recordings(id, name, events) VALUES ('r', 'T', '[]') "
                          "ON CONFLICT(id) DO UPDATE SET name = excluded.name")
            self.assertEqual(c.execute("SELECT name FROM recordings").fetchall(), [("T",)])
            with self.assertRaises(db.IntegrityError):
                with c:
                    c.execute("INSERT INTO evidence(app_id, recording_id, selection, notes) VALUES (999, 'r', '[]', '')")
            with self.assertRaises(ZeroDivisionError):
                with c:
                    c.execute("UPDATE apps SET name = 'changed'")
                    raise ZeroDivisionError
            self.assertEqual(c.execute("SELECT name FROM apps").fetchone()[0], "A 100% app")
            self.assertEqual(c.execute("SELECT string_agg(name, ', ') FROM apps").fetchone()[0], "A 100% app")
            self.assertIn("schema_migrations", c.tables())
            self.assertIn({"from": "app_id", "table": "apps", "to": "id"}, c.foreign_keys("evidence"))
            self.assertEqual(c.columns("recordings"), ["id", "name", "events"])

    def test_migrations_run_once(self):
        with closing(connect(self.database)) as c:
            self.assertEqual(migrations.run(c), [])
            self.assertEqual({row[0] for row in c.execute("SELECT version FROM schema_migrations")},
                             {version for version, _, _ in migrations.MIGRATIONS})
        create_app(self.database)  # a second start applies nothing


class CopyTest(unittest.TestCase):
    """A portal on SQLite copied into a new database (PostgreSQL under TWINLAB2_TEST_DATABASE_URL)."""

    def test_copy_keeps_every_row_and_new_ids_follow(self):
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / "old.db"
            client = Client(create_app(source))
            for name in ("Orders", "Billing"):
                created = client.post("/api/v1/apps", json={"name": name})
                self.assertEqual(created.status_code, 201, created.text)
                # one scenario each (the old form's requirements text became one)
                scenario = client.post(f"/api/v1/apps/{created.json()['id']}/scenarios", json={"name": "Do it."})
                self.assertEqual(scenario.status_code, 201, scenario.text)
            with closing(connect(source)) as c, c:
                c.execute("INSERT INTO attachments(app_id, title, kind, extension, content) VALUES (1, 'x', 'API', '.txt', ?)",
                          (b"\x00\xffbytes",))
                c.execute("INSERT INTO recordings(id, name, events) VALUES ('r1', 'R', '[]')")
                c.execute("INSERT INTO evidence(app_id, recording_id, selection, notes) VALUES (2, 'r1', '[1]', 'n')")
            dest = new_database(Path(work) / "dest")
            (Path(work) / "dest").mkdir()
            counts = migrations.copy(str(source), str(dest), out=lambda *_: None)
            self.assertEqual((counts["apps"], counts["scenarios"], counts["evidence"], counts["attachments"]), (2, 2, 1, 1))
            with closing(connect(dest)) as c:
                self.assertEqual(bytes(c.execute("SELECT content FROM attachments").fetchone()[0]), b"\x00\xffbytes")
                with c:
                    new = c.execute("INSERT INTO apps(name, git_url, requirements) VALUES ('Third', '', '') RETURNING id"
                                    ).fetchone()[0]
                self.assertEqual(new, 3)
            with self.assertRaisesRegex(RuntimeError, "not empty"):
                migrations.copy(str(source), str(dest), out=lambda *_: None)

    def test_engine_under_test(self):
        with tempfile.TemporaryDirectory() as work:
            database = new_database(work)
            with closing(connect(database)) as c:
                self.assertEqual(c.engine, ENGINE)


if __name__ == "__main__":
    unittest.main()
