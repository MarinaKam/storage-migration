"""One-off operator-run backfill: upload vision-lab's locally-retained images that
are missing from R2 (by content SHA-256) under vl-staging/vision-lab/<relative path>.

Not wired into the Makefile; not part of the manifest-driven dataset transfer
(r2_transfer.py). Standalone on purpose: r2_transfer.py's load_plan() assumes a
single CSV manifest with {image_id, s3_url} columns and a flat images/ directory,
which does not fit vision-lab's arbitrary local tree. This script reuses the same
safety properties (O_NOFOLLOW local read with a stability check, conditional PUT
with IfNoneMatch, remote read-back verification, atomic per-object receipts) by
re-implementing them inline rather than importing r2_transfer.py, because that
module currently fails to import (download.py:191 has invalid Python 3 syntax --
`except OSError, ValueError, ...:` -- a pre-existing bug in this checkout, not
touched here).

Work list: {project}/.local/vl-image-upload-plan.json, produced by the caller
from a content-hash audit (sha256, size, primary local absolute path, target R2
key). Never invented here -- this script only reads local bytes and re-hashes
them; it does not decide what "missing" means.

Usage:
  python3 vl_image_upload.py --plan   # offline: print counts, no network calls
  python3 vl_image_upload.py --all    # run the transfer
  python3 vl_image_upload.py --limit N
"""

import argparse
import fcntl
import hashlib
import io
import json
import os
import signal
import stat
import sys
import threading
import time
import warnings
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from botocore.exceptions import ClientError

IMAGE_LIMIT = 50 * 1024**2
STOP = threading.Event()


class ConflictError(Exception):
    pass


def sha(data):
    return hashlib.sha256(data).hexdigest()


def stable_bytes(path, limit):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= limit:
            raise ValueError("Invalid local file size or type")
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    if len(data) != before.st_size or (before.st_size, before.st_mtime_ns) != (
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ValueError("Local file changed during read")
    return data


def image_type(data):
    from PIL import Image

    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data)) as im:
            fmt = im.format
            im.verify()
        with Image.open(io.BytesIO(data)) as im:
            im.load()
    return Image.MIME.get(fmt, "application/octet-stream")


def client_for(credentials):
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=credentials["endpoint"],
        region_name="auto",
        aws_access_key_id=credentials["access_key_id"],
        aws_secret_access_key=credentials["secret_access_key"],
        config=Config(
            signature_version="s3v4",
            connect_timeout=10,
            read_timeout=30,
            retries={"mode": "standard", "total_max_attempts": 3},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            s3={"addressing_style": "path"},
            max_pool_connections=4,
        ),
    )


def remote_digest(client, bucket, key, limit):
    try:
        response = client.get_object(Bucket=bucket, Key=key)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "NoSuchKey":
            return None
        raise
    body = response["Body"]
    try:
        expected = response["ContentLength"]
        if not 0 < expected <= limit:
            raise ConflictError("Unexpected remote size")
        digest, count = hashlib.sha256(), 0
        while chunk := body.read(128 * 1024):
            count += len(chunk)
            if count > limit:
                raise ConflictError("Remote size exceeds limit")
            digest.update(chunk)
        if count != expected:
            raise ConflictError("Incomplete remote read")
        return (digest.hexdigest(), count)
    finally:
        body.close()


def ensure_object(client, bucket, key, data, content_type, limit):
    expected = (sha(data), len(data))
    existing = remote_digest(client, bucket, key, limit)
    if existing is not None:
        if existing != expected:
            raise ConflictError("Existing remote object differs; not overwritten")
        return "existing_verified"
    try:
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=data,
            ContentLength=len(data),
            ContentType=content_type,
            IfNoneMatch="*",
        )
    except ClientError as error:
        if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") not in (409, 412):
            raise
    if remote_digest(client, bucket, key, limit) != expected:
        raise ConflictError("Remote read-back mismatch")
    return "copied_verified"


