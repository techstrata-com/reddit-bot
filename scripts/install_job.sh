#!/bin/bash
# Install the Reddit bot daily job as a macOS launchd service (runs 24/7).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLIST_NAME="com.redditbot.job.plist"
PLIST_SRC="$ROOT/launchd/$PLIST_NAME"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_NAME"

PYTHON=""
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
elif [[ -x "$ROOT/.venv312/bin/python" ]]; then
  PYTHON="$ROOT/.venv312/bin/python"
else
  PYTHON="$(command -v python3)"
fi

mkdir -p "$HOME/Library/LaunchAgents"
sed \
  -e "s|__PROJECT_ROOT__|$ROOT|g" \
  -e "s|__PYTHON__|$PYTHON|g" \
  "$PLIST_SRC" > "$PLIST_DST"

launchctl bootout "gui/$(id -u)/$PLIST_NAME" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
launchctl enable "gui/$(id -u)/$PLIST_NAME"
launchctl kickstart -k "gui/$(id -u)/$PLIST_NAME"

echo "Installed: $PLIST_DST"
echo "Logs: $(dirname "$ROOT")/reddit-bot-logs/"
echo "Status: launchctl print gui/$(id -u)/$PLIST_NAME"
