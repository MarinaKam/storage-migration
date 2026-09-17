"""Operator-run manifest migration. Bounded memory, conditional writes, remote read-back."""

import argparse
import csv
import datetime
import fcntl
import hashlib
import io
import json
import logging
import os
import re
import signal
import stat
import sys
import threading
import time
import urllib.error
import urllib.request
import warnings
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from urllib.parse import urlsplit

from botocore.exceptions import ClientError
from download import NoRedirect, validate_url

IMAGE_LIMIT = 25 * 1024**2
META_LIMIT = 100 * 1024**2
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


def load_plan(project):
    dataset = json.loads((project / ".local/dataset.json").read_text())
    transfer = json.loads((project / ".local/r2-transfer.json").read_text())
    credentials = json.loads((project / ".local/credentials.json").read_text())
    endpoint, bucket = credentials["endpoint"], credentials["bucket"]
    if not re.fullmatch(
        r"https://[a-f0-9]{32}(?:\.(?:eu|us|fedramp))?\.r2\.cloudflarestorage\.com", endpoint
    ):
        raise ValueError("Invalid endpoint")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket):
        raise ValueError("Invalid bucket")
    prefix = transfer["prefix"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", prefix):
        raise ValueError("Invalid prefix")
    manifest, images = Path(dataset["manifest"]), Path(dataset["images"])
    if (
        not manifest.is_absolute()
        or not images.is_absolute()
        or images.is_symlink()
        or not images.is_dir()
    ):
        raise ValueError("Invalid local dataset paths")
    raw = stable_bytes(manifest, META_LIMIT)
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    if not {"image_id", "s3_url"}.issubset(reader.fieldnames or []):
        raise ValueError("Invalid manifest columns")
    rows, seen = [], set()
    for index, row in enumerate(reader):
        ident = (row.get("image_id") or "").strip()
        if index >= 100000 or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", ident) or ident in seen:
            raise ValueError("Invalid or duplicate manifest ID")
        seen.add(ident)
        url = validate_url((row.get("s3_url") or "").strip(), dataset["allowed_source_hosts"])
        ext = Path(urlsplit(url).path).suffix.lower()
        if ext not in {
            "",
            ".jpg",
            ".jpeg",
            ".png",
            ".webp",
            ".gif",
            ".tif",
            ".tiff",
            ".bmp",
            ".heic",
        }:
            raise ValueError("Unsupported source extension")
        rows.append(
            {
                "id": ident,
                "url": url,
                "key": f"{prefix}/images/{ident}{ext}",
                "source_ref": sha(url.encode()),
                "local": images / (ident + ".jpg"),
            }
        )
    if not rows:
        raise ValueError("Empty manifest")
    metadata = []
    names = set()
    paths = transfer["metadata_files"]
    if not isinstance(paths, list) or not 1 <= len(paths) <= 20:
        raise ValueError("Invalid metadata file list")
    for name in paths:
        path = Path(name)
        if (
            not path.is_absolute()
            or not re.fullmatch(r"[A-Za-z0-9_-]+\.(csv|json|jsonl)", path.name)
            or path.name in names
        ):
            raise ValueError("Invalid metadata path")
        names.add(path.name)
        data = stable_bytes(path, META_LIMIT)
        metadata.append(
            {
                "path": path,
                "key": f"{prefix}/metadata/{path.name}",
                "sha256": sha(data),
                "bytes": len(data),
            }
        )
    if not any(x["path"] == manifest and x["sha256"] == sha(raw) for x in metadata):
        raise ValueError("Metadata must include the exact manifest")
    identity = {
        "endpoint": endpoint,
        "bucket": bucket,
        "prefix": prefix,
        "manifest": sha(raw),
        "metadata": [(x["key"], x["sha256"]) for x in metadata],
    }
    return {
        "credentials": credentials,
        "bucket": bucket,
        "prefix": prefix,
        "rows": rows,
        "metadata": metadata,
        "id": sha(json.dumps(identity, sort_keys=True).encode()),
    }


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


def source_bytes(url):
    for attempt in range(3):
        if STOP.is_set():
            raise InterruptedError()
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            with opener.open(url, timeout=15) as response:
                if response.status != 200:
                    raise ValueError("Unexpected source status")
                length = response.headers.get("Content-Length")
                expected = int(length) if length is not None else None
                if expected is not None and not 0 < expected <= IMAGE_LIMIT:
                    raise ValueError("Invalid source length")
                data = bytearray()
                deadline = time.monotonic() + 120
                while chunk := response.read(128 * 1024):
                    if STOP.is_set():
                        raise InterruptedError()
                    if time.monotonic() > deadline or len(data) + len(chunk) > IMAGE_LIMIT:
                        raise ValueError("Source time/size limit")
                    data.extend(chunk)
                if not data or (expected is not None and len(data) != expected):
                    raise ValueError("Incomplete source response")
                return bytes(data)
        except urllib.error.URLError, TimeoutError, ConnectionError:
            if attempt == 2 or STOP.wait(2 * (attempt + 1)):
                raise


def atomic_receipt(path, row):
    temp = path.with_suffix(".tmp")
    with temp.open("w") as stream:
        json.dump(row, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def transfer_image(client, plan, item, state_dir):
    key = item["key"]
    receipt = state_dir / (sha(key.encode()) + ".json")
    phase = "source"
    try:
        if STOP.is_set():
            raise InterruptedError()
        local_data = None
        # Local images are operator-controlled inputs, not proof of source-S3 equality.
        if item["local"].exists() or item["local"].is_symlink():
            local_data = stable_bytes(item["local"], IMAGE_LIMIT)
        if receipt.exists():
            previous = json.loads(receipt.read_text())
            expected_fields = {"plan_id": plan["id"], "key": key, "source_ref": item["source_ref"]}
            if all(previous.get(k) == v for k, v in expected_fields.items()):
                expected = (previous["sha256"], previous["bytes"])
                if local_data is not None and (sha(local_data), len(local_data)) != expected:
                    raise ConflictError("Local source changed since prior verified transfer")
                phase = "resume_readback"
                remote = remote_digest(client, plan["bucket"], key, IMAGE_LIMIT)
                if remote == expected:
                    return {
                        "id": item["id"],
                        "key": key,
                        "status": "existing_verified",
                        "bytes": expected[1],
                    }
                if remote is not None:
                    raise ConflictError("Remote object changed since prior verified transfer")
        phase = "source"
        data = local_data if local_data is not None else source_bytes(item["url"])
        content_type = image_type(data)
        if STOP.is_set():
            raise InterruptedError()
        phase = "r2_copy_readback"
        status = ensure_object(client, plan["bucket"], key, data, content_type, IMAGE_LIMIT)
        record = {
            "plan_id": plan["id"],
            "key": key,
            "source_ref": item["source_ref"],
            "sha256": sha(data),
            "bytes": len(data),
            "authority": "local" if local_data is not None else "source_http",
        }
        atomic_receipt(receipt, record)
        return {
            "id": item["id"],
            "key": key,
            "status": status,
            "bytes": len(data),
            "authority": record["authority"],
        }
    except Exception as error:  # noqa: BLE001 - fail closed and redact credentials/URLs
        reason = error.reason if isinstance(error, urllib.error.URLError) else error
        result = {
            "id": item["id"],
            "key": key,
            "status": "stopped" if isinstance(error, InterruptedError) else "failed",
            "phase": phase,
            "error_type": type(error).__name__,
            "reason_type": type(reason).__name__,
        }
        if isinstance(error, ClientError):
            result["http_status"] = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        return result


def main():
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--plan", action="store_true")
    action.add_argument("--all", action="store_true")
    action.add_argument("--limit", type=int)
    args = parser.parse_args()
    if args.limit is not None and not 1 <= args.limit <= 100000:
        parser.error("Invalid limit")
    project = Path(__file__).resolve().parents[1]
    logging.disable(logging.CRITICAL)
    try:
        plan = load_plan(project)
    except Exception as error:  # noqa: BLE001 - fail closed and redact credentials/URLs
        safe_reasons = {
            "Invalid endpoint",
            "Invalid bucket",
            "Invalid prefix",
            "Invalid local dataset paths",
            "Invalid manifest columns",
            "Invalid or duplicate manifest ID",
            "Unsupported source extension",
            "Empty manifest",
            "Invalid metadata file list",
            "Invalid metadata path",
            "Metadata must include the exact manifest",
            "Source URL outside configured HTTPS boundary",
            "Invalid local file size or type",
            "Local file changed during read",
        }
        detail = (
            str(error)
            if type(error) is ValueError and str(error) in safe_reasons
            else type(error).__name__
        )
        print("Local plan rejected: " + detail + ". No network calls.")
        return 1
    local = sum(item["local"].is_file() for item in plan["rows"])
    print(f"Destination: {plan['bucket']}/{plan['prefix']}/")
    print(
        f"Images: {len(plan['rows'])}; local candidates: {local}; source fetch candidates: {len(plan['rows']) - local}"
    )
    print(
        f"Metadata files: {len(plan['metadata'])}; metadata bytes: {sum(x['bytes'] for x in plan['metadata'])}"
    )
    if args.plan:
        print(
            "Offline plan only. Local candidate contents and remote copies are not verified. No network calls."
        )
        return 0
    STOP.clear()
    lock = (project / ".local/download.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("A local download or transfer is already running. Stop it first.")
        return 1

    def stop(signum, frame):
        STOP.set()
        print("\nStopping: no new images will start; waiting for active requests.", flush=True)

    previous_handler = signal.signal(signal.SIGINT, stop)
    state_dir = project / ".local/r2-state" / plan["id"]
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S%fZ")
    journal_path = project / "logs" / ("r2-" + stamp + ".jsonl")
    journal_path.parent.mkdir(exist_ok=True, mode=0o700)
    chosen = plan["rows"] if args.all else plan["rows"][: args.limit]
    started = time.monotonic()
    counts = {"copied_verified": 0, "existing_verified": 0, "failed": 0, "stopped": 0}
    total_bytes = 0
    try:
        client = client_for(plan["credentials"])
        # Freeze and verify metadata before processing photographs.
        for metadata in plan["metadata"]:
            if STOP.is_set():
                return 130
            data = stable_bytes(metadata["path"], META_LIMIT)
            if sha(data) != metadata["sha256"]:
                raise ConflictError("Metadata changed after planning")
            ensure_object(
                client,
                plan["bucket"],
                metadata["key"],
                data,
                "application/octet-stream",
                META_LIMIT,
            )
        print(
            "Metadata read-back verified. Transferring images; Ctrl+C stops scheduling.", flush=True
        )
        with journal_path.open("x") as journal, ThreadPoolExecutor(max_workers=4) as pool:
            todo = iter(chosen)
            pending = set()

            def schedule():
                item = next(todo, None) if not STOP.is_set() else None
                if item is not None:
                    pending.add(pool.submit(transfer_image, client, plan, item, state_dir))

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
                    f"{processed}/{len(chosen)} ({100 * processed / len(chosen):.1f}%) | copied {counts['copied_verified']}"
                    f" | existing {counts['existing_verified']} | failed {counts['failed']}"
                    f" | verified {total_bytes / 1e6:.2f} MB | {elapsed // 60:02d}:{elapsed % 60:02d}"
                )
                print(
                    ("\r" if sys.stdout.isatty() else "") + line + "   ",
                    end="" if sys.stdout.isatty() else "\n",
                    flush=True,
                )
        print("\n" + json.dumps(counts))
        print("Journal: " + str(journal_path))
        print("No local photos deleted. No automatic report pages created.")
        return 130 if STOP.is_set() else (2 if counts["failed"] else 0)
    except Exception as error:  # noqa: BLE001 - fail closed and redact credentials/URLs
        print(
            "\nTransfer stopped: "
            + type(error).__name__
            + ". Existing objects and local photos were not overwritten."
        )
        return 1
    finally:
        signal.signal(signal.SIGINT, previous_handler)
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
