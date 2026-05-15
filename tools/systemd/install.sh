#!/usr/bin/env bash
# Copyright (c) 2026 Jiawen <Jiawen.Cc@outlook.com>
# Licensed under the Elastic License 2.0 — see LICENSE in the project root.
#
# Install Compass Gmail background sync on Linux via systemd (user-level units).
# Run from the Compass project root: bash tools/systemd/install.sh
#
# Uses ~/.config/systemd/user/  (no sudo required for user-level units).
# Requires: systemd ≥ 230 (any modern distro).

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TEMPLATE_DIR="$PROJECT_ROOT/tools/systemd"
DEST_DIR="$HOME/.config/systemd/user"

# Detect venv Python
if [ -x "$PROJECT_ROOT/.venv/bin/python" ]; then
    PYTHON="$PROJECT_ROOT/.venv/bin/python"
elif [ -x "$PROJECT_ROOT/.venv-quick/bin/python" ]; then
    PYTHON="$PROJECT_ROOT/.venv-quick/bin/python"
elif command -v python3 >/dev/null; then
    PYTHON="$(command -v python3)"
    echo "⚠️  Using system python3. Make sure deps installed for it."
else
    echo "❌ No python found." >&2
    exit 1
fi

echo "  PROJECT_ROOT = $PROJECT_ROOT"
echo "  PYTHON       = $PYTHON"

# Render units
mkdir -p "$DEST_DIR" "$PROJECT_ROOT/data/logs"

sed -e "s|__PYTHON__|$PYTHON|g" \
    -e "s|__PROJECT_ROOT__|$PROJECT_ROOT|g" \
    "$TEMPLATE_DIR/compass-gmail-sync.service.template" > "$DEST_DIR/compass-gmail-sync.service"

sed -e "s|__PYTHON__|$PYTHON|g" \
    -e "s|__PROJECT_ROOT__|$PROJECT_ROOT|g" \
    "$TEMPLATE_DIR/compass-gmail-sync.timer.template" > "$DEST_DIR/compass-gmail-sync.timer"

# Reload + enable + start the timer
systemctl --user daemon-reload
systemctl --user enable --now compass-gmail-sync.timer

# Keep the timer running across logouts (Linux idle laptop pattern)
loginctl enable-linger "$(whoami)" 2>/dev/null || true

echo ""
echo "✅ Installed compass-gmail-sync.timer (user-level)"
echo "   First fire: ~5 minutes after boot, then every 4 hours"
echo ""
echo "Useful commands:"
echo "  • Trigger now:    systemctl --user start compass-gmail-sync.service"
echo "  • Check timer:    systemctl --user list-timers | grep compass"
echo "  • See logs:       journalctl --user -u compass-gmail-sync.service -n 50"
echo "  • Tail recent:    tail -20 $PROJECT_ROOT/data/logs/gmail-sync.*.log"
echo "  • Uninstall:      bash tools/systemd/uninstall.sh"
