#!/usr/bin/env bash
# Start scripts/alert_player.py automatically at login on macOS.
#
#   ./scripts/install_alert_player_mac.sh              install + start
#   ./scripts/install_alert_player_mac.sh --uninstall  stop + remove
#
# Uses ALERT_PLAYER_KEY / SENTINEL_API from the environment if set.
set -euo pipefail

LABEL="com.sentinelvision.alert-player"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if [ "${1:-}" = "--uninstall" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Alert player removed."
  exit 0
fi

PYTHON="$(command -v python3)"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/.sentinelvision"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$ROOT/scripts/alert_player.py</string>
    <string>--api</string><string>${SENTINEL_API:-http://localhost:8000/api/v1}</string>
    <string>--key</string><string>${ALERT_PLAYER_KEY:-alert-dev-key-change-me}</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/.sentinelvision/alert_player.log</string>
  <key>StandardErrorPath</key><string>$HOME/.sentinelvision/alert_player.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Alert player installed; it starts at login. Log: ~/.sentinelvision/alert_player.log"
