#!/usr/bin/env bash
# Removes the desktop integration Clipboard+ created for an AppImage install:
# the GNOME shortcuts, the autostart entry and the menu entry. Run it as
#   ./Clipboard+-x86_64.AppImage --uninstall [--purge] [--delete-appimage]
# or directly. Nothing else on the system is touched.
#   --purge            ALSO delete clipboard history, settings, account key,
#                      telemetry id, recordings and downloaded models.
#   --delete-appimage  also delete the AppImage file itself ($APPIMAGE).
set -euo pipefail
PURGE=0
DELETE_IMAGE=0
for argument in "$@"; do
  case "$argument" in
    --purge) PURGE=1 ;;
    --delete-appimage) DELETE_IMAGE=1 ;;
    *)
      echo "Unknown option: $argument" >&2
      exit 2
      ;;
  esac
done

# Ask a running instance to quit (the tray and clipboard service watch these files).
runtime="${XDG_RUNTIME_DIR:-/tmp}/dictation-$(id -u)"
if [[ -d "$runtime" ]]; then
  printf quit >"$runtime/menubar-quit" 2>/dev/null || true
  printf quit >"$runtime/clip-quit" 2>/dev/null || true
  sleep 1
fi

base="/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings"
if command -v gsettings >/dev/null 2>&1; then
  for name in dictation clipboard-history; do
    path="$base/$name/"
    if current="$(gsettings get org.gnome.settings-daemon.plugins.media-keys custom-keybindings 2>/dev/null)"; then
      updated="$(printf '%s' "$current" | sed "s#'${path}', ##g; s#, '${path}'##g; s#'${path}'##g")"
      gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings "$updated"
      gsettings reset-recursively "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:${path}" 2>/dev/null || true
    fi
  done
fi
rm -f "$HOME/.config/autostart/clipboardplus.desktop" \
  "$HOME/.config/autostart/whisper-dictation.desktop" \
  "$HOME/.local/share/applications/clipboardplus.desktop" \
  "$HOME/.local/share/applications/whisper-dictation.desktop"

if [[ "$PURGE" == 1 ]]; then
  rm -rf -- "${XDG_CONFIG_HOME:-$HOME/.config}/dictation" \
    "${XDG_CACHE_HOME:-$HOME/.cache}/dictation" \
    "$HOME/.local/share/whisper.cpp/models"
  echo "Purged all Clipboard+ user data."
else
  echo "Kept your history, settings and models (add --purge to delete them)."
fi
if [[ "$DELETE_IMAGE" == 1 && -n "${APPIMAGE:-}" ]]; then
  rm -f -- "$APPIMAGE"
  echo "Deleted $APPIMAGE"
else
  echo "Delete the AppImage file to finish removing the app."
fi
echo "Removed Clipboard+ desktop integration."
