#!/bin/bash
# Uninstalls Clipboard+ on macOS: quits it, removes the login item
# (LaunchAgent) and the app. Double-click it, or run
#   "Uninstall Clipboard+.command" [--purge]
# --purge ALSO deletes clipboard history, settings, account key, telemetry id
# and downloaded models. Without it they are kept.
set -u
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

quit_dir="${TMPDIR:-/tmp}"
for runtime in "$quit_dir"/dictation-"$(id -u)" /tmp/dictation-"$(id -u)"; do
  if [ -d "$runtime" ]; then
    printf quit >"$runtime/menubar-quit" 2>/dev/null
    printf quit >"$runtime/clip-quit" 2>/dev/null
  fi
done
sleep 1

for label in com.apercallc.clipboardplusdesktop.menubar org.whisperdictation.menubar; do
  plist="$HOME/Library/LaunchAgents/$label.plist"
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null
  rm -f "$plist"
done
rm -rf "/Applications/Clipboard+.app" "$HOME/Applications/Clipboard+.app"

if [ "$PURGE" = 1 ]; then
  rm -rf "$HOME/Library/Application Support/WhisperDictation" \
    "$HOME/Library/Caches/WhisperDictation" \
    "$HOME/.local/share/whisper.cpp/models"
  echo "Removed Clipboard+ and purged all user data."
else
  echo "Removed Clipboard+. History, settings and models were kept (run with --purge to delete them)."
fi
