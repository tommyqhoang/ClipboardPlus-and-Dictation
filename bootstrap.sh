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

main() (
  local ref="${DICTATION_REF:-main}" work archive platform python_prefix python=python3
  if [[ ! "$ref" =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "DICTATION_REF must be a tag, branch name, or commit without slashes." >&2
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
      echo "Installing Homebrew using its official installer (interactive)."
      /bin/bash -c "$(curl --proto '=https' --tlsv1.2 -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
      if [[ -x /opt/homebrew/bin/brew ]]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
      elif [[ -x /usr/local/bin/brew ]]; then
        eval "$(/usr/local/bin/brew shellenv)"
      fi
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
  curl --proto '=https' --tlsv1.2 --fail --location --retry 3 \
    --proto-redir '=https' \
    "https://github.com/tommyqhoang/ClipboardPlus-and-Dictation/archive/${ref}.tar.gz" -o "$archive"
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
  echo "Done. Clipboard+ is open: finish setup in its window."
)

# Under `bash -c "$(curl ...)"` BASH_SOURCE is empty, so fall back to $0.
if [[ "${BASH_SOURCE[0]:-$0}" == "$0" ]]; then
  main "$@"
fi
