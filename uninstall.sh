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
runtime = Paths().runtime
if runtime.is_dir():
    (runtime / "menubar-quit").write_text("quit")  # Closes the tray app.
    (runtime / "clip-quit").write_text("quit")  # And the clipboard service (history is kept).
PY
fi
if command -v gsettings >/dev/null 2>&1; then
  if current="$(gsettings get org.gnome.settings-daemon.plugins.media-keys custom-keybindings 2>/dev/null)"; then
    updated="$(printf '%s' "$current" | sed "s#'${KEYBINDING_PATH}', ##g; s#, '${KEYBINDING_PATH}'##g; s#'${KEYBINDING_PATH}'##g")"
    gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings "$updated"
    gsettings reset-recursively "$KEYBINDING_SCHEMA"
  fi
fi
rm -f "$BIN_DEST" "${HOME}/.local/lib/dictation.py" "${HOME}/.local/lib/desktop.py" "${HOME}/.local/lib/onboarding.py" "${HOME}/.local/lib/rewriting.py" "${HOME}/.local/lib/workflow.py" "${HOME}/.local/lib/app.py" "${HOME}/.local/lib/app_service.py" "${HOME}/.local/lib/hotkeys.py" "${HOME}/.local/lib/menubar.py" "${HOME}/.local/lib/tray.py" "${HOME}/.local/lib/clipboardplus.py" "${HOME}/.local/lib/clipstore.py" "${HOME}/.local/lib/clipwatch.py" "${HOME}/.local/lib/clipwatch_linux.py" "${HOME}/.local/lib/clipwatch_macos.py" "${HOME}/.local/lib/clipwatch_windows.py" "${HOME}/.local/lib/clipservice.py" "${HOME}/.local/lib/clipsync.py" "${HOME}/.local/lib/clipcontrol.py" "${HOME}/.local/lib/clipui.py" "${HOME}/.local/lib/tray-recording.png" "${HOME}/.local/lib/menubar-icon.png" "${HOME}/.local/lib/menubar-recording.png" "${HOME}/.config/autostart/whisper-dictation.desktop" "${HOME}/.local/lib/whisper-dictation.png" "${HOME}/.local/lib/whisper-dictation.ico" "${HOME}/.local/share/applications/whisper-dictation.desktop" "${HOME}/.local/bin/Whisper Dictation.command"
rm -f "${HOME}/.local/.dictation-install.json"
for module in dictation desktop onboarding rewriting workflow app app_service hotkeys menubar tray clipboardplus clipstore clipwatch clipwatch_linux clipwatch_macos clipwatch_windows clipservice clipsync clipcontrol clipui; do
  rm -f "${HOME}/.local/lib/__pycache__/${module}".*.pyc
done
rmdir "${HOME}/.local/lib/__pycache__" 2>/dev/null || true
VENV="${HOME}/.local/share/whisper-dictation/venv"
# Only remove the private environment the installer created.
if [[ -f "${VENV}/pyvenv.cfg" ]]; then
  rm -rf -- "$VENV"
fi
echo "Removed the command and runtime module. Models, settings, and saved transcripts were retained."
