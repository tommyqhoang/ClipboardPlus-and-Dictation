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
cp "$ROOT/LICENSE" "$ROOT/THIRD-PARTY-NOTICES.md" "$APP/Contents/Resources/"
cp -R "$DIST"/* "$APP/Contents/MacOS/"
# codesign treats every directory inside Contents/MacOS as nested code and rejects
# PyInstaller's _internal (an embedded Python.framework, lib-dynload, ...) with
# "bundle format unrecognized". Only executables belong in MacOS: keep the data in
# Resources (sealed as resources, each Mach-O still signed below) and leave a
# symlink so the executables find _internal next to themselves.
mv "$APP/Contents/MacOS/_internal" "$APP/Contents/Resources/_internal"
# Likewise any plain file PyInstaller left beside the executables (LICENSE and the
# notices are already in Resources): codesign expects code, and only code, in MacOS.
for entry in "$APP/Contents/MacOS"/*; do
  [[ -L "$entry" ]] && continue
  if [[ -d "$entry" ]] || ! file -b "$entry" | grep -q 'Mach-O'; then
    rm -rf -- "$entry"
  fi
done
ln -s ../Resources/_internal "$APP/Contents/MacOS/_internal"
# The executables read their data through that symlink; fail the build rather than
# ship a menu bar app with no icon images.
for required in menubar-icon.png menubar-recording.png libtk8.6.dylib; do
  [[ -f "$APP/Contents/MacOS/_internal/$required" ]] || { echo "missing $required in the bundle" >&2; exit 1; }
done

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
# With APPLE_SIGNING_IDENTITY set (a "Developer ID Application: ..." identity
# already in the keychain; release.yml imports it from secrets), sign for real:
# hardened runtime, secure timestamp and the entitlements file, so the DMG can
# be notarized. Otherwise fall back to the ad-hoc identity described above.
ENTITLEMENTS="$ROOT/packaging/macos/entitlements.plist"
if [[ -n "${APPLE_SIGNING_IDENTITY:-}" ]]; then
  echo "Signing bundle (Developer ID: ${APPLE_SIGNING_IDENTITY})…"
  sign() { codesign --force --options runtime --timestamp --entitlements "$ENTITLEMENTS" --sign "$APPLE_SIGNING_IDENTITY" "$@"; }
else
  echo "Signing bundle (ad-hoc identity)…"
  sign() { codesign --force --sign - "$@"; }
fi
while IFS= read -r -d '' f; do
  # Info.plist names menubar as CFBundleExecutable. Signing that binary can
  # cause codesign to sign/validate the enclosing app bundle immediately, so
  # its sibling executables must already have signatures. `find` traversal
  # order differs between macOS filesystems and architectures; sign the main
  # executable explicitly after every other Mach-O instead of trusting it.
  [[ "$f" == "$APP/Contents/MacOS/menubar" ]] && continue
  if file -b "$f" | grep -q 'Mach-O'; then
    sign "$f"
  fi
done < <(find "$APP/Contents" -type f -print0)
sign "$APP/Contents/MacOS/menubar"
sign "$APP"
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
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
APP="Clipboard+.app"
DESTINATION="${1:-/Applications}"
TARGET="$DESTINATION/$APP"
if [[ -e "$TARGET" ]]; then
  identifier="$(plutil -extract CFBundleIdentifier raw -o - "$TARGET/Contents/Info.plist")"
  case "$identifier" in
    com.apercallc.clipboardplus | com.apercallc.clipboardplusdesktop) ;;
    *) echo "Another app uses $TARGET. Choose another Applications folder." >&2; exit 1 ;;
  esac
fi
# Replacing an app while it is running leaves its old process and shortcuts
# active. Let it finish recording and quit normally before changing its files.
for identifier in com.apercallc.clipboardplus com.apercallc.clipboardplusdesktop; do
  if [[ "$(osascript -e "application id \"$identifier\" is running" 2>/dev/null || true)" = "true" ]]; then
    echo "Quit Clipboard+ from its menu bar icon, then run this installer again." >&2
    exit 1
  fi
done
mkdir -p "$DESTINATION"
STAGE="$(mktemp -d "$DESTINATION/.clipboardplus-install.XXXXXX")"
complete=0
cleanup() {
  if [[ "$complete" = 0 && -d "$STAGE/previous.app" ]]; then
    rm -rf "$TARGET"
    mv "$STAGE/previous.app" "$TARGET"
  fi
  rm -rf "$STAGE"
}
trap cleanup EXIT
# Copy and validate before moving the previous app. The staging folder is on
# the same volume, so both renames are atomic and failures restore the old app.
ditto "$HERE/$APP" "$STAGE/$APP"
codesign --verify --deep --strict "$STAGE/$APP"
xattr -dr com.apple.quarantine "$STAGE/$APP" 2>/dev/null || true
if [[ -e "$TARGET" ]]; then mv "$TARGET" "$STAGE/previous.app"; fi
mv "$STAGE/$APP" "$TARGET"
echo "Installed. Launching Clipboard+…"
open "$TARGET"
complete=1
EOF
chmod +x "$STAGING/Install Clipboard+.command"
cp "$ROOT/packaging/macos/uninstall.command" "$STAGING/Uninstall Clipboard+.command"
chmod +x "$STAGING/Uninstall Clipboard+.command"
cp "$ROOT/LICENSE" "$ROOT/THIRD-PARTY-NOTICES.md" "$STAGING/"

hdiutil create -volname "Clipboard+" -srcfolder "$STAGING" -ov -format UDZO \
  "$ROOT/dist/Clipboard+.dmg"

echo "Built dist/Clipboard+.dmg"
echo "Unsigned build — users double-click 'Install Clipboard+' in the DMG"
echo "(copies to /Applications, strips quarantine, launches). Manual fallback:"
echo "  xattr -dr com.apple.quarantine /Applications/Clipboard+.app"
