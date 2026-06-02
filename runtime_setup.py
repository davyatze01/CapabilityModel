import importlib.metadata as importlib_metadata
import io
from pathlib import Path
import re
import subprocess
import sys


def _canonicalize_dist_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower().strip()


def _parse_requirement_name(requirement_line: str) -> str | None:
    line = requirement_line.strip()
    if not line or line.startswith("#") or line.startswith("-"):
        return None
    match = re.match(r"^\s*([A-Za-z0-9_.-]+)", line)
    if not match:
        return None
    return _canonicalize_dist_name(match.group(1))


def _ensure_requirements_installed(requirements_path: Path) -> None:
    if not requirements_path.exists():
        return

    installed = {
        _canonicalize_dist_name((dist.metadata.get("Name") or ""))
        for dist in importlib_metadata.distributions()
    }
    installed.discard("")

    required: set[str] = set()
    for line in requirements_path.read_text(encoding="utf-8").splitlines():
        name = _parse_requirement_name(line)
        if name:
            required.add(name)

    missing = sorted(name for name in required if name not in installed)
    if not missing:
        return

    print(
        "[Bootstrap] Missing Python packages detected: "
        + ", ".join(missing)
        + ". Installing from requirements.txt...",
        flush=True,
    )
    install_cmd = [sys.executable, "-m", "pip", "install", "-r", str(requirements_path)]
    try:
        subprocess.run(install_cmd, check=True)
    except subprocess.CalledProcessError:
        # Common on macOS/system Python without write access to site-packages.
        if sys.prefix == sys.base_prefix:
            print(
                "[Bootstrap] Retrying dependency install with --user (non-venv Python).",
                flush=True,
            )
            subprocess.run(install_cmd + ["--user"], check=True)
        else:
            raise


def run_runtime_setup() -> None:
    """Apply process-level runtime setup and install missing requirements."""
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)

    repo_root = Path(__file__).resolve().parent
    _ensure_requirements_installed(repo_root / "requirements.txt")
