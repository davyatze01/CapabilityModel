"""One-time setup helper: checks for Rscript, the R packages the pipeline needs (r5r,
data.table), and a Java 21 JVM (r5r's hard requirement) -- and offers to install what's
missing.

Never runs silently: every step that needs elevated privileges is printed before it runs,
and only proceeds after an explicit y/N confirmation. Nothing here runs automatically as
part of main.py or any pipeline stage -- run this yourself once per machine.

Run: python scripts/setup_r.py
"""

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routing.public_transport_routing_stage import _resolve_r5r_java_home

_REQUIRED_R_PACKAGES = ["r5r", "data.table"]
_IN_FLATPAK = os.path.isfile("/.flatpak-info")


def _which(name: str) -> str | None:
    """shutil.which, but resolved on the host when running inside the VS Code Flatpak
    sandbox -- the sandboxed PATH doesn't see host binaries (confirmed: Rscript, dnf, apt
    and pacman can all be present on the host while invisible to a plain shutil.which from
    inside the sandbox), so a bare shutil.which would report a false negative here and this
    script would then offer to reinstall software that's already there.
    """
    if _IN_FLATPAK and shutil.which("flatpak-spawn"):
        result = subprocess.run(
            ["flatpak-spawn", "--host", "which", name],
            capture_output=True, text=True,
        )
        return result.stdout.strip() or None
    return shutil.which(name)


def _host_cmd_prefix() -> list[str]:
    """Inside the VS Code Flatpak sandbox, install commands must run on the host via
    flatpak-spawn --host (same reasoning as public_transport_routing_stage.py's Rscript launch)."""
    if _IN_FLATPAK and shutil.which("flatpak-spawn"):
        return ["flatpak-spawn", "--host"]
    return []


def _confirm(prompt: str) -> bool:
    return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")


def _run(cmd: list[str]) -> bool:
    print(f"[Setup] Running: {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd).returncode == 0


def _detect_linux_install_cmd() -> list[str] | None:
    if _which("apt"):
        return ["sudo", "apt", "install", "-y", "r-base"]
    if _which("dnf"):
        return ["sudo", "dnf", "install", "-y", "R"]
    if _which("pacman"):
        return ["sudo", "pacman", "-S", "--noconfirm", "r"]
    return None


def _install_r() -> bool:
    system = platform.system()
    prefix = _host_cmd_prefix()

    if system == "Windows":
        if _which("winget"):
            cmd = ["winget", "install", "--id", "RProject.R", "-e"]
        elif _which("choco"):
            cmd = ["choco", "install", "r.project", "-y"]
        else:
            print(
                "[Setup] Neither winget nor choco found. Install R manually from "
                "https://cran.r-project.org/bin/windows/base/ and re-run this script.",
                flush=True,
            )
            return False
    elif system == "Darwin":
        if _which("brew"):
            cmd = ["brew", "install", "r"]
        else:
            print(
                "[Setup] Homebrew not found. Install R manually from "
                "https://cran.r-project.org/bin/macosx/ and re-run this script.",
                flush=True,
            )
            return False
    elif system == "Linux":
        cmd = _detect_linux_install_cmd()
        if cmd is None:
            print(
                "[Setup] No supported package manager found (apt/dnf/pacman). "
                "Install R manually: https://cran.r-project.org/bin/linux/",
                flush=True,
            )
            return False
    else:
        print(f"[Setup] Unrecognized platform {system!r}. Install R manually.", flush=True)
        return False

    full_cmd = prefix + cmd
    print(f"[Setup] Rscript not found. This will install R:\n    {' '.join(full_cmd)}", flush=True)
    if not _confirm("[Setup] Proceed?"):
        print("[Setup] Skipped. Install R yourself, then re-run this script.", flush=True)
        return False
    return _run(full_cmd)


def _install_r_packages(rscript_exe: str) -> bool:
    pkgs_r = ", ".join(repr(p) for p in _REQUIRED_R_PACKAGES)
    check = subprocess.run(
        [rscript_exe, "-e",
         f"pkgs <- c({pkgs_r}); "
         "missing <- pkgs[!pkgs %in% installed.packages()[,'Package']]; "
         "cat(paste(missing, collapse=','))"],
        capture_output=True, text=True,
    )
    missing = [p for p in check.stdout.strip().split(",") if p]
    if not missing:
        print(f"[Setup] R packages already installed: {_REQUIRED_R_PACKAGES}", flush=True)
        return True

    print(f"[Setup] Missing R packages: {missing}", flush=True)
    print(
        "[Setup] r5r's first real use also downloads an R5 jar (network, no admin needed) -- "
        "that happens on the pipeline's first bus-routing run, not here.",
        flush=True,
    )
    if not _confirm(f"[Setup] Install {missing} via install.packages()? (user-space, no admin needed)"):
        print("[Setup] Skipped.", flush=True)
        return False

    install_expr = (
        "install.packages(c(" + ", ".join(repr(p) for p in missing) + "), "
        "repos='https://cloud.r-project.org')"
    )
    return _run(_host_cmd_prefix() + [rscript_exe, "-e", install_expr])


def _check_java() -> None:
    java_home = _resolve_r5r_java_home()
    if java_home:
        print(f"[Setup] Java 21 found: {java_home}", flush=True)
        return
    print(
        "[Setup] WARNING: no Java 21 found (r5r requires exactly Java 21; newer JDKs are "
        "rejected). This script does not install a JDK -- get one from "
        "https://adoptium.net/temurin/releases/?version=21 and either put it where "
        "public_transport_routing_stage._resolve_r5r_java_home looks (e.g. "
        "/usr/lib/jvm/java-21-openjdk on Linux) or set CAP_JAVA_HOME to its install path.",
        flush=True,
    )


def main() -> None:
    rscript_exe = _which("Rscript")
    if rscript_exe is None:
        if not _install_r():
            sys.exit(1)
        rscript_exe = _which("Rscript")
        if rscript_exe is None:
            print(
                "[Setup] R was installed but Rscript still isn't on PATH. Restart your "
                "terminal/shell (PATH changes need a fresh session) and re-run this script.",
                flush=True,
            )
            sys.exit(1)
    else:
        print(f"[Setup] Rscript found: {rscript_exe}", flush=True)

    if not _install_r_packages(rscript_exe):
        sys.exit(1)

    _check_java()
    print("[Setup] Done.", flush=True)


if __name__ == "__main__":
    main()
