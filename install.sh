#!/usr/bin/env bash
set -euo pipefail

APP_NAME="Dictation Toggle"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_SRC="${PROJECT_DIR}/bin/dictate-toggle"
BIN_DEST="${HOME}/.local/bin/dictate-toggle"
MODEL_DIR="${HOME}/.local/share/whisper.cpp/models"
MODEL_NAME="${DICTATION_MODEL_NAME:-ggml-base.en.bin}"
MODEL_URL="${DICTATION_MODEL_URL:-https://huggingface.co/ggerganov/whisper.cpp/resolve/main/${MODEL_NAME}}"
MODEL_DEST="${MODEL_DIR}/${MODEL_NAME}"
MODEL_LINK="${MODEL_DIR}/dictation-model.bin"
WHISPER_VERSION="v1.8.7"
WHISPER_COMMIT="48f628a84833905ee4a0658ee6d4a5c915ce1997"
SKIP_MODEL=0
SKIP_PACKAGES=0
SKIP_DOWNLOAD=0
KEYBINDING_PATH="/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/dictation/"
KEYBINDING_SCHEMA="org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:${KEYBINDING_PATH}"
DEFAULT_BINDING="${DICTATION_BINDING:-<Super><Shift>d}"

need() {
  command -v "$1" >/dev/null 2>&1
}

install_packages() {
  if ! need apt-get; then
    echo "apt-get not found. This installer is intended for Debian or Debian-based systems." >&2
    return 1
  fi

  local base_packages=(
    alsa-utils
    build-essential
    ca-certificates
    cmake
    curl
    git
    gnome-session-canberra
    libnotify-bin
    perl
    python3
    python3-tk
    util-linux
    wl-clipboard
  )

  echo "Installing Debian packages: ${base_packages[*]}"
  sudo apt-get update
  sudo apt-get install -y "${base_packages[@]}"
  if [[ "$SKIP_MODEL" == 0 ]]; then
    install_whisper
  fi
}

install_whisper() {
  if [[ -n "${DICTATION_WHISPER_BIN:-}" ]]; then
    [[ -x "$DICTATION_WHISPER_BIN" ]] || {
      echo "DICTATION_WHISPER_BIN is not executable." >&2
      return 1
    }
    return 0
  fi
  if need whisper-cli; then
    echo "whisper-cli already installed: $(command -v whisper-cli)"
    return 0
  fi

  if apt-cache show whisper.cpp >/dev/null 2>&1; then
    echo "Installing Debian package: whisper.cpp"
    if sudo apt-get install -y whisper.cpp; then
      return 0
    fi
    echo "Debian package install failed; falling back to source build."
  else
    echo "Debian package whisper.cpp not available; falling back to source build."
  fi

  install_whisper_from_source
}

install_whisper_from_source() {
  local src_dir="${HOME}/.local/opt/whisper.cpp-${WHISPER_VERSION}"
  local bin_path="${src_dir}/build/bin/whisper-cli"

  mkdir -p "${HOME}/.local/opt"
  if [[ -d "$src_dir/.git" ]]; then
    if [[ "$(git -C "$src_dir" rev-parse HEAD)" != "$WHISPER_COMMIT" ]]; then
      echo "$src_dir is not the expected $WHISPER_VERSION source; remove it or set DICTATION_WHISPER_BIN." >&2
      return 1
    fi
    if [[ -x "$bin_path" ]]; then
      echo "Pinned source-built whisper-cli already present: $bin_path"
      return 0
    fi
  elif [[ -e "$src_dir" ]]; then
    echo "$src_dir exists but is not a git checkout; remove it or set DICTATION_WHISPER_BIN." >&2
    return 1
  else
    echo "Cloning whisper.cpp $WHISPER_VERSION into $src_dir"
    git clone --depth 1 --branch "$WHISPER_VERSION" \
      https://github.com/ggml-org/whisper.cpp.git "$src_dir"
    if [[ "$(git -C "$src_dir" rev-parse HEAD)" != "$WHISPER_COMMIT" ]]; then
      echo "Downloaded whisper.cpp source did not match the pinned commit." >&2
      return 1
    fi
  fi

  echo "Building whisper-cli from source"
  cmake -S "$src_dir" -B "$src_dir/build" -DCMAKE_BUILD_TYPE=Release -DWHISPER_SDL2=OFF
  cmake --build "$src_dir/build" --config Release --target whisper-cli -j"$(nproc)"

  if [[ ! -x "$bin_path" ]]; then
    echo "Build finished, but whisper-cli was not found at $bin_path." >&2
    return 1
  fi

  echo "Built whisper-cli: $bin_path"
}

install_model() {
  local partial_dest="${MODEL_DEST}.part"
  local candidate="$MODEL_DEST"
  local expected_sha256="${DICTATION_MODEL_SHA256:-}"

  if [[ -z "$expected_sha256" ]]; then
    case "$MODEL_NAME" in
      ggml-base.en.bin) expected_sha256="a03779c86df3323075f5e796cb2ce5029f00ec8869eee3fdfb897afe36c6d002" ;;
      ggml-base.bin) expected_sha256="60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe" ;;
    esac
  fi

  mkdir -p "$MODEL_DIR"
  if [[ -s "$MODEL_DEST" ]]; then
    echo "Model already present: $MODEL_DEST"
  else
    echo "Downloading Whisper model: $MODEL_NAME"
    curl --proto '=https' --proto-redir '=https' --fail --location --retry 3 \
      --retry-delay 2 --connect-timeout 15 \
      --continue-at - --output "$partial_dest" "$MODEL_URL"
    if [[ ! -s "$partial_dest" ]]; then
      echo "Model download completed without producing a usable file." >&2
      return 1
    fi
    candidate="$partial_dest"
  fi

  # Reject common HTML/error downloads. The optional SHA-256 verifies the entire file.
  python3 - "$candidate" "$expected_sha256" <<'PY'
