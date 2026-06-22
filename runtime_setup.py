import importlib.metadata as importlib_metadata
import io
import os
from pathlib import Path
import re
import subprocess
import sys

# Native math libraries (OpenBLAS/MKL) each spawn a thread pool sized to the CPU. With many
# multiprocessing workers that means workers x cores threads contending — sustained max power
# and the load->idle spikes that can expose unstable hardware. These env vars cap each process
# to a single math thread and MUST be set before numpy/scipy are first imported, which is why
# this lives in runtime_setup (called at the very top of main, before those imports).
_THREAD_ENV_VARS = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
)


def _truthy(value: str | None) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on") if value is not None else False


def safe_mode_enabled() -> bool:
    """Whether the opt-in gentle execution mode is requested via CAP_SAFE_MODE."""
    return _truthy(os.environ.get("CAP_SAFE_MODE"))


def _apply_safe_mode_env() -> None:
    """Cap native math-library threads (and optionally the BLAS core type) for safe mode.

    Existing user-set values are respected so an explicit override always wins. Runs before
    numpy import here and propagates to spawned workers via inherited os.environ.
    """
    if not safe_mode_enabled():
        return
    for var in _THREAD_ENV_VARS:
        os.environ.setdefault(var, "1")
    # Optional: force a less power-hungry kernel (e.g. "Haswell" to avoid AVX-512 spikes).
    coretype = os.environ.get("CAP_OPENBLAS_CORETYPE")
    if coretype:
        os.environ.setdefault("OPENBLAS_CORETYPE", coretype)
    print(
        "[Safe mode] CAP_SAFE_MODE on: math-library threads capped to 1 per process "
        f"({', '.join(_THREAD_ENV_VARS)}=1).",
        flush=True,
    )


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


def lower_process_priority_if_safe() -> None:
    """Best-effort drop the current process to below-normal priority under safe mode.

    Called from pool worker initializers so the heavy routing workers yield to the rest of
    the system, smoothing the sustained-load profile. No-op when safe mode is off or psutil
    is unavailable.
    """
    if not safe_mode_enabled():
        return
    try:
        import psutil

        proc = psutil.Process()
        if os.name == "nt":
            proc.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        else:
            proc.nice(min(19, proc.nice() + 10))
    except Exception:
        pass


def run_runtime_setup() -> None:
    """Apply process-level runtime setup and install missing requirements."""
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)

    # Must happen before numpy/scipy import so the thread caps take effect.
    _apply_safe_mode_env()

    repo_root = Path(__file__).resolve().parent
    _ensure_requirements_installed(repo_root / "requirements.txt")
