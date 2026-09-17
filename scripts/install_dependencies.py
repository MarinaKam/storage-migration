"""Install declared dependencies into the active project environment."""

import argparse
import subprocess
import sys
import tomllib
from pathlib import Path

root = Path(__file__).resolve().parents[1]
if Path(sys.prefix).resolve() != (root / ".venv").resolve():
    raise SystemExit("Use make deps to install into the project .venv only.")
config = tomllib.loads((root / "pyproject.toml").read_text())
parser = argparse.ArgumentParser()
parser.add_argument("--dev", action="store_true")
args = parser.parse_args()
dependencies = list(config["project"]["dependencies"])
if args.dev:
    dependencies += config["project"]["optional-dependencies"]["dev"]
subprocess.run([sys.executable, "-m", "pip", "install", *dependencies], check=True)
