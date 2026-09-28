#!/usr/bin/env bash
set -euo pipefail
# Run after: pyinstaller packaging/macos/clipboardplus.spec
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DIST="$ROOT/dist/clipboardplus"
APP="$ROOT/dist/Clipboard+.app"

VERSION="$(python3 -c "import sys; sys.path.insert(0, '$ROOT/lib'); import desktop; print(desktop.APP_VERSION)")"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
sed "s/__APP_VERSION__/$VERSION/g" "$ROOT/packaging/macos/Info.plist" >"$APP/Contents/Info.plist"
cp "$ROOT/assets/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
cp -R "$DIST"/* "$APP/Contents/MacOS/"

# --- Ad-hoc code signing ----------------------------------------------------
# Two reasons this step exists:
# 1. Apple Silicon refuses to run unsigned Mach-Os, and on macOS 15
#    (Sequoia) Gatekeeper shows a downloaded (quarantined), unsigned app as
#    "damaged and can't be opened" — the old right-click -> Open bypass is
#    gone, so users were hard-blocked with no way to launch at all.
# 2. The workflow's ffmpeg bundling step runs dylibbundler, which rewrites
#    load paths AFTER Homebrew signed those binaries, invalidating their
#    signatures. A Mach-O with an invalid signature won't even exec on ARM.
# Re-sign every Mach-O with a fresh ad-hoc identity ("-"), innermost files
# first, then the .app itself. This does NOT replace Developer ID signing +
# notarization — the app still isn't notarized, so first launch needs the
# System Settings -> Privacy & Security -> "Open Anyway" step (or
# `xattr -dr com.apple.quarantine`). That step is the real fix, tracked as a
# follow-up; this keeps the unsigned build installable and loadable meanwhile.
echo "Signing bundle (ad-hoc identity)…"
while IFS= read -r -d '' f; do
  if file -b "$f" | grep -q 'Mach-O'; then
    codesign --force --sign - "$f"
  fi
done < <(find "$APP/Contents/MacOS" -type f -print0)
codesign --force --sign - "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"

STAGING="$ROOT/dist/dmg-staging"
rm -rf "$STAGING"
mkdir -p "$STAGING"
cp -R "$APP" "$STAGING/"
ln -s /Applications "$STAGING/Applications"

# Double-click installer for the unsigned build. Until the app is notarized,
# Gatekeeper shows a downloaded .app as "damaged" with no built-in bypass on
# macOS 15+, and expecting users to type xattr into Terminal is too much
# friction. A .command isn't an app bundle or Mach-O, so Gatekeeper doesn't
# assess it — Terminal opens it with a plain "downloaded from the internet?"
# confirmation at most. The user flow becomes: open DMG, double-click
# "Install Clipboard+", done. Dragging onto the Applications symlink still
# works for anyone who prefers it, paired with the manual xattr step.
cat >"$STAGING/Install Clipboard+.command" <<'EOF'
#!/bin/bash
# Copies Clipboard+ from this DMG into /Applications and strips the
# quarantine flag macOS adds to downloads — the flag that makes Gatekeeper
# report the unsigned app as "damaged and can't be opened".
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
APP="Clipboard+.app"
TARGET="/Applications/$APP"
rm -rf "$TARGET"
cp -R "$HERE/$APP" "$TARGET"
xattr -dr com.apple.quarantine "$TARGET" 2>/dev/null || true
echo "Installed. Launching Clipboard+…"
open "$TARGET"
EOF
chmod +x "$STAGING/Install Clipboard+.command"

hdiutil create -volname "Clipboard+" -srcfolder "$STAGING" -ov -format UDZO \
  "$ROOT/dist/Clipboard+.dmg"

echo "Built dist/Clipboard+.dmg"
echo "Unsigned build — users double-click 'Install Clipboard+' in the DMG"
echo "(copies to /Applications, strips quarantine, launches). Manual fallback:"
echo "  xattr -dr com.apple.quarantine /Applications/Clipboard+.app"
