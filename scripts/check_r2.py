"""Operator-invoked bucket access check. No object listing or writes."""
import json
import logging
import re
from pathlib import Path


def main():
    try:
        import boto3
        from botocore.config import Config
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        print("Missing dependency. Run: make deps")
        return 1
    # Never emit SDK diagnostics containing request or authentication details.
    logging.disable(logging.CRITICAL)
    try:
        path = Path(__file__).resolve().parents[1] / ".local" / "credentials.json"
        config = json.loads(path.read_text())
        endpoint = config["endpoint"]
        if not isinstance(endpoint, str) or not re.fullmatch(
            r"https://[a-f0-9]{32}(?:\.(?:eu|us|fedramp))?\.r2\.cloudflarestorage\.com", endpoint
        ):
            raise ValueError("Invalid endpoint")
        if not isinstance(config.get("bucket"), str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", config["bucket"]
        ):
            raise ValueError("Unexpected bucket")
        if any(not isinstance(config.get(k), str) or not config[k].strip()
               for k in ("access_key_id", "secret_access_key")):
            raise ValueError("Missing credentials")
    except (OSError, ValueError, KeyError, TypeError):
        print("Local configuration missing or invalid. No request sent; no credentials displayed.")
        return 1
    try:
        client = boto3.client(
            "s3", endpoint_url=endpoint, region_name="auto",
            aws_access_key_id=config["access_key_id"],
            aws_secret_access_key=config["secret_access_key"],
            config=Config(signature_version="s3v4", connect_timeout=10, read_timeout=10,
                          retries={"total_max_attempts": 1}, s3={"addressing_style": "path"}),
        )
        client.head_bucket(Bucket=config["bucket"])
    except ClientError as error:
        status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        label = str(status) if type(status) is int else "unknown"
        print("R2 access check failed (HTTP " + label + "). Check endpoint, key and bucket scope.")
        return 1
    except (BotoCoreError, Exception):
        print("R2 connection failed. Check network and local configuration; details hidden to protect credentials.")
        return 1
    print("R2 connection OK. No files listed, uploaded, downloaded or deleted.")
    print("This checks bucket access only; upload permissions and data integrity are not proven.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
