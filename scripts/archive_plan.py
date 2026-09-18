"""Build an offline candidate inventory, retaining link relationships separately."""

import datetime
import json
import re
import time
from pathlib import Path

from inventory import scan

DATABASE_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".db3", ".s3db", ".sl3")
SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def sqlite_family_base(path):
    """Conservative filename classification, including extensionless database sidecars."""
    value = str(path).casefold()
    for suffix in SIDECAR_SUFFIXES:
        if value.endswith(suffix):
            return value[: -len(suffix)]
    match = re.search(r"-mj[0-9a-f]{8,}$", value)
    return value[: match.start()] if match else None


def database_family_paths(entries):
    # A sidecar also identifies its base file even if that base has no extension.
    bases = {base for item in entries if (base := sqlite_family_base(item["path"]))}
    return {
        item["path"]
        for item in entries
        if item["path"].casefold().endswith(DATABASE_SUFFIXES)
        or sqlite_family_base(item["path"]) is not None
        or item["path"].casefold() in bases
    }


def main():
    project = Path(__file__).resolve().parents[1]
    sources = json.loads((project / ".local/archive.json").read_text())["sources"]
    deadline, remaining = time.monotonic() + 120, [250000]
    files, links, excluded_databases, errors = [], [], [], []
    for source in sources:
        root = Path(source["root"])
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise ValueError("Invalid archive root")
        for folder in source["include"]:
            relative = Path(folder)
            if relative.is_absolute() or ".." in relative.parts or len(relative.parts) != 1:
                raise ValueError("Only direct child folders can be selected")
            directory = root / relative
            if not directory.exists():
                errors.append({"source": source["name"], "folder": folder, "reason": "missing"})
                continue
            result = scan(directory, deadline, remaining)
            if not result["complete"]:
                errors.append(
                    {"source": source["name"], "folder": folder, "reason": "partial_scan"}
                )
            database_paths = database_family_paths(result["entries"])
            for item in result["entries"]:
                row = {"source": source["name"], "root": str(root), **item}
                row["path"] = str(relative / item["path"])
                if item["kind"] == "symlink":
                    links.append(row)
                elif item["path"] in database_paths:
                    excluded_databases.append(row)
                else:
                    files.append(row)
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output = project / "manifests" / ("archive-candidates-" + stamp + ".json")
    output.parent.mkdir(exist_ok=True, mode=0o700)
    report = {
        "status": "candidates_only",
        "transfer_authorized": False,
        "deletion_authorized": False,
        "dependency_analysis": "not_performed",
        "active_documents_classified": False,
        "application_restart_verified": False,
        "files": files,
        "links_not_followed": links,
        "database_files_excluded": excluded_databases,
        "errors": errors,
        "hashes_computed": False,
    }
    output.write_text(json.dumps(report, indent=2))
    print(
        f"Candidates: {len(files)} files; {sum(x['bytes'] for x in files) / 1e9:.3f} GB; links retained: {len(links)}"
    )
    print(f"Database files excluded: {len(excluded_databases)}; scan problems: {len(errors)}")
    print("Manifest: " + str(output))
    print(
        "Metadata only. Code/document references are not checked. Active dependencies must be retained. Not a transfer or deletion approval."
    )
    return 2 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
