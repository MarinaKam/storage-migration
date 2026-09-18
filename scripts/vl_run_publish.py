"""Thin invocation wrapper around vision_lab.data_engine.run_publisher.

The logic lives in VL (2026-09-18 correction: it previously lived only
here, coupling VL's own artifact shape to a migration tool). This script
only reads local credentials/config and calls into VL's module -- it does
not reimplement plan_objects/publish_run/run_status.

Usage:
  python3 vl_run_publish.py --plan --run-dir <dir> --run-id <id>
  python3 vl_run_publish.py --publish --run-dir <dir> --run-id <id>
  python3 vl_run_publish.py --status --run-id <id>
"""

import argparse
import json
import sys
from pathlib import Path


def _load_vl_module():
    """Import vision_lab without requiring it installed in this venv --
    storage-migration is a separate project on purpose (AGENTS.md: "lives
    its own life"); it borrows VL's checkout path rather than depending on
    the package."""
    vl_src = Path.home() / "PycharmProjects" / "vision-lab" / "src"
    if str(vl_src) not in sys.path:
        sys.path.insert(0, str(vl_src))
    from vision_lab.data_engine import run_publisher

    return run_publisher


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--prefix", default="vision-lab-shared-medium")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--plan", action="store_true")
    action.add_argument("--publish", action="store_true")
    action.add_argument("--status", action="store_true")
    args = parser.parse_args()

    project = Path(__file__).resolve().parents[1]
    creds = json.loads((project / ".local/credentials.json").read_text())
    run_publisher = _load_vl_module()

    if args.status:
        result = run_publisher.run_status(
            run_id=args.run_id,
            bucket=creds["bucket"],
            prefix=args.prefix,
            endpoint=creds["endpoint"],
            access_key_id=creds["access_key_id"],
            secret_access_key=creds["secret_access_key"],
        )
        print(json.dumps(result, indent=2))
        return 0

    if not args.run_dir:
        parser.error("--run-dir is required for --plan/--publish")
    objects = run_publisher.plan_objects(args.run_dir, args.run_id, args.prefix)
    print(f"run {args.run_id}: {len(objects)} objects to verify/publish")
    for obj in objects:
        print(f"  {obj['local_path']} -> {obj['key']}")
    if args.plan:
        print("Offline plan only. No network calls.")
        return 0

    try:
        result = run_publisher.publish_run(
            run_dir=args.run_dir,
            run_id=args.run_id,
            bucket=creds["bucket"],
            prefix=args.prefix,
            endpoint=creds["endpoint"],
            access_key_id=creds["access_key_id"],
            secret_access_key=creds["secret_access_key"],
        )
    except run_publisher.RunPublishError as exc:
        print(f"publish failed: {exc}")
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
