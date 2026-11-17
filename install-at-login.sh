#!/bin/bash
# Install the launcher as a login item, so it is always running.
# Undo with:  launchctl bootout gui/$(id -u)/com.alanvallavaraj.emotiv-launcher
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL=com.alanvallavaraj.emotiv-launcher
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/.emotiv-launcher"
sed -e "s|REPLACE_WITH_PATH|$HERE|g" -e "s|REPLACE_WITH_HOME|$HOME|g" \
    "$HERE/$LABEL.plist" > "$DEST"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$DEST"
echo "Installed. It will start at every login and restart if it ever stops."
echo "  plist:  $DEST"
echo "  remove: launchctl bootout gui/$(id -u)/$LABEL && rm '$DEST'"
