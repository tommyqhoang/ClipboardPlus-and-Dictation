#!/usr/bin/env bash
set -euo pipefail
BIN_DEST="${HOME}/.local/bin/dictate-toggle"
KEYBINDING_PATH="/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/dictation/"
KEYBINDING_SCHEMA="org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:${KEYBINDING_PATH}"

APP_LIB="${HOME}/.local/lib/whisper-dictation"
LEGACY_LIB="${HOME}/.local/lib" # Where versions before the app's own folder lived.
MODULES="telemetry dictation desktop onboarding rewriting workflow app app_service hotkeys menubar tray clipboardplus clipstore clipwatch clipwatch_linux clipwatch_macos clipwatch_windows clipservice clipsync clipcontrol clipui overlay updates engine"

# Old versions used the shared ~/.local/lib folder. Its generic names may now
# belong to another application, so remove them only when the old layout is ours.
legacy_owned() {
  [[ -f "${LEGACY_LIB}/dictation.py" && -f "${LEGACY_LIB}/desktop.py" ]] &&
    grep -Eq 'whisper-dictation|WhisperDictation' "${LEGACY_LIB}/dictation.py"
}

# Never signal a PID read from disk; only the session supervisor owns the recorder.
library="$APP_LIB"
if [[ -f "${library}/dictation.py" ]]; then
  python3 - "$library" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from dictation import Paths, busy
from pathlib import Path
import desktop
import hotkeys
if busy(Paths()):
    sys.exit("Dictation is active. Stop or cancel it and wait for completion before uninstalling.")
runtime = Paths().runtime
if runtime.is_dir():
    (runtime / "menubar-quit").write_text("quit")  # Closes the tray app.
    (runtime / "clip-quit").write_text("quit")  # And the clipboard service (history is kept).
library = Path(sys.argv[1])
python = desktop.install_prefix(library / "app.py") / "share/whisper-dictation/venv/bin/python"
hotkeys.set_login_item(False, [str(python), str(library / "tray.py")])
hotkeys.gnome_remove(hotkeys.history_command(library, str(python)), path=hotkeys.GNOME_HISTORY_PATH)
PY
fi
if command -v gsettings >/dev/null 2>&1; then
  if current="$(gsettings get org.gnome.settings-daemon.plugins.media-keys custom-keybindings 2>/dev/null)"; then
    updated="$(printf '%s' "$current" | sed "s#'${KEYBINDING_PATH}', ##g; s#, '${KEYBINDING_PATH}'##g; s#'${KEYBINDING_PATH}'##g")"
    gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings "$updated"
    gsettings reset-recursively "$KEYBINDING_SCHEMA"
  fi
fi
rm -f "$BIN_DEST" "${HOME}/.config/autostart/whisper-dictation.desktop" "${HOME}/.local/share/applications/whisper-dictation.desktop" "${HOME}/.local/bin/Whisper Dictation.command" "${HOME}/.local/.dictation-install.json"
# Only this app's own files: other tools may share ~/.local/lib.
for library in "$APP_LIB" "$LEGACY_LIB"; do
  if [[ "$library" == "$LEGACY_LIB" ]] && ! legacy_owned; then
    continue
  fi
  for module in $MODULES; do
    if [[ "$library" == "$LEGACY_LIB" ]] &&
      { [[ ! -f "${library}/${module}.py" ]] || ! grep -Eq 'whisper-dictation|WhisperDictation' "${library}/${module}.py"; }; then
      continue
    fi
    rm -f "${library}/${module}.py" "${library}/__pycache__/${module}".*.pyc
  done
  for asset in tray-recording.png menubar-icon.png menubar-recording.png whisper-dictation.png whisper-dictation.ico; do
    if [[ "$library" == "$LEGACY_LIB" && "$asset" != whisper-dictation.* ]]; then
      continue
    fi
    rm -f "${library}/${asset}"
  done
  rmdir "${library}/__pycache__" 2>/dev/null || true
done
rmdir "$APP_LIB" 2>/dev/null || true
VENV="${HOME}/.local/share/whisper-dictation/venv"
# Only remove the private environment the installer created.
if [[ -f "${VENV}/pyvenv.cfg" ]]; then
  rm -rf -- "$VENV"
fi
echo "Removed the command and runtime module. Models, settings, saved transcripts and clipboard history were retained."
