#!/usr/bin/env bash
set -euo pipefail

# Usage: ./uninstall.sh [--purge | --keep-data] [--yes]
#   (default)    remove the app; keep clipboard history, settings, the Clipboard+
#                account key, the telemetry id, transcripts and downloaded models.
#   --purge      ALSO permanently delete all of that user data (irreversible).
#   --keep-data  never ask, never delete user data (for scripts).
#   --yes        with --purge, skip the confirmation question.
PURGE=0
ASSUME_YES=0
ASK=1
for argument in "$@"; do
  case "$argument" in
    --purge) PURGE=1 ;;
    --keep-data) ASK=0 ;;
    --yes | -y) ASSUME_YES=1 ;;
    -h | --help)
      sed -n '3,8p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown option: $argument (see --help)" >&2
      exit 2
      ;;
  esac
done
if [[ "$PURGE" == 0 && "$ASK" == 1 && -t 0 && -t 1 ]]; then
  read -r -p "Also delete your clipboard history, settings, account key and downloaded models? [y/N] " reply || reply=""
  if [[ "$reply" == [yY]* ]]; then
    PURGE=1
    ASSUME_YES=1
  fi
fi
if [[ "$PURGE" == 1 && "$ASSUME_YES" == 0 ]]; then
  read -r -p "--purge permanently deletes all Clipboard+ user data. Continue? [y/N] " reply || reply=""
  [[ "$reply" == [yY]* ]] || PURGE=0
fi

BIN_DEST="${HOME}/.local/bin/dictate-toggle"
KEYBINDING_PATH="/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/dictation/"

APP_LIB="${HOME}/.local/lib/whisper-dictation"
LEGACY_LIB="${HOME}/.local/lib" # Where versions before the app's own folder lived.
MODULES="telemetry dictation desktop onboarding rewriting rewriteui workflow app app_service browserauth permissions cues app_styles app_settings menubar_logic logsetup hotkeys shortcut_test shortcut_panel menubar tray traymenu clipboardplus clipstore clipwatch clipwatch_linux clipwatch_macos clipwatch_windows clipservice clipsync clipcontrol clipui overlay updates engine"

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
import os
import plistlib
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
prefix = desktop.install_prefix(library / "app.py")
python = prefix / "share/whisper-dictation/venv/bin/python"
hotkeys.set_login_item(False, [str(python), str(library / "tray.py")])
hotkeys.gnome_remove(hotkeys.history_command(library, str(python)), path=hotkeys.GNOME_HISTORY_PATH)
# The installed module may predate these names (an old install being removed).
current_id = getattr(desktop, "DESKTOP_ENTRY_ID", "clipboardplus")
former_ids = getattr(desktop, "FORMER_DESKTOP_ENTRY_IDS", ("whisper-dictation",))
for entry_id in (current_id, *former_ids):
    (prefix / f"share/applications/{entry_id}.desktop").unlink(missing_ok=True)
if desktop.platform_name() == "macos":
    # Mirrors setup-desktop.py's uninstall(): the .app bundle install() creates
    # isn't under `library`, so it needs its own cleanup here too.
    system_apps = Path("/Applications")
    apps_root = (
        system_apps
        if prefix == Path.home() / ".local" and os.access(system_apps, os.W_OK)
        else prefix.parent / "Applications"
    )
    bundle = apps_root / f"{hotkeys.APP_NAME}.app"
    agents = (
        hotkeys.agent_path(),
        *(
            Path.home() / f"Library/LaunchAgents/{label}.plist"
            for label in hotkeys.FORMER_AGENT_LABELS
        ),
    )
    if any(
        agent.is_file()
        and any(
            Path(argument) == bundle or bundle in Path(argument).parents
            for argument in plistlib.loads(agent.read_bytes()).get("ProgramArguments", [])
        )
        for agent in agents
    ):
        hotkeys.set_login_item(False, [])
    info = bundle / "Contents/Info.plist"
    if info.is_file() and plistlib.loads(info.read_bytes()).get("CFBundleIdentifier") in (
        hotkeys.BUNDLE_ID,
        *hotkeys.FORMER_BUNDLE_IDS,
    ):
        (bundle / "Contents/MacOS/WhisperDictation").unlink(missing_ok=True)
        (bundle / "Contents/Resources/AppIcon.icns").unlink(missing_ok=True)
        info.unlink()
        for folder in (
            bundle / "Contents/MacOS",
            bundle / "Contents/Resources",
            bundle / "Contents",
            bundle,
        ):
            if folder.exists() and not any(folder.iterdir()):
                folder.rmdir()
