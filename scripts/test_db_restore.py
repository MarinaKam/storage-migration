"""Offline restore-boundary tests with generated SQLite data and a process double."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db_backup as b
import db_restore as r


class RestoreTests(unittest.TestCase):
    def test_sqlite_restore_uses_backup_without_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "original.db"
            connection = sqlite3.connect(original)
            connection.execute("CREATE TABLE sample(v TEXT)")
            connection.execute("INSERT INTO sample VALUES('preserved')")
            connection.commit()
            connection.close()
            folder = root / "backup"
            folder.mkdir()
            record = b.snapshot_sqlite(original, folder / "snapshot.sqlite")
            (folder / "verification.json").write_text(json.dumps(record))
            original.unlink()  # Generated fixture only: prove restore does not read original.
            source, verified = r.verify_backup(folder)
            result = r.restore_sqlite(source, verified, root / "restored")
            self.assertTrue(result["independent_reopen_verified"])
            self.assertFalse(result["application_reads_verified"])
            with self.assertRaises(FileExistsError):
                r.restore_sqlite(source, verified, root / "restored")
            source.write_bytes(b"corrupt")
            with self.assertRaises(ValueError):
                r.verify_backup(folder)

    def test_postgres_executes_restore_then_restart_and_retains_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "snapshot.dump"
            source.write_bytes(b"PGDMPfixture")
            calls = []
            with (
                patch.object(r, "run", side_effect=lambda args, **kwargs: calls.append(args)),
                patch.object(r, "wait_ready"),
                patch.object(
                    r, "pg_snapshot", return_value={"sample": {"rows": 1, "sha256": "same"}}
                ),
            ):
                result = r.restore_postgres(source, 17)
            self.assertTrue(result["table_data_after_restart_equal"])
            self.assertFalse(result["application_reads_verified"])
            self.assertTrue(
                any("pg_restore" in cmd and "--single-transaction" in cmd for cmd in calls)
            )
            self.assertTrue(any("--network=none" in cmd for cmd in calls))
            self.assertTrue(any("restart" in cmd for cmd in calls))
            self.assertFalse(any("rm" in cmd or "dropdb" in cmd for cmd in calls))

    def test_postgres_restart_mismatch_is_not_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "snapshot.dump"
            source.write_bytes(b"PGDMPfixture")
            with (
                patch.object(r, "run"),
                patch.object(r, "wait_ready"),
                patch.object(r, "pg_snapshot", side_effect=[{"a": 1}, {"a": 2}]),
                self.assertRaises(ValueError),
            ):
                r.restore_postgres(source, 17)


if __name__ == "__main__":
    unittest.main()
