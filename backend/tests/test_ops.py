"""A backup must include committed WAL pages, restore exactly, and retain safe copies."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from app.ops import BACKUP_NAME, backup, backup_health, restore


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "orders.sqlite3"
        self.backups = self.root / "backups"
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, quantity INTEGER)")
            connection.execute("INSERT INTO orders VALUES (1, 12)")
            connection.commit()

    def quantity(self, path):
        with closing(sqlite3.connect(path)) as connection:
            return connection.execute("SELECT quantity FROM orders WHERE id=1").fetchone()[0]

    def test_online_backup_reads_committed_wal_pages(self):
        with closing(sqlite3.connect(self.database)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("UPDATE orders SET quantity=37")
            writer.commit()
            self.assertTrue(Path(str(self.database) + "-wal").exists())
            copy = backup(self.backups, source=self.database)
            self.assertEqual(37, self.quantity(copy))
        self.assertTrue(backup_health(self.backups))

    def test_restore_preserves_pre_restore_database_and_content(self):
        copy = backup(self.backups, source=self.database)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("UPDATE orders SET quantity=99")
            connection.commit()
        previous = restore(copy, target=self.database, services_stopped=True)
        self.assertEqual(12, self.quantity(self.database))
        self.assertEqual(99, self.quantity(previous))

    def test_retention_deletes_only_own_backups(self):
        self.backups.mkdir()
        unrelated = self.backups / "business.sqlite3"
        unrelated.write_text("keep")
        for _ in range(16):
            backup(self.backups, source=self.database)
        copies = [p for p in self.backups.iterdir() if BACKUP_NAME.fullmatch(p.name)]
        self.assertEqual(14, len(copies))
        self.assertTrue(unrelated.exists())
        self.assertFalse(any(p.name.startswith(".umytpa") for p in self.backups.iterdir()))

    def test_corrupt_backup_cannot_overwrite_database(self):
        corrupt = self.root / "corrupt.sqlite3"
        corrupt.write_text("not a database")
        with self.assertRaises(sqlite3.DatabaseError):
            restore(corrupt, target=self.database, services_stopped=True)
        self.assertEqual(12, self.quantity(self.database))

    def test_restore_requires_stopped_services_and_no_wal_clients(self):
        copy = backup(self.backups, source=self.database)
        with self.assertRaises(ValueError):
            restore(copy, target=self.database, services_stopped=False)
        with closing(sqlite3.connect(self.database)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("UPDATE orders SET quantity=17")
            writer.commit()
            with self.assertRaisesRegex(ValueError, "WAL sidecars"):
                restore(copy, target=self.database, services_stopped=True)
        self.assertEqual(17, self.quantity(self.database))

    def test_missing_database_and_stale_backup_are_not_healthy(self):
        with self.assertRaises(FileNotFoundError):
            backup(self.backups, source=self.root / "absent.sqlite3")
        self.assertFalse(backup_health(self.backups))
        copy = backup(self.backups, source=self.database)
        os.utime(copy, (1, 1))
        self.assertFalse(backup_health(self.backups))


if __name__ == "__main__":
    unittest.main()
