"""Restore a verified backup into an isolated local destination; never replace a source."""

import argparse
import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from db_backup import digest, sqlite_details


def verify_backup(folder):
    record = json.loads((folder / "verification.json").read_text())
    candidates = [p for p in (folder / "snapshot.sqlite", folder / "snapshot.dump") if p.exists()]
    if len(candidates) != 1 or candidates[0].is_symlink():
        raise ValueError("Select exactly one regular backup snapshot")
    source = candidates[0]
    if source.stat().st_size != record["bytes"] or digest(source) != record["sha256"]:
        raise ValueError("Backup checksum or size differs")
    return source, record


def restore_sqlite(source, record, destination):
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    target = destination / "restored.sqlite"
    with source.open("rb") as src, target.open("xb") as dst:
        shutil.copyfileobj(src, dst)
    if digest(target) != record["sha256"]:
        raise ValueError("Restored SQLite checksum differs")
    # Each call opens and closes a separate read-only connection.
    first, reopened = sqlite_details(target), sqlite_details(target)
    expected = {k: record[k] for k in ("tables", "user_version")}
    if first != expected or reopened != expected:
        raise ValueError("Restored SQLite metadata differs")
    return {
        "destination": str(target),
        "database_read_verified": True,
        "independent_reopen_verified": True,
        "tables": reopened["tables"],
        "application_reads_verified": False,
        "remote_restart_verified": False,
    }


def run(args, **kwargs):
    return subprocess.run(args, check=True, stderr=subprocess.PIPE, timeout=600, **kwargs)


def pg_read(container, sql):
    return run(
        [
            "docker",
            "exec",
            container,
            "psql",
            "-X",
            "-q",
            "-w",
            "-U",
            "postgres",
            "-d",
            "restorecheck",
            "-Atc",
            sql,
        ],
        stdout=subprocess.PIPE,
    ).stdout


def pg_snapshot(container):
    import hashlib

    tables = json.loads(
        pg_read(
            container,
            "SELECT coalesce(json_agg(x ORDER BY schemaname, tablename), '[]') FROM "
            "(SELECT schemaname, tablename FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema')) x",
        )
    )
    fingerprints = {}
    for table in tables:
        identifier = ".".join(
            '"' + table[k].replace('"', '""') + '"' for k in ("schemaname", "tablename")
        )
        # Sorted serialized rows make this independent of physical row order.
        data = pg_read(
            container,
            "COPY (SELECT row_to_json(t)::text AS row FROM "
            + identifier
            + ' t ORDER BY row_to_json(t)::text COLLATE "C") TO STDOUT',
        )
        count = int(pg_read(container, "SELECT count(*) FROM " + identifier))
        fingerprints[identifier] = {"rows": count, "sha256": hashlib.sha256(data).hexdigest()}
    return fingerprints


def wait_ready(container):
    for _ in range(60):
        result = subprocess.run(
            ["docker", "exec", container, "pg_isready", "-U", "postgres", "-d", "restorecheck"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            return
        time.sleep(1)
    raise TimeoutError("Isolated database did not become ready")


def restore_postgres(source, major):
    if not 10 <= major <= 99:
        raise ValueError("Invalid PostgreSQL major version")
    image = f"postgres:{major}"
    # Do not pull images or expose a port; the target cannot connect to old services.
    run(["docker", "image", "inspect", image], stdout=subprocess.DEVNULL)
    container = "restore-check-" + uuid.uuid4().hex[:12]
    run(
        [
            "docker",
            "run",
            "--detach",
            "--pull=never",
            "--name",
            container,
            "--network=none",
            "--label",
            "storage-migration-purpose=restore-check",
            "--env",
            "POSTGRES_HOST_AUTH_METHOD=trust",
            "--env",
            "POSTGRES_DB=restorecheck",
            image,
        ],
        stdout=subprocess.DEVNULL,
    )
    print("Isolated restore container: " + container, flush=True)
    wait_ready(container)
    with source.open("rb") as stream:
        run(
            [
                "docker",
                "exec",
                "-i",
                container,
                "pg_restore",
                "-w",
                "-U",
                "postgres",
                "-d",
                "restorecheck",
                "--exit-on-error",
                "--single-transaction",
                "--no-owner",
                "--no-privileges",
            ],
            stdin=stream,
            stdout=subprocess.DEVNULL,
        )
    before = pg_snapshot(container)
    run(["docker", "restart", container], stdout=subprocess.DEVNULL)
    wait_ready(container)
    after = pg_snapshot(container)
    if before != after:
        raise ValueError("Restored PostgreSQL data differs after restart")
    return {
        "destination_container": container,
        "database_restore_verified": True,
        "table_data_after_restart_equal": True,
        "table_fingerprints": after,
        "ownership_and_privileges_restored": False,
        "application_reads_verified": False,
        "railway_verified": False,
        "note": "Local isolated rehearsal only. Container retained. No source database was modified.",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument("--major", type=int, default=17)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    try:
        folder = args.backup.resolve(strict=True)
        source, record = verify_backup(folder)
        destination = project / ".local/restores" / uuid.uuid4().hex
        if source.suffix == ".sqlite":
            result = restore_sqlite(source, record, destination)
        else:
            destination.mkdir(parents=True, mode=0o700, exist_ok=False)
            result = restore_postgres(source, args.major)
        if digest(source) != record["sha256"]:
            raise ValueError("Backup changed during restore")
        (destination / "restore-result.json").write_text(json.dumps(result, indent=2))
        print("Restore result: " + str(destination / "restore-result.json"))
        print(
            "Database restore rehearsal only. Application reads and Railway operation remain unverified. No sources deleted."
        )
        return 0
    except Exception as error:  # noqa: BLE001 - do not expose connection details or database contents
        print(
            "Restore not verified: "
            + type(error).__name__
            + ". Isolated outputs retained; source not overwritten."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
