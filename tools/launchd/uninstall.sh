#!/usr/bin/env bash
# Copyright (c) 2026 Jiawen <Jiawen.Cc@outlook.com>
# Licensed under the Elastic License 2.0 — see LICENSE in the project root.
#
# Remove the Compass Gmail launchd job.

set -euo pipefail
PLIST="$HOME/Library/LaunchAgents/com.compass.gmail-sync.plist"

if [ ! -f "$PLIST" ]; then
    echo "Nothing to uninstall — $PLIST does not exist."
    exit 0
fi

launchctl unload "$PLIST" 2>/dev/null || true
rm "$PLIST"

echo "✅ Removed com.compass.gmail-sync"
echo "   Log files left in data/logs/ — delete manually if you want."