PY
fi
# GNOME custom shortcuts (dictation and clipboard history), also for packaged
# (AppImage) installs, which have no installed library to do this for us.
HISTORY_PATH="/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/clipboard-history/"
if command -v gsettings >/dev/null 2>&1; then
  for path in "$KEYBINDING_PATH" "$HISTORY_PATH"; do
    if current="$(gsettings get org.gnome.settings-daemon.plugins.media-keys custom-keybindings 2>/dev/null)"; then
      updated="$(printf '%s' "$current" | sed "s#'${path}', ##g; s#, '${path}'##g; s#'${path}'##g")"
      gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings "$updated"
      gsettings reset-recursively "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:${path}" 2>/dev/null || true
    fi
  done
fi
if [[ "$PURGE" == 1 ]]; then
  # The account key may live in the OS keychain, which deleting files does not
  # reach. Best effort, before the runtime module and venv are removed.
  case "$(uname -s)" in
    Darwin)
      key_config="${XDG_CONFIG_HOME:+$XDG_CONFIG_HOME/dictation}"
      key_config="${key_config:-$HOME/Library/Application Support/WhisperDictation}"
      ;;
    *) key_config="${XDG_CONFIG_HOME:-$HOME/.config}/dictation" ;;
  esac
  key_python="${HOME}/.local/share/whisper-dictation/venv/bin/python"
  [[ -x "$key_python" ]] || key_python="$(command -v python3 || true)"
  if [[ -n "$key_python" && -f "${APP_LIB}/clipboardplus.py" ]]; then
    "$key_python" -c 'import sys; sys.path.insert(0, sys.argv[1]); import clipboardplus, pathlib; clipboardplus.remove_key(pathlib.Path(sys.argv[2]))' "$APP_LIB" "$key_config" 2>/dev/null || true
  fi
fi
rm -f "$BIN_DEST" \
  "${HOME}/.config/autostart/whisper-dictation.desktop" "${HOME}/.config/autostart/clipboardplus.desktop" \
  "${HOME}/.local/share/applications/whisper-dictation.desktop" "${HOME}/.local/share/applications/clipboardplus.desktop" \
  "${HOME}/.local/bin/Whisper Dictation.command" "${HOME}/.local/.dictation-install.json"
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
if [[ "$PURGE" == 1 ]]; then
  sleep 1 # Let the tray and clipboard service see their quit files and exit.
  case "$(uname -s)" in
    Darwin)
      config_dir="${XDG_CONFIG_HOME:+$XDG_CONFIG_HOME/dictation}"
      config_dir="${config_dir:-$HOME/Library/Application Support/WhisperDictation}"
      cache_dir="${XDG_CACHE_HOME:+$XDG_CACHE_HOME/dictation}"
      cache_dir="${cache_dir:-$HOME/Library/Caches/WhisperDictation}"
      ;;
    *)
      config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/dictation"
      cache_dir="${XDG_CACHE_HOME:-$HOME/.cache}/dictation"
      ;;
  esac
  # config_dir holds clipboard/ (history database), clipboard-plus-key (account
  # key), telemetry-id, settings and models/; cache_dir holds recoverable audio.
  rm -rf -- "$config_dir" "$cache_dir" "${HOME}/.local/share/whisper.cpp/models"
  echo "Removed the command and runtime module, and PURGED all user data (history, settings, keys, telemetry id, models)."
else
  echo "Removed the command and runtime module. Models, settings, saved transcripts and clipboard history were retained (re-run with --purge to delete them)."
fi
