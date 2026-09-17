"""Install declared dependencies into the active project environment."""
import subprocess
import sys
import tomllib
from pathlib import Path

root = Path(__file__).resolve().parents[1]
if Path(sys.prefix).resolve() != (root / ".venv").resolve():
    raise SystemExit("Use make deps to install into the project .venv only.")
config = tomllib.loads((root / "pyproject.toml").read_text())
subprocess.run([sys.executable, "-m", "pip", "install", *config["project"]["dependencies"]], check=True)
