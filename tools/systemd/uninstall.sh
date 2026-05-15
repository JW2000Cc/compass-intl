#!/usr/bin/env bash
# Copyright (c) 2026 Jiawen <Jiawen.Cc@outlook.com>
# Licensed under the Elastic License 2.0 — see LICENSE in the project root.

set -euo pipefail
DEST_DIR="$HOME/.config/systemd/user"

systemctl --user disable --now compass-gmail-sync.timer 2>/dev/null || true
rm -f "$DEST_DIR/compass-gmail-sync.timer" "$DEST_DIR/compass-gmail-sync.service"
systemctl --user daemon-reload

echo "✅ Removed compass-gmail-sync (timer + service)"
echo "   Log files left in data/logs/ — delete manually if you want."
