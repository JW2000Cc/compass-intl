# Copyright (c) 2026 Jiawen <Jiawen.Cc@outlook.com>
# Licensed under the Elastic License 2.0 — see LICENSE in the project root.

"""
Manage the OS-native background Gmail sync from inside the Flask app.

Status / enable / disable across macOS launchd, Linux systemd user units,
and Windows Task Scheduler.

The actual install/uninstall logic lives in shell scripts under
`tools/{launchd,systemd,windows-task-scheduler}/`. This module just selects
the right script for the current OS and runs it via subprocess.
"""
from __future__ import annotations

import logging
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class BackgroundSyncStatus:
    """Snapshot of background-sync state for the UI."""

    supported: bool          # is this OS one of macOS / Linux / Windows?
    platform: str            # "macOS" / "Linux" / "Windows" / "other"
    installed: bool          # is the schedule entry currently registered?
    install_command: str     # human-readable hint for the UI
    last_log_line: Optional[str] = None  # last entry from the sync log, for context
    error: Optional[str] = None          # if introspection failed


def _project_root() -> Path:
    """Return the Compass project root (3 levels up from this file)."""
    return Path(__file__).resolve().parent.parent.parent


def _detect_platform() -> str:
    system = platform.system()
    if system == "Darwin":
        return "macOS"
    if system == "Linux":
        return "Linux"
    if system == "Windows":
        return "Windows"
    return "other"


def _read_last_log_line(project_root: Path) -> Optional[str]:
    log_path = project_root / "data" / "logs" / "gmail-sync.out.log"
    if not log_path.exists():
        return None
    try:
        # Read last ~2KB and pick the last non-blank line
        with open(log_path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 2048), 0)
            tail = fh.read().decode("utf-8", errors="replace")
        lines = [ln for ln in tail.split("\n") if ln.strip()]
        return lines[-1] if lines else None
    except OSError:
        return None


def status() -> BackgroundSyncStatus:
    """Detect whether the background sync schedule is currently registered."""
    plat = _detect_platform()
    root = _project_root()
    last_log = _read_last_log_line(root)

    if plat == "macOS":
        plist = Path.home() / "Library/LaunchAgents/com.compass.gmail-sync.plist"
        return BackgroundSyncStatus(
            supported=True,
            platform=plat,
            installed=plist.exists(),
            install_command="bash tools/launchd/install.sh",
            last_log_line=last_log,
        )

    if plat == "Linux":
        # User-level systemd timer
        unit = Path.home() / ".config/systemd/user/compass-gmail-sync.timer"
        return BackgroundSyncStatus(
            supported=True,
            platform=plat,
            installed=unit.exists(),
            install_command="bash tools/systemd/install.sh",
            last_log_line=last_log,
        )

    if plat == "Windows":
        # Probe via schtasks; presence => installed
        try:
            r = subprocess.run(
                ["schtasks", "/Query", "/TN", "CompassGmailSync"],
                capture_output=True, text=True, timeout=5,
            )
            installed = r.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            installed = False
        return BackgroundSyncStatus(
            supported=True,
            platform=plat,
            installed=installed,
            install_command="tools\\windows-task-scheduler\\install.bat",
            last_log_line=last_log,
        )

    # Unknown platform — fall through
    return BackgroundSyncStatus(
        supported=False,
        platform=plat,
        installed=False,
        install_command="",
        last_log_line=last_log,
        error=f"Background sync is not supported on {plat}",
    )


def enable() -> BackgroundSyncStatus:
    """Run the install script for the current OS. Returns updated status."""
    s = status()
    if not s.supported:
        s.error = s.error or "Unsupported OS"
        return s
    if s.installed:
        # Idempotent — re-running install is fine
        log.info("Background sync already installed; re-running install for refresh")

    root = _project_root()

    if s.platform == "macOS":
        cmd = ["bash", str(root / "tools/launchd/install.sh")]
    elif s.platform == "Linux":
        cmd = ["bash", str(root / "tools/systemd/install.sh")]
    elif s.platform == "Windows":
        cmd = [
            "cmd.exe", "/c",
            str(root / "tools" / "windows-task-scheduler" / "install.bat"),
        ]
    else:
        s.error = f"No installer for {s.platform}"
        return s

    try:
        r = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            s.error = (r.stderr or r.stdout or "install script failed")[:500]
            return s
        log.info("Background sync install succeeded: %s", r.stdout[:200])
    except subprocess.TimeoutExpired:
        s.error = "install script timed out (>60s)"
        return s
    except FileNotFoundError as exc:
        s.error = f"Required tool missing: {exc}"
        return s

    return status()


def disable() -> BackgroundSyncStatus:
    """Run the uninstall script for the current OS. Returns updated status."""
    s = status()
    if not s.supported:
        s.error = s.error or "Unsupported OS"
        return s
    if not s.installed:
        return s  # already disabled — no-op

    root = _project_root()

    if s.platform == "macOS":
        cmd = ["bash", str(root / "tools/launchd/uninstall.sh")]
    elif s.platform == "Linux":
        cmd = ["bash", str(root / "tools/systemd/uninstall.sh")]
    elif s.platform == "Windows":
        cmd = [
            "cmd.exe", "/c",
            str(root / "tools" / "windows-task-scheduler" / "uninstall.bat"),
        ]
    else:
        s.error = f"No uninstaller for {s.platform}"
        return s

    try:
        subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        s.error = "uninstall script timed out"
        return s

    return status()
