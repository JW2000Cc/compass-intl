#!/usr/bin/env python3
# Copyright (c) 2026 Jiawen <Jiawen.Cc@outlook.com>
# Licensed under the Elastic License 2.0 — see LICENSE in the project root.

"""
Standalone Gmail sync — runs without the Flask app.

This is what powers the launchd / systemd / Task Scheduler background sync.
The Flask app's /gmail/sync route is a thin wrapper around the same run_sync()
function, so behaviour is identical.

Exit codes:
  0 — sync succeeded (even if 0 new emails)
  1 — pre-flight failed (no LLM key, no OAuth token, etc.)
  2 — sync error mid-flight (transient — retry next interval)

Usage:
  cd <compass-intl-root>
  .venv/bin/python tools/gmail_sync_cli.py [--days 7] [--verbose]
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Make the compass package importable when running this script directly.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Gmail sync without the Flask app")
    parser.add_argument("--days", type=int, default=14,
                        help="How many days back to scan (default: 14)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print INFO-level logs to stderr")
    args = parser.parse_args()

    log_level = logging.INFO if args.verbose else logging.WARNING
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    log = logging.getLogger("gmail_sync_cli")

    # Late imports so logging is configured first
    from compass.config import Settings
    from compass.extensions import init_engine, session_scope
    from compass.services.gmail_sync import run_sync

    settings = Settings.from_env()

    if not settings.has_llm:
        log.error("No LLM_API_KEY configured. Set it in .env or via /jobs/llm-settings/")
        return 1

    if not settings.data_dir.exists():
        log.error("data_dir does not exist: %s", settings.data_dir)
        return 1

    init_engine(settings)

    try:
        with session_scope() as session:
            stats = run_sync(
                session,
                data_dir=settings.data_dir,
                provider=settings.llm_provider,
                api_key=settings.llm_api_key,
                model=settings.llm_model,
                days=args.days,
            )
    except RuntimeError as exc:
        log.error("Gmail sync precondition failed: %s", exc)
        return 1
    except Exception:
        log.exception("Gmail sync errored mid-flight")
        return 2

    # Report stats — these go to stdout so launchd / systemd can log them
    print(f"Gmail sync OK: {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
