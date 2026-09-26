#!/usr/bin/env bash
# Download an application snapshot; users do not need a git checkout.
set -euo pipefail

main() (
  local ref="${DICTATION_REF:-main}" work archive platform python_prefix
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
  echo "Installs Whisper Dictation and dependencies for your user. Package managers may request administrator access."
  if [[ "$platform" == Darwin ]]; then
    if ! command -v brew >/dev/null 2>&1; then
      echo "Installing Homebrew using its official installer (interactive)."
      /bin/bash -c "$(curl --proto '=https' --tlsv1.2 -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
      if [[ -x /opt/homebrew/bin/brew ]]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
      elif [[ -x /usr/local/bin/brew ]]; then
        eval "$(/usr/local/bin/brew shellenv)"
      fi
    fi
    brew install python@3.14 python-tk@3.14 ffmpeg whisper.cpp
    python_prefix="$(brew --prefix python@3.14)"
    PATH="${python_prefix}/libexec/bin:${PATH}"
    export PATH
  elif ! command -v curl >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y curl ca-certificates
  fi
  work="$(mktemp -d -t whisper-dictation.XXXXXXXX)"
  # Only this newly allocated temporary directory is removed.
  trap 'rm -rf -- "$work"' EXIT
  archive="$work/app.tar.gz"
  curl --proto '=https' --tlsv1.2 --fail --location --retry 3 \
    --proto-redir '=https' \
    "https://github.com/tommyqhoang/wayland-whisper-dictation/archive/${ref}.tar.gz" -o "$archive"
  mkdir "$work/app"
  tar -xzf "$archive" --strip-components=1 -C "$work/app"
  if [[ "$platform" == Linux ]]; then
    bash "$work/app/install.sh" --no-model
  else
    python3 "$work/app/setup-desktop.py"
  fi
  echo "Opening Whisper Dictation. Finish setup in the app window."
  python3 "$work/app/setup-desktop.py" --launch-only
)

# Under `bash -c "$(curl ...)"` BASH_SOURCE is empty, so fall back to $0.
if [[ "${BASH_SOURCE[0]:-$0}" == "$0" ]]; then
  main "$@"
fi
