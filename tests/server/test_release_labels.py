"""A release's readable number: <builder package revision>.<build of that package>."""

import sqlite3
import unittest

from twinlab2 import registry


def database(releases, packages):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE releases (id INTEGER PRIMARY KEY, app_id INTEGER, revision TEXT)")
    db.execute("CREATE TABLE builder_packages (app_id INTEGER, rev INTEGER, revision TEXT)")
    db.executemany("INSERT INTO releases VALUES (?, ?, ?)", releases)
    db.executemany("INSERT INTO builder_packages VALUES (?, ?, ?)", packages)
    return db


class ReleaseLabelTest(unittest.TestCase):
    def test_package_revision_then_build_count(self):
        db = database([(1, 7, "r1"), (2, 7, "r1"), (3, 7, "r2"), (4, 8, "x1"), (5, 7, "r1")],
                      [(7, 1, "r1"), (7, 2, "r2"), (8, 1, "x1")])
        self.assertEqual(registry.release_labels(db, 7), {1: "1.1", 2: "1.2", 3: "2.1", 5: "1.3"})
        self.assertEqual(registry.release_labels(db, 8), {4: "1.1"})

    def test_a_brief_without_a_frozen_package_is_numbered_after_the_packages(self):
        db = database([(1, 7, "old"), (2, 7, "r1"), (3, 7, "old"), (4, 7, "older")], [(7, 1, "r1")])
        self.assertEqual(registry.release_labels(db, 7), {1: "2.1", 2: "1.1", 3: "2.2", 4: "3.1"})

    def test_no_release(self):
        self.assertEqual(registry.release_labels(database([], []), 7), {})


if __name__ == "__main__":
    unittest.main()