def atomic_receipt(path, row):
    temp = path.with_suffix(".tmp")
    with temp.open("w") as stream:
        json.dump(row, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def transfer_one(client, bucket, item, state_dir):
    key = item["target_r2_key"]
    expected_sha = item["sha256"]
    receipt = state_dir / (sha(key.encode()) + ".json")
    phase = "local_read"
    try:
        if STOP.is_set():
            raise InterruptedError()
        if receipt.exists():
            previous = json.loads(receipt.read_text())
            if previous.get("key") == key and previous.get("sha256") == expected_sha:
                phase = "resume_readback"
                remote = remote_digest(client, bucket, key, IMAGE_LIMIT)
                if remote is not None and remote[0] == expected_sha:
                    return {"key": key, "sha256": expected_sha, "status": "existing_verified", "bytes": remote[1]}
        phase = "local_read"
        data = stable_bytes(Path(item["primary_local_path"]), IMAGE_LIMIT)
        real_sha = sha(data)
        if real_sha != expected_sha:
            raise ConflictError(f"Local content changed since audit: expected {expected_sha}, got {real_sha}")
        content_type = image_type(data)
        if STOP.is_set():
            raise InterruptedError()
        phase = "r2_copy_readback"
        status = ensure_object(client, bucket, key, data, content_type, IMAGE_LIMIT)
        atomic_receipt(receipt, {"key": key, "sha256": expected_sha, "bytes": len(data)})
        return {"key": key, "sha256": expected_sha, "status": status, "bytes": len(data)}
    except Exception as error:  # noqa: BLE001 - fail closed, no secrets/paths leaked into result
        return {
            "key": key,
            "sha256": expected_sha,
            "status": "stopped" if isinstance(error, InterruptedError) else "failed",
            "phase": phase,
            "error_type": type(error).__name__,
        }


def main():
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--plan", action="store_true")
    action.add_argument("--all", action="store_true")
    action.add_argument("--limit", type=int)
    args = parser.parse_args()

    project = Path(__file__).resolve().parents[1]
    credentials = json.loads((project / ".local/credentials.json").read_text())
    bucket = credentials["bucket"]
    items = json.loads((project / ".local/vl-image-upload-plan.json").read_text())

    print(f"Destination: {bucket}/vision-lab/")
    print(f"Objects: {len(items)}; total bytes: {sum(i['size_bytes'] for i in items)}")
    if args.plan:
        print("Offline plan only. No network calls.")
        return 0

    if args.limit is not None and not 1 <= args.limit <= 200000:
        parser.error("Invalid limit")

    lock = (project / ".local/vl-image-upload.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("A local vl image upload is already running. Stop it first.")
        return 1

    def stop(signum, frame):
        STOP.set()
        print("\nStopping: no new uploads will start; waiting for active requests.", flush=True)

    previous_handler = signal.signal(signal.SIGINT, stop)
    state_dir = project / ".local/r2-state" / "vl-image-upload-20260918"
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    journal_path = project / "logs" / (
        "vl-image-upload-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + ".jsonl"
    )
    journal_path.parent.mkdir(exist_ok=True, mode=0o700)
    chosen = items if args.all else items[: args.limit]

    started = time.monotonic()
    counts = {"copied_verified": 0, "existing_verified": 0, "failed": 0, "stopped": 0}
    total_bytes = 0
    try:
        client = client_for(credentials)
        with journal_path.open("x") as journal, ThreadPoolExecutor(max_workers=4) as pool:
            todo = iter(chosen)
            pending = set()

            def schedule():
                item = next(todo, None) if not STOP.is_set() else None
                if item is not None:
                    pending.add(pool.submit(transfer_one, client, bucket, item, state_dir))

            for _ in range(4):
                schedule()
            while pending:
                done, pending = wait(pending, timeout=1, return_when=FIRST_COMPLETED)
                for future in done:
                    result = future.result()
                    counts[result["status"]] += 1
                    total_bytes += result.get("bytes", 0)
                    journal.write(json.dumps(result) + "\n")
                    journal.flush()
                    os.fsync(journal.fileno())
                    schedule()
                processed = sum(counts.values())
                elapsed = int(time.monotonic() - started)
                line = (
                    f"{processed}/{len(chosen)} ({100 * processed / max(len(chosen),1):.1f}%) | copied {counts['copied_verified']}"
                    f" | existing {counts['existing_verified']} | failed {counts['failed']}"
                    f" | verified {total_bytes / 1e6:.2f} MB | {elapsed // 60:02d}:{elapsed % 60:02d}"
                )
                print(line, flush=True)
        print(json.dumps(counts))
        print("Journal: " + str(journal_path))
        return 130 if STOP.is_set() else (2 if counts["failed"] else 0)
    except Exception as error:  # noqa: BLE001
        print("\nUpload stopped: " + type(error).__name__ + ". Existing objects and local photos were not overwritten.")
        return 1
    finally:
        signal.signal(signal.SIGINT, previous_handler)
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