import hashlib
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
with path.open("rb") as stream:
    if stream.read(4) != b"lmgg":
        sys.exit("Model does not have a whisper.cpp GGML header; check the download.")
    if sys.argv[2]:
        stream.seek(0)
        digest = hashlib.sha256()
        for block in iter(lambda: stream.read(1048576), b""):
            digest.update(block)
        if digest.hexdigest() != sys.argv[2].lower():
            sys.exit("Model SHA-256 mismatch. Select a trusted model download.")
PY
  if [[ -z "$expected_sha256" ]]; then
    echo "Warning: this custom model has only a GGML header check. Set DICTATION_MODEL_SHA256 for full verification." >&2
  fi
  if [[ "$candidate" == "$partial_dest" ]]; then
    mv -f "$partial_dest" "$MODEL_DEST"
    echo "Downloaded model: $MODEL_DEST"
  fi

  if [[ -e "$MODEL_LINK" && ! -L "$MODEL_LINK" ]]; then
    echo "Refusing to replace a regular file at $MODEL_LINK." >&2
    return 1
  fi
  ln -sfn "$MODEL_DEST" "$MODEL_LINK"
  echo "Selected model: $MODEL_LINK -> $MODEL_NAME"
}

install_script() {
  mkdir -p "${HOME}/.local/bin" "${HOME}/.local/lib"
  install -m 0644 "${PROJECT_DIR}/lib/dictation.py" "${HOME}/.local/lib/dictation.py"
  install -m 0644 "${PROJECT_DIR}/lib/desktop.py" "${HOME}/.local/lib/desktop.py"
  install -m 0644 "${PROJECT_DIR}/lib/onboarding.py" "${HOME}/.local/lib/onboarding.py"
  install -m 0644 "${PROJECT_DIR}/lib/rewriting.py" "${HOME}/.local/lib/rewriting.py"
  install -m 0644 "${PROJECT_DIR}/lib/workflow.py" "${HOME}/.local/lib/workflow.py"
  install -m 0755 "$BIN_SRC" "$BIN_DEST"
  python3 "${PROJECT_DIR}/setup-desktop.py"
  echo "Installed $BIN_DEST"
}

install_gnome_shortcut() {
  if ! need gsettings; then
    echo "gsettings not found; skipping GNOME shortcut setup."
    return 0
  fi

  local current updated
  if ! current="$(gsettings get org.gnome.settings-daemon.plugins.media-keys custom-keybindings 2>/dev/null)"; then
    echo "GNOME media-key settings are unavailable; skipping shortcut setup."
    return 0
  fi
  if [[ "$current" == "@as []" || "$current" == "[]" ]]; then
    updated="['${KEYBINDING_PATH}']"
  elif [[ "$current" == *"'${KEYBINDING_PATH}'"* ]]; then
    updated="$current"
  else
    updated="${current%]}, '${KEYBINDING_PATH}']"
  fi

  if ! gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings "$updated"; then
    echo "Could not update GNOME custom keybindings; skipping shortcut setup." >&2
    return 0
  fi
  if ! {
    gsettings set "$KEYBINDING_SCHEMA" name "$APP_NAME" &&
      gsettings set "$KEYBINDING_SCHEMA" command "\"$BIN_DEST\"" &&
      gsettings set "$KEYBINDING_SCHEMA" binding "$DEFAULT_BINDING"
  }; then
    echo "Could not configure the GNOME shortcut; the command was still installed." >&2
    return 0
  fi
  echo "Installed GNOME shortcut: $DEFAULT_BINDING"
}

main() {
  for arg in "$@"; do
    case "$arg" in
      --no-packages) SKIP_PACKAGES=1 ;;
      --http) SKIP_MODEL=1 ;;
      --no-model) SKIP_DOWNLOAD=1 ;;
      --help)
        echo "Usage: ./install.sh [--no-packages] [--http] [--no-model]"
        return 0
        ;;
      *)
        echo "Unknown option: $arg" >&2
        return 2
        ;;
    esac
  done
  if [[ ! "$MODEL_NAME" =~ ^ggml-[A-Za-z0-9._-]+\.bin$ ]]; then
    echo "MODEL_NAME must be a ggml-*.bin filename without directory components." >&2
    return 1
  fi
  if [[ "$SKIP_PACKAGES" == 0 ]]; then
    install_packages
  else
    echo "Skipping package installation."
  fi

  if ! need python3; then
    echo "Python 3.10 or newer is required." >&2
    return 1
  fi
  python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))'
  if [[ "$SKIP_MODEL" == 0 && "$SKIP_DOWNLOAD" == 0 ]]; then
    install_model
  fi
  if [[ "$SKIP_MODEL" == 0 && -z "${DICTATION_WHISPER_BIN:-}" ]] && ! need whisper-cli && [[ ! -x "${HOME}/.local/opt/whisper.cpp-${WHISPER_VERSION}/build/bin/whisper-cli" ]]; then
    echo "whisper-cli was not found. Re-run without --no-packages or set DICTATION_WHISPER_BIN." >&2
    return 1
  fi
  install_script
  install_gnome_shortcut

  echo
  echo "Installed. Whisper Dictation will open automatically when using the quick installer."
  echo "It is also available from your application menu."
  if [[ "$SKIP_MODEL" == 1 ]]; then
    echo "Set backend=http and your transcription endpoint in config.json before recording."
  fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
