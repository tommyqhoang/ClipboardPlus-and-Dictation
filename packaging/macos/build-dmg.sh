#!/usr/bin/env bash
set -euo pipefail
# Run after: pyinstaller packaging/macos/clipboardplus.spec
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DIST="$ROOT/dist/clipboardplus"
APP="$ROOT/dist/Clipboard+.app"

VERSION="$(python3 -c "import sys; sys.path.insert(0, '$ROOT/lib'); import desktop; print(desktop.APP_VERSION)")"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
sed "s/__APP_VERSION__/$VERSION/g" "$ROOT/packaging/macos/Info.plist" > "$APP/Contents/Info.plist"
cp "$ROOT/assets/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
cp -R "$DIST"/* "$APP/Contents/MacOS/"

STAGING="$ROOT/dist/dmg-staging"
rm -rf "$STAGING"
mkdir -p "$STAGING"
cp -R "$APP" "$STAGING/"
ln -s /Applications "$STAGING/Applications"

hdiutil create -volname "Clipboard+" -srcfolder "$STAGING" -ov -format UDZO \
  "$ROOT/dist/Clipboard+.dmg"

echo "Built dist/Clipboard+.dmg"
echo "First launch needs one right-click -> Open (unsigned build)."
