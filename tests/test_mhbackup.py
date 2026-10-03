#!/usr/bin/env python3
"""
Tests for mhbackup.

Run: python3 -m unittest discover -s tests -v
"""

import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mhbackup
import mhstore
from mhbackup import BackupError


class Backup(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.root = base / "repo"
        self.dest = base / "drive"
        self.dest.mkdir()
        self.store = mhstore.open_store(self.root)
        self.store.add_project("p", "P")
        self.store.add_task("p", "one")
        self.audit = (self.root / "operations" / "tasks-audit.log").read_text()

    def tearDown(self):
        self.store.conn.close()
        self._tmp.cleanup()

    def test_snapshot_includes_writes_still_in_the_wal(self):
        # The store holds its connection open, so the insert may be only in
        # the -wal file. A plain copy of tasks.db could miss it.
        tasks, _ = mhbackup.backup(self.root, self.dest, today=date(2026, 10, 3))
        self.assertEqual(tasks, 1)
        copy = sqlite3.connect(self.dest / "tasks-2026-10-03.db")
        self.assertEqual(copy.execute("SELECT title FROM tasks").fetchone()[0], "one")
        copy.close()
        self.assertEqual((self.dest / "tasks-audit-2026-10-03.log").read_text(), self.audit)
        self.assertEqual(sorted(p.name for p in self.dest.iterdir()),
                         ["tasks-2026-10-03.db", "tasks-audit-2026-10-03.log"])

    def test_restored_snapshot_is_a_working_store(self):
        # The README's restore steps, on a repo that has never seen the store.
        mhbackup.backup(self.root, self.dest, today=date(2026, 10, 3))
        other = Path(self._tmp.name) / "other-laptop"
        (other / "operations").mkdir(parents=True)
        shutil.copyfile(self.dest / "tasks-2026-10-03.db",
                        other / "operations" / "tasks.db")
        restored = mhstore.open_store(other)
        try:
            self.assertEqual([t["title"] for t in restored.tasks("p")], ["one"])
            restored.add_task("p", "two")
            self.assertEqual(len(restored.tasks("p")), 2)
        finally:
            restored.conn.close()

    def test_missing_destination_fails_instead_of_creating_it(self):
        gone = self.dest / "not-mounted"
        with self.assertRaises(BackupError):
            mhbackup.backup(self.root, gone)
        self.assertFalse(gone.exists())

    def test_missing_store_fails(self):
        with self.assertRaises(BackupError):
            mhbackup.backup(self.dest, self.dest)

    def test_prune_keeps_newest_days_and_their_logs(self):
        for d in range(1, 6):
            mhbackup.backup(self.root, self.dest, today=date(2026, 10, d), keep=3)
        names = sorted(p.name for p in self.dest.iterdir())
        self.assertEqual(names, [
            "tasks-2026-10-03.db", "tasks-2026-10-04.db", "tasks-2026-10-05.db",
            "tasks-audit-2026-10-03.log", "tasks-audit-2026-10-04.log",
            "tasks-audit-2026-10-05.log",
        ])

    def test_main_reports_failure_with_exit_code(self):
        self.assertEqual(mhbackup.main(["--root", str(self.root),
                                        "--dest", str(self.dest / "nope")]), 1)
        self.assertEqual(mhbackup.main(["--root", str(self.root),
                                        "--dest", str(self.dest)]), 0)


if __name__ == "__main__":
    unittest.main()
