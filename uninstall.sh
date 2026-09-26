#!/usr/bin/env bash
set -euo pipefail
BIN_DEST="${HOME}/.local/bin/dictate-toggle"
KEYBINDING_PATH="/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/dictation/"
KEYBINDING_SCHEMA="org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:${KEYBINDING_PATH}"

# Never signal a PID read from disk; only the session supervisor owns the recorder.
if [[ -f "${HOME}/.local/lib/dictation.py" ]]; then
  python3 - "${HOME}/.local/lib" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from dictation import Paths, busy
if busy(Paths()):
    sys.exit("Dictation is active. Stop or cancel it and wait for completion before uninstalling.")
PY
fi
if command -v gsettings >/dev/null 2>&1; then
  if current="$(gsettings get org.gnome.settings-daemon.plugins.media-keys custom-keybindings 2>/dev/null)"; then
    updated="$(printf '%s' "$current" | sed "s#'${KEYBINDING_PATH}', ##g; s#, '${KEYBINDING_PATH}'##g; s#'${KEYBINDING_PATH}'##g")"
    gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings "$updated"
    gsettings reset-recursively "$KEYBINDING_SCHEMA"
  fi
fi
rm -f "$BIN_DEST" "${HOME}/.local/lib/dictation.py" "${HOME}/.local/lib/desktop.py" "${HOME}/.local/lib/onboarding.py" "${HOME}/.local/lib/rewriting.py" "${HOME}/.local/lib/workflow.py" "${HOME}/.local/lib/app.py" "${HOME}/.local/lib/app_service.py" "${HOME}/.local/share/applications/whisper-dictation.desktop" "${HOME}/.local/bin/Whisper Dictation.command"
echo "Removed the command and runtime module. Models, settings, and saved transcripts were retained."
