# Copyright (c) 2026 Jiawen <Jiawen.Cc@outlook.com>
# Licensed under the Elastic License 2.0 — see LICENSE in the project root.
# You may not provide this as a hosted/managed service to third parties.
# Commercial use (including SaaS): Jiawen.Cc@outlook.com
"""Entry point: `python -m compass.run`.

Cross-platform startup with Windows-specific compatibility:
  - Force UTF-8 stdout/stderr on Windows (default cp1252 mangles 中文)
  - Set asyncio policy to WindowsSelectorEventLoopPolicy where needed
  - Sanity-check port availability before binding (clearer error than EADDR)
"""
from __future__ import annotations

import logging

import socket
import sys

from .app import create_app
from .config import Settings

log = logging.getLogger(__name__)


def _windows_compat() -> None:
    """Apply Windows-only fixups. No-op on macOS/Linux."""
    if sys.platform != "win32":
        return
    # 1. Force UTF-8 on stdio so 中文 / emoji don't crash on cp1252 default.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    # 2. asyncio policy: SelectorEventLoop is more compatible with libraries
    # that assume POSIX semantics (e.g., subprocess on Win Proactor breaks some things).
    try:
        import asyncio

        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except (AttributeError, ImportError):
        pass


def _port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """Return True if the port is already bound by another process."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        try:
            sock.bind((host, port))
            return False
        except OSError:
            return True


def main() -> None:
    _windows_compat()

    settings = Settings.from_env()
    if _port_in_use(settings.port):
        sys.stderr.write(
            f"\n[ERROR] Port {settings.port} is already in use.\n"
            f"  • Another Compass instance may be running\n"
            f"  • Or another app grabbed the port\n"
            f"  • Set COMPASS_PORT in .env to use a different port\n\n"
        )
        sys.exit(2)

    app = create_app(settings)
    print(f"\n  Compass · http://127.0.0.1:{settings.port}\n")
    app.run(host="127.0.0.1", port=settings.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
