#!/usr/bin/env bash
# Download an application snapshot; users do not need a git checkout.
set -euo pipefail

# Only reached when the script was saved and run without curl; the one-line
# command already has it.
install_curl() {
  local sudo=""
  if [[ "$(id -u)" != 0 ]]; then
    sudo=sudo
  fi
  if command -v apt-get >/dev/null 2>&1; then
    $sudo apt-get update && $sudo apt-get install -y curl ca-certificates
  elif command -v dnf >/dev/null 2>&1; then
    $sudo dnf install -y curl ca-certificates
  elif command -v pacman >/dev/null 2>&1; then
    $sudo pacman -S --needed --noconfirm curl ca-certificates
  elif command -v zypper >/dev/null 2>&1; then
    $sudo zypper --non-interactive install curl ca-certificates
  else
    echo "Install curl, then run this installer again." >&2
    return 1
  fi
}

REPO="tommyqhoang/ClipboardPlus-and-Dictation"
# Used only when the GitHub API cannot be reached; bump with each release.
PINNED_TAG="v1.6.6"

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | cut -d' ' -f1
  else
    echo "No sha256sum or shasum found to verify the download." >&2
    return 1
  fi
}

fetch() { # fetch URL DEST: to a file, never straight into an interpreter
  curl --proto '=https' --tlsv1.2 --fail --location --retry 3 \
    --proto-redir '=https' --connect-timeout 15 "$1" -o "$2"
}

# The newest release tag; the pinned tag when GitHub's API is unreachable.
latest_tag() {
  local json="$1/latest.json" tag=""
  if fetch "https://api.github.com/repos/${REPO}/releases/latest" "$json" 2>/dev/null; then
    tag="$(sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$json" | head -n1)"
  fi
  if [[ ! "$tag" =~ ^v?[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    tag="$PINNED_TAG"
  fi
  echo "$tag"
}

# Download the tag's source and refuse to unpack it unless it matches the
# SHA-256 the release published (SHA256SUMS, or a per-file .sha256 sidecar).
fetch_verified_source() { # fetch_verified_source TAG WORKDIR ARCHIVE
  local tag="$1" work="$2" archive="$3" name expected="" entry
  fetch "https://github.com/${REPO}/releases/download/${tag}/SHA256SUMS" "$work/SHA256SUMS" 2>/dev/null || true
  for name in "clipboardplus-source-${tag}.tar.gz" "${tag}.tar.gz"; do
    if [[ -s "$work/SHA256SUMS" ]]; then
      entry="$(awk -v n="$name" '{f=$2; sub(/^\*/, "", f)} f==n {print tolower($1); exit}' "$work/SHA256SUMS")"
      [[ -n "$entry" ]] && {
        expected="$entry"
        break
      }
    fi
  done
  if [[ -z "$expected" ]]; then
    echo "Release ${tag} has no published checksum for its source; refusing to install." >&2
    return 1
  fi
  if fetch "https://github.com/${REPO}/releases/download/${tag}/clipboardplus-source-${tag}.tar.gz" "$archive" 2>/dev/null &&
    [[ "$(sha256_of "$archive")" == "$expected" ]]; then
    return 0
  fi
  fetch "https://github.com/${REPO}/archive/refs/tags/${tag}.tar.gz" "$archive" || return 1
  if [[ ! "$expected" =~ ^[0-9a-f]{64}$ || "$(sha256_of "$archive")" != "$expected" ]]; then
    rm -f -- "$archive"
    echo "The downloaded source did not match its published checksum; nothing was installed." >&2
    return 1
  fi
}

main() (
  local ref="${DICTATION_REF:-}" work archive platform python_prefix python=python3
  if [[ -n "$ref" && ! "$ref" =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "DICTATION_REF must be a release tag without slashes." >&2
    return 1
  fi
  platform="$(uname -s)"
  case "$platform" in
    Darwin | Linux) ;;
    *)
      echo "Use bootstrap.ps1 on Windows." >&2
      return 1
      ;;
  esac
  echo "Installing Clipboard+ for your user. Your package manager may ask for your password."
  if [[ "$platform" == Darwin ]]; then
    # Homebrew may be installed without being on PATH (non-login shells).
    if ! command -v brew >/dev/null 2>&1; then
      if [[ -x /opt/homebrew/bin/brew ]]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
      elif [[ -x /usr/local/bin/brew ]]; then
        eval "$(/usr/local/bin/brew shellenv)"
      fi
    fi
    if ! command -v brew >/dev/null 2>&1; then
      echo "Homebrew is required but not installed. Install it from https://brew.sh" >&2
      echo "(review the installer there first), then run this command again." >&2
      return 1
    fi
    brew install python@3.14 python-tk@3.14
    brew install ffmpeg whisper.cpp || echo "Speech tools could not be installed. Clipboard+ will still install; retry speech setup later." >&2
    python_prefix="$(brew --prefix python@3.14)"
    # Use the Homebrew Python that has Tk, not whichever python3 is first on PATH.
    if [[ -x "${python_prefix}/bin/python3" ]]; then
      python="${python_prefix}/bin/python3"
    fi
  elif ! command -v curl >/dev/null 2>&1; then
    install_curl
  fi
  work="$(mktemp -d -t whisper-dictation.XXXXXXXX)"
  # Only this newly allocated temporary directory is removed.
  trap 'rm -rf -- "$work"' EXIT
  archive="$work/app.tar.gz"
  [[ -n "$ref" ]] || ref="$(latest_tag "$work")"
  echo "Installing release ${ref}."
  fetch_verified_source "$ref" "$work" "$archive" || return 1
  mkdir "$work/app"
  tar -xzf "$archive" --strip-components=1 -C "$work/app"
  # The installers leave the closing words to this script.
  export DICTATION_QUICK_INSTALL=1
  if [[ "$platform" == Linux ]]; then
    bash "$work/app/install.sh" --no-model
  else
    "$python" "$work/app/setup-desktop.py"
  fi
  echo
  echo "Done. Clipboard+ is installed. If it did not open, launch it from your application menu to finish setup."
)

# Under `bash -c "$(curl ...)"` BASH_SOURCE is empty, so fall back to $0.
if [[ "${BASH_SOURCE[0]:-$0}" == "$0" ]]; then
  main "$@"
fi
