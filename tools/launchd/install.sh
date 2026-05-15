#!/usr/bin/env bash
# Copyright (c) 2026 Jiawen <Jiawen.Cc@outlook.com>
# Licensed under the Elastic License 2.0 — see LICENSE in the project root.
#
# Install Compass Gmail background sync on macOS via launchd.
# Run from the Compass project root: bash tools/launchd/install.sh
#
# What this does:
#   1. Detects your project root and venv Python
#   2. Renders the launchd plist template with your paths
#   3. Installs to ~/Library/LaunchAgents/com.compass.gmail-sync.plist
#   4. Loads it with launchctl (next fire: in 4 hours)
#
# Prereq: complete /gmail/setup in Compass once first (OAuth token).

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PLIST_TEMPLATE="$PROJECT_ROOT/tools/launchd/com.compass.gmail-sync.plist.template"
PLIST_DEST="$HOME/Library/LaunchAgents/com.compass.gmail-sync.plist"

# Detect venv Python (try common locations)
if [ -x "$PROJECT_ROOT/.venv/bin/python" ]; then
    PYTHON="$PROJECT_ROOT/.venv/bin/python"
elif [ -x "$PROJECT_ROOT/.venv-quick/bin/python" ]; then
    PYTHON="$PROJECT_ROOT/.venv-quick/bin/python"
elif command -v python3 >/dev/null; then
    PYTHON="$(command -v python3)"
    echo "⚠️  Using system python3 — no project venv detected at .venv/ or .venv-quick/"
    echo "    Make sure compass + its dependencies are installed for this python."
else
    echo "❌ No python found. Install Python 3.12+ first." >&2
    exit 1
fi

echo "  PROJECT_ROOT = $PROJECT_ROOT"
echo "  PYTHON       = $PYTHON"
echo "  PLIST_DEST   = $PLIST_DEST"

# Render template
mkdir -p "$(dirname "$PLIST_DEST")"
sed -e "s|__PYTHON__|$PYTHON|g" \
    -e "s|__PROJECT_ROOT__|$PROJECT_ROOT|g" \
    "$PLIST_TEMPLATE" > "$PLIST_DEST"

# Ensure log dir
mkdir -p "$PROJECT_ROOT/data/logs"

# Unload first (idempotent) then load
launchctl unload "$PLIST_DEST" 2>/dev/null || true
launchctl load -w "$PLIST_DEST"

echo ""
echo "✅ Installed and loaded com.compass.gmail-sync"
echo "   Next sync: in 4 hours (or whenever Mac wakes if asleep)"
echo ""
echo "Useful commands:"
echo "  • Trigger a sync now:   launchctl kickstart -k gui/\$(id -u)/com.compass.gmail-sync"
echo "  • See last sync logs:   tail -20 $PROJECT_ROOT/data/logs/gmail-sync.*.log"
echo "  • Check status:          launchctl print gui/\$(id -u)/com.compass.gmail-sync | grep state"
echo "  • Uninstall:             bash tools/launchd/uninstall.sh"
