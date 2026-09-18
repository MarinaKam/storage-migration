"""Operator-run SQLite snapshots and PostgreSQL custom dumps. No deployment."""

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from contextlib import closing
from pathlib import Path


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def sqlite_details(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("SQLite integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("SQLite foreign-key check failed")
        names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        counts = {}
        for name in names:
            quoted = '"' + name.replace('"', '""') + '"'
            counts[name] = connection.execute("SELECT count(*) FROM " + quoted).fetchone()[0]
        return {
            "tables": counts,
            "user_version": connection.execute("PRAGMA user_version").fetchone()[0],
        }


def snapshot_sqlite(source, destination, timeout=180):
    if not source.is_absolute() or source.is_symlink() or not source.is_file():
        raise ValueError("Invalid SQLite source")
    if destination.exists():
        raise ValueError("Backup destination already exists")
    partial = destination.with_suffix(".partial")
    with partial.open("xb"):
        pass
    started = time.monotonic()

    def progress(status, remaining, total):
        if time.monotonic() - started > timeout:
            raise TimeoutError("SQLite backup deadline exceeded")

    # Failed partial output is retained; the source is never deleted.
    with (
        closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src,
        closing(sqlite3.connect(partial)) as dst,
    ):
        src.backup(dst, pages=256, progress=progress, sleep=0.1)
        dst.execute("PRAGMA journal_mode=DELETE")
    result = sqlite_details(partial)
    # Restore to a separate local file and reopen independently.
    restored = destination.with_suffix(".restore-check")
    with partial.open("rb") as src, restored.open("xb") as dst:
        shutil.copyfileobj(src, dst)
    if digest(restored) != digest(partial) or sqlite_details(restored) != result:
        raise ValueError("SQLite restored snapshot differs")
    result["sha256"] = digest(partial)
    result["bytes"] = partial.stat().st_size
    result["verification"] = "snapshot_integrity_and_local_restore"
    with partial.open("rb") as stream:
        os.fsync(stream.fileno())
    os.link(partial, destination)
    partial.unlink()
    restored.unlink()  # Only this command's temporary verification copy.
    return result


def dump_postgres(config, destination):
    container, database, user = (config[k] for k in ("container", "database", "user"))
    if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", value) for value in (container, database, user)):
        raise ValueError("Invalid local container database configuration")
    base = ["docker", "exec", container]
    version = subprocess.run(
        base + ["pg_dump", "--version"], capture_output=True, check=True, timeout=30
    ).stdout.decode()
    expected_major = config["major"]
    if type(expected_major) is not int or not 10 <= expected_major <= 99:
        raise ValueError("Invalid PostgreSQL major version")
    if not re.search(r"\b" + str(expected_major) + r"\.", version):
        raise ValueError("PostgreSQL client major differs from configured version")
    server = subprocess.run(
        base + ["psql", "-X", "-w", "-U", user, "-d", database, "-Atc", "SHOW server_version_num"],
        capture_output=True,
        check=True,
        timeout=30,
    ).stdout.strip()
    if int(server) // 10000 != expected_major:
        raise ValueError("PostgreSQL server/client major mismatch")
    partial = destination.with_suffix(".partial")
    with partial.open("xb") as stream:
        subprocess.run(
            base + ["pg_dump", "-w", "-U", user, "-d", database, "--format=custom"],
            stdout=stream,
            stderr=subprocess.PIPE,
            check=True,
            timeout=600,
        )
        stream.flush()
        os.fsync(stream.fileno())
    with partial.open("rb") as stream:
        header = stream.read(5)
    if partial.stat().st_size < 5 or header != b"PGDMP":
        raise ValueError("Invalid PostgreSQL custom archive")
    # Same-major parser inside the existing container; no destination database touched.
    with partial.open("rb") as stream:
        subprocess.run(
            ["docker", "exec", "-i", container, "pg_restore", "--list"],
            stdin=stream,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=True,
            timeout=60,
        )
    os.link(partial, destination)
    partial.unlink()
    return {
        "sha256": digest(destination),
        "bytes": destination.stat().st_size,
        "verification": "custom_archive_catalog_readable",
        "restore_verified": False,
        "note": "A database restore and application checks are still required; roles and globals are not included.",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--name")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    try:
        entries = json.loads((project / ".local/databases.json").read_text())["databases"]
        if args.plan:
            for name, entry in entries.items():
                print(
                    f"{name}: {entry['engine']}; scope={entry.get('scope', 'unconfirmed')}; configured={entry.get('configured', False)}"
                )
            print("Plan only. No database connections, backups or cloud calls.")
            return 0
        if args.name not in entries or not re.fullmatch(r"[a-z0-9-]+", args.name or ""):
            raise ValueError("Choose a configured database name")
        config = entries[args.name]
        if not config.get("configured"):
            raise ValueError("Database source is not configured")
        stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S%fZ")
        target = project / ".local/backups" / args.name / stamp
        target.mkdir(parents=True, mode=0o700)
        if config["engine"] == "sqlite":
            result = snapshot_sqlite(Path(config["path"]), target / "snapshot.sqlite")
        elif config["engine"] == "postgres-container":
            result = dump_postgres(config, target / "snapshot.dump")
        else:
            raise ValueError("Unsupported database engine")
        (target / "verification.json").write_text(json.dumps(result, indent=2))
        print(f"Backup: {target}; bytes={result['bytes']}; verification={result['verification']}")
        print("No source data deleted, no remote deployment and no R2 upload.")
        return 0
    except Exception as error:  # noqa: BLE001 - suppress connection details and raw database diagnostics
        print(
            "Backup not completed: "
            + type(error).__name__
            + ". Source data unchanged; inspect configuration locally."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
