"""Offline candidate-list checks using a temporary directory tree."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import archive_plan as a


class ArchiveTests(unittest.TestCase):
    def test_database_family_matrix(self):
        names = []
        for stem in ("store", "with space", "nested/state"):
            for extension in (".db", ".sqlite", ".sqlite3", ".db3", ".s3db", ".sl3", ""):
                for suffix in ("", "-wal", "-shm", "-journal", "-mj1234abcd"):
                    names.extend((stem + extension + suffix, (stem + extension + suffix).upper()))
        entries = [{"path": name} for name in names]
        self.assertEqual(a.database_family_paths(entries), set(names))
        ordinary = [{"path": name} for name in ("README.md", "schema.sql", "notes.sqlite3-wal.txt")]
        self.assertEqual(a.database_family_paths(ordinary), set())

    def test_links_are_retained_and_database_files_separated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            (project / ".local").mkdir(parents=True)
            source = root / "source"
            data = source / "data"
            data.mkdir(parents=True)
            (data / "result.json").write_text("{}")
            (data / "store.db").write_bytes(b"not opened")
            (data / "store.db-wal").write_bytes(b"not opened")
            for name in (
                "other.sqlite3-wal",
                "other.sqlite3-shm",
                "store.db-journal",
                "other.sqlite-journal",
                "bare-journal",
                "bare",
                "store.db-mj1234abcd",
            ):
                (data / name).write_bytes(b"not opened")
            (data / "active.md").write_text("See result.json and schema.sql")
            (data / ".env").write_text("not opened")
            (data / "alias").symlink_to(data / "result.json")
            (project / ".local/archive.json").write_text(
                json.dumps(
                    {"sources": [{"name": "sample", "root": str(source), "include": ["data"]}]}
                )
            )
            with (
                patch.object(a, "__file__", str(project / "scripts/archive_plan.py")),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(a.main(), 0)
            report = json.loads(next((project / "manifests").glob("*.json")).read_text())
            self.assertEqual(
                {x["path"] for x in report["files"]}, {"data/result.json", "data/active.md"}
            )
            self.assertEqual(len(report["database_files_excluded"]), 9)
            self.assertEqual(len(report["links_not_followed"]), 1)
            self.assertFalse(report["transfer_authorized"])
            self.assertFalse(report["hashes_computed"])
            self.assertFalse(report["deletion_authorized"])
            self.assertEqual(report["dependency_analysis"], "not_performed")
            self.assertFalse(report["application_restart_verified"])


if __name__ == "__main__":
    unittest.main()
