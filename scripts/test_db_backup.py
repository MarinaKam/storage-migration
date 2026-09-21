"""Offline database backup checks; no live database or container is accessed."""

import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import db_backup as b
from hash_support import sha256_of


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_sqlite_committed_wal_and_restore(self):
        source = self.root / "source.db"
        with closing(sqlite3.connect(source)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE rows (id INTEGER PRIMARY KEY, value TEXT)")
            conn.execute("INSERT INTO rows VALUES (1, 'retained')")
            conn.commit()
            backup = self.root / "snapshot.sqlite"
            result = b.snapshot_sqlite(source, backup)
            self.assertEqual(result["tables"]["rows"], 1)
            with closing(sqlite3.connect(backup)) as restored:
                self.assertEqual(
                    restored.execute("SELECT * FROM rows").fetchall(), [(1, "retained")]
                )
            self.assertEqual(result["sha256"], sha256_of(backup).removeprefix("sha256:"))
            self.assertEqual(conn.execute("SELECT count(*) FROM rows").fetchone()[0], 1)

    def test_existing_destination_and_symlink_rejected(self):
        source = self.root / "source.db"
        with closing(sqlite3.connect(source)) as conn:
            conn.execute("CREATE TABLE t (v TEXT)")
        target = self.root / "snapshot.sqlite"
        target.write_bytes(b"keep")
        with self.assertRaises(ValueError):
            b.snapshot_sqlite(source, target)
        self.assertEqual(target.read_bytes(), b"keep")
        link = self.root / "link.db"
        link.symlink_to(source)
        with self.assertRaises(ValueError):
            b.snapshot_sqlite(link, self.root / "other.sqlite")

    def test_foreign_key_violation_is_not_published(self):
        source = self.root / "source.db"
        with closing(sqlite3.connect(source)) as conn:
            conn.executescript(
                "CREATE TABLE parent(id INTEGER PRIMARY KEY); CREATE TABLE child(id REFERENCES parent(id)); INSERT INTO child VALUES(5);"
            )
        target = self.root / "snapshot.sqlite"
        with self.assertRaises(ValueError):
            b.snapshot_sqlite(source, target)
        self.assertFalse(target.exists())

    def test_postgres_archive_check_is_not_restore_claim(self):
        config = {"container": "fixture", "database": "sample", "user": "sample", "major": 17}

        def fake(args, **kwargs):
            if "--version" in args:
                return subprocess.CompletedProcess(args, 0, b"pg_dump (PostgreSQL) 17.6")
            if "psql" in args:
                return subprocess.CompletedProcess(args, 0, b"170006")
            if "--format=custom" in args:
                kwargs["stdout"].write(b"PGDMPfixture")
            if "--list" in args:
                self.assertEqual(kwargs["stdin"].read(), b"PGDMPfixture")
            return subprocess.CompletedProcess(args, 0, b"")

        with patch.object(b.subprocess, "run", side_effect=fake):
            result = b.dump_postgres(config, self.root / "snapshot.dump")
        self.assertFalse(result["restore_verified"])
        self.assertEqual(result["verification"], "custom_archive_catalog_readable")

    def test_postgres_wrong_major_stops_before_dump(self):
        config = {"container": "fixture", "database": "sample", "user": "sample", "major": 17}
        with (
            patch.object(
                b.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, b"pg_dump (PostgreSQL) 15.13"),
            ) as run,
            self.assertRaises(ValueError),
        ):
            b.dump_postgres(config, self.root / "snapshot.dump")
        self.assertEqual(run.call_count, 1)
        self.assertFalse((self.root / "snapshot.dump").exists())


if __name__ == "__main__":
    unittest.main()
