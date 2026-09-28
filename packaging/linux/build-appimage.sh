#!/usr/bin/env bash
set -euo pipefail
# Run after: pyinstaller packaging/linux/clipboardplus.spec
# Requires linuxdeploy and appimagetool on PATH (fetched by CI in Phase 4;
# download them yourself from their GitHub Releases to run this locally).
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DIST="$ROOT/dist/clipboardplus"
APPDIR="$ROOT/dist/AppDir"

rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin"
cp -R "$DIST"/* "$APPDIR/usr/bin/"
mkdir -p "$APPDIR/usr/share/applications" "$APPDIR/usr/share/icons/hicolor/1024x1024/apps"
cp "$ROOT/packaging/linux/AppDir/clipboardplus.desktop" "$APPDIR/usr/share/applications/"
cp "$ROOT/assets/icon-1024.png" \
  "$APPDIR/usr/share/icons/hicolor/1024x1024/apps/clipboardplus.png"
ln -sf usr/share/applications/clipboardplus.desktop "$APPDIR/clipboardplus.desktop"
ln -sf usr/share/icons/hicolor/1024x1024/apps/clipboardplus.png "$APPDIR/clipboardplus.png"
# A dispatcher, not a plain symlink to tray: a persisted command (GNOME shortcut,
# autostart entry) invokes $APPIMAGE with an entry name as its first argument
# (desktop.persistent_relaunch()), since the mounted usr/bin path it would
# otherwise point at disappears once every running instance of this AppImage exits.
cp "$ROOT/packaging/linux/AppDir/AppRun" "$APPDIR/AppRun"
chmod +x "$APPDIR/AppRun"

appimagetool "$APPDIR" "$ROOT/dist/Clipboard+-x86_64.AppImage"

echo "Built dist/Clipboard+-x86_64.AppImage"
echo "First run needs: chmod +x 'Clipboard+-x86_64.AppImage'"
