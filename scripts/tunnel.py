"""Open an operator-controlled Railway SSH tunnel using private local settings."""

import argparse
import json
import os
import shutil
import socket
import uuid
from pathlib import Path


def command(config, executable):
    project = str(uuid.UUID(config["project"]))
    environment, service = config["environment"], config["service"]
    for value in (environment, service):
        if not isinstance(value, str) or not value or value.startswith("-"):
            raise ValueError("Invalid environment or service")
    port = config["port"]
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("Invalid local port")
    return [
        executable,
        "connect",
        service,
        "--project",
        project,
        "--environment",
        environment,
        "--tunnel-only",
        "--port",
        str(port),
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check local setup only")
    args = parser.parse_args()
    path = Path(__file__).resolve().parents[1] / ".local/tunnel.json"
    try:
        executable = shutil.which("railway")
        if not executable:
            print("Railway CLI is missing. Install it and sign in before opening the tunnel.")
            return 1
        argv = command(json.loads(path.read_text()), executable)
    except OSError, ValueError, KeyError, TypeError:
        print("Missing or invalid .local/tunnel.json. See README; no connection opened.")
        return 1
    port = int(argv[-1])
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", port))
    except OSError:
        print(f"Port {port} is unavailable. Existing processes were not changed.")
        print("An existing listener is not proof of a working Railway tunnel.")
        return 1
    if args.check:
        print("Local configuration, CLI and port checked. No Railway connection tested.")
        return 0
    print(f"Opening Railway SSH tunnel on port {port}. Keep this terminal open.", flush=True)
    print(
        "Ctrl+C closes this tunnel. Database passwords are not needed by this wrapper.", flush=True
    )
    # Replace this process: the CLI owns its SSH lifecycle and receives Ctrl+C directly.
    os.execv(executable, argv)


if __name__ == "__main__":
    raise SystemExit(main())
