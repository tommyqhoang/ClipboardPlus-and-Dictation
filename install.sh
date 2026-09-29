#!/usr/bin/env bash
set -euo pipefail

APP_NAME="Clipboard+: dictation"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
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
ALLOW_UNVERIFIED_MODEL="${DICTATION_ALLOW_UNVERIFIED_MODEL:-0}"
KEYBINDING_PATH="/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/dictation/"
KEYBINDING_SCHEMA="org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:${KEYBINDING_PATH}"
DEFAULT_BINDING="${DICTATION_BINDING:-<Shift><Super>d}"

PYTHON=""
PM=""
RUNTIME_PACKAGES=()
BUILD_PACKAGES=()
WHISPER_PACKAGE=""
OPTIONAL_PACKAGES=()
TRAY_PACKAGE=""
APT_UPDATED=0

need() {
  command -v "$1" >/dev/null 2>&1
}

python_has() {
  "$1" -c "import $2" >/dev/null 2>&1
}

# The distribution's Tk and GTK packages serve its own interpreter, which is not
# always the python3 first on PATH (Homebrew, pyenv, conda). Prefer one that has
# everything the tray needs, then any python3 so the checks can report what is
# missing.
find_python() {
  local candidate first=""
  for candidate in /usr/bin/python3 "$(command -v python3 || true)"; do
    if [[ -n "$candidate" ]] && need "$candidate"; then
      first="${first:-$candidate}"
      if python_has "$candidate" "tkinter, venv, gi"; then
        PYTHON="$candidate"
        return 0
      fi
    fi
  done
  PYTHON="$first"
  [[ -n "$PYTHON" ]]
}

detect_package_manager() {
  local manager
  for manager in apt-get dnf pacman zypper; do
    if need "$manager"; then
      PM="$manager"
      return 0
    fi
  done
  return 1
}

# Package names per distribution family; every one is verified to exist.
select_packages() {
  case "$PM" in
    apt-get)
      RUNTIME_PACKAGES=(alsa-utils ca-certificates curl gir1.2-ayatanaappindicator3-0.1
        gnome-session-canberra libnotify-bin perl python3 python3-gi python3-tk python3-venv
        util-linux wl-clipboard xclip)
      BUILD_PACKAGES=(build-essential cmake git)
      WHISPER_PACKAGE=whisper.cpp
      OPTIONAL_PACKAGES=(wtype xdotool x11-xserver-utils)
      TRAY_PACKAGE=gnome-shell-extension-appindicator
      ;;
    dnf)
      RUNTIME_PACKAGES=(alsa-utils ca-certificates curl libayatana-appindicator-gtk3 libnotify
        perl python3 python3-gobject python3-tkinter util-linux wl-clipboard xclip)
      BUILD_PACKAGES=(cmake gcc-c++ git make)
      # Fedora's whisper-cpp depends on PyTorch and ROCm (8 GiB); build the pinned CPU release.
      WHISPER_PACKAGE=""
      OPTIONAL_PACKAGES=(wtype xdotool xrandr)
      TRAY_PACKAGE=gnome-shell-extension-appindicator
      ;;
    pacman)
      RUNTIME_PACKAGES=(alsa-utils ca-certificates curl libayatana-appindicator libnotify perl
        python python-gobject tk util-linux wl-clipboard xclip)
      BUILD_PACKAGES=(base-devel cmake git)
      WHISPER_PACKAGE=""
      OPTIONAL_PACKAGES=(wtype xdotool xorg-xrandr)
      TRAY_PACKAGE=gnome-shell-extension-appindicator
      ;;
    zypper)
      RUNTIME_PACKAGES=(alsa-utils ca-certificates curl libnotify-tools perl python3
        python3-gobject python3-tk typelib-1_0-AyatanaAppIndicator3-0_1 util-linux wl-clipboard xclip)
      BUILD_PACKAGES=(cmake gcc-c++ git make)
      WHISPER_PACKAGE=""
      OPTIONAL_PACKAGES=(wtype xdotool xrandr)
      TRAY_PACKAGE=gnome-shell-extension-appindicator
      ;;
    *) return 1 ;;
  esac
}

as_root() {
  if [[ "$(id -u)" == 0 ]]; then
    "$@"
  elif need sudo; then
    sudo "$@"
  else
    echo "Administrator access is needed to install packages, but sudo was not found." >&2
    echo "Run this installer as root, or install the packages yourself and use --no-packages." >&2
    return 1
  fi
}

pm_install() {
  case "$PM" in
    apt-get)
      if [[ "$APT_UPDATED" == 0 ]]; then
        as_root apt-get update || return 1
        APT_UPDATED=1
      fi
      as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y "$@"
      ;;
    dnf) as_root dnf install -y --setopt=install_weak_deps=False "$@" ;;
    pacman)
      # A fresh system may have no package database yet; sync it and retry once.
      as_root pacman -S --needed --noconfirm "$@" ||
        {
          as_root pacman -Sy --noconfirm &&
            as_root pacman -S --needed --noconfirm "$@"
        }
      ;;
    zypper) as_root zypper --non-interactive install --no-recommends "$@" ;;
  esac
}

typelib_available() {
  "$PYTHON" - <<'PY' >/dev/null 2>&1
import gi

for name in ("AyatanaAppIndicator3", "AppIndicator3"):
    try:
        gi.require_version(name, "0.1")
        raise SystemExit(0)
    except ValueError:
        pass
raise SystemExit(1)
PY
}

# True when something the desktop app needs is absent, so nothing is installed
# (and no administrator prompt appears) on a machine that is already ready.
runtime_missing() {
  local tool
  for tool in arecord wl-copy xclip notify-send curl; do
    need "$tool" || return 0
  done
  find_python || return 0
  python_has "$PYTHON" "tkinter, venv, gi" || return 0
  typelib_available || return 0
  return 1
}

unsupported_system() {
  cat >&2 <<'MSG'
No supported package manager found (apt, dnf, pacman or zypper).
Install these yourself, then re-run with --no-packages:
  - Python 3.10+ with Tk (tkinter), venv and PyGObject (gi)
  - AyatanaAppIndicator3 (libayatana-appindicator) GObject bindings
  - arecord (alsa-utils), wl-copy (wl-clipboard), xclip, notify-send (libnotify), curl
  - whisper-cli (whisper.cpp), or set DICTATION_WHISPER_BIN
MSG
}

install_packages() {
  if runtime_missing; then
    if ! detect_package_manager; then
      unsupported_system
      return 1
    fi
    select_packages
    echo "Installing missing dependencies with $PM: ${RUNTIME_PACKAGES[*]}"
    pm_install "${RUNTIME_PACKAGES[@]}"
    find_python || true
    if runtime_missing; then
      echo "Dependencies are still missing after installation; see the messages above." >&2
      return 1
    fi
  else
    echo "System dependencies already installed."
  fi
  install_optional_packages
  if [[ "$SKIP_MODEL" == 0 ]]; then
    if ! install_whisper; then
      echo "Speech engine installation failed. Clipboard+ will still install; retry speech setup later." >&2
      SKIP_DOWNLOAD=1
    fi
  fi
}

install_optional_packages() {
  detect_package_manager || return 0
  select_packages
  local index package
  local tools=(wtype xdotool xrandr)
  for index in "${!tools[@]}"; do
    if ! need "${tools[$index]}"; then
      package="${OPTIONAL_PACKAGES[$index]}"
      pm_install "$package" || echo "Optional helper $package is unavailable; its feature may need manual setup." >&2
    fi
  done
  if [[ "${XDG_CURRENT_DESKTOP:-}" == *GNOME* ]]; then
    local extension=appindicatorsupport@rgcjonas.gmail.com
    if ! need gnome-extensions || ! gnome-extensions info "$extension" >/dev/null 2>&1; then
      pm_install "$TRAY_PACKAGE" || echo "Install the GNOME AppIndicator extension to show the tray icon." >&2
    fi
    if need gnome-extensions; then
      gnome-extensions enable "$extension" 2>/dev/null ||
        echo "Log out and back in, then enable AppIndicator support in GNOME Extensions."
    fi
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

  detect_package_manager || true
  select_packages 2>/dev/null || true
  if [[ -n "$WHISPER_PACKAGE" ]]; then
    echo "Installing $PM package: $WHISPER_PACKAGE"
    if pm_install "$WHISPER_PACKAGE" && need whisper-cli; then
      return 0
    fi
    echo "Package install did not provide whisper-cli; falling back to source build."
  else
    echo "No whisper.cpp package for this system; building from source."
  fi

  install_whisper_from_source
}

install_whisper_from_source() {
  local src_dir="${HOME}/.local/opt/whisper.cpp-${WHISPER_VERSION}"
  local bin_path="${src_dir}/build/bin/whisper-cli"

  if ! { need cmake && need git && { need cc || need gcc; } && need make; }; then
    if [[ -z "$PM" ]]; then
      echo "Building whisper.cpp needs cmake, git, make and a C++ compiler." >&2
      return 1
    fi
    echo "Installing build tools: ${BUILD_PACKAGES[*]}"
    pm_install "${BUILD_PACKAGES[@]}" || return 1
  fi

  mkdir -p "${HOME}/.local/opt" || return 1
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
    # Fetch the pinned commit itself (the $WHISPER_VERSION tag is documentation and
    # could be moved), then confirm what was checked out.
    echo "Fetching whisper.cpp $WHISPER_VERSION ($WHISPER_COMMIT) into $src_dir"
    if ! { git init -q "$src_dir" &&
      git -C "$src_dir" fetch -q --depth 1 https://github.com/ggml-org/whisper.cpp.git "$WHISPER_COMMIT" &&
      git -C "$src_dir" checkout -q FETCH_HEAD; }; then
      rm -rf -- "$src_dir"
      return 1
    fi
    if [[ "$(git -C "$src_dir" rev-parse HEAD)" != "$WHISPER_COMMIT" ]]; then
      echo "Downloaded whisper.cpp source did not match the pinned commit." >&2
      rm -rf -- "$src_dir"
      return 1
    fi
  fi

  echo "Building whisper-cli and whisper-server from source"
  cmake -S "$src_dir" -B "$src_dir/build" -DCMAKE_BUILD_TYPE=Release -DWHISPER_SDL2=OFF || return 1
  cmake --build "$src_dir/build" --config Release --target whisper-cli whisper-server -j"$(nproc)" || return 1

  if [[ ! -x "$bin_path" ]]; then
    echo "Build finished, but whisper-cli was not found at $bin_path." >&2
    return 1
  fi

  echo "Built whisper-cli: $bin_path"
}

# Download URL to DEST, resuming a leftover DEST only when the server proves it
# can continue from exactly that byte (206 + matching Content-Range); anything
# else discards the partial file and starts over.
download_model() {
  local url="$1" dest="$2" size=0 headers status range chunk="${2}.chunk"
  local common=(--proto '=https' --proto-redir '=https' --fail --location --retry 3
    --retry-delay 2 --connect-timeout 15)
  headers="$(mktemp)" || return 1
  if [[ -s "$dest" ]]; then
    size="$(wc -c <"$dest" | tr -d ' ')"
    rm -f -- "$chunk"
    if curl "${common[@]}" --header "Range: bytes=${size}-" --dump-header "$headers" \
      --output "$chunk" "$url"; then
      status="$(tr -d '\r' <"$headers" | awk 'toupper($1) ~ /^HTTP\// {s=$2} END {print s}')"
      range="$(tr -d '\r' <"$headers" | awk -F': *' 'tolower($1)=="content-range" {r=$2} END {print r}')"
      if [[ "$status" == 206 && "$range" == "bytes ${size}-"* ]]; then
        cat -- "$chunk" >>"$dest" && rm -f -- "$chunk" "$headers"
        return
      elif [[ "$status" == 200 ]]; then
        mv -f -- "$chunk" "$dest" && rm -f -- "$headers" # The server ignored the range: full file.
        return
      fi
    fi
    echo "The server cannot resume the earlier partial download; starting over."
    rm -f -- "$chunk" "$dest"
  fi
  rm -f -- "$headers"
  curl "${common[@]}" --output "$dest" "$url"
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

  if [[ -z "$expected_sha256" && "$ALLOW_UNVERIFIED_MODEL" != 1 ]]; then
    echo "No SHA-256 is known for $MODEL_NAME. Set DICTATION_MODEL_SHA256, or pass" >&2
    echo "--allow-unverified-model (DICTATION_ALLOW_UNVERIFIED_MODEL=1) to accept it unverified." >&2
    return 1
  fi
  mkdir -p "$MODEL_DIR" || return 1
  if [[ -s "$MODEL_DEST" ]]; then
    echo "Model already present: $MODEL_DEST"
  else
    echo "Downloading Whisper model: $MODEL_NAME"
    download_model "$MODEL_URL" "$partial_dest" || return 1
    if [[ ! -s "$partial_dest" ]]; then
      echo "Model download completed without producing a usable file." >&2
      return 1
    fi
    candidate="$partial_dest"
  fi

  [[ -n "$PYTHON" ]] || find_python || return 1
  # Reject common HTML/error downloads. The optional SHA-256 verifies the entire file.
  "$PYTHON" - "$candidate" "$expected_sha256" <<'PY' || return 1
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
    echo "Warning: accepting $MODEL_NAME with only a GGML header check (--allow-unverified-model)." >&2
  fi
  if [[ "$candidate" == "$partial_dest" ]]; then
    mv -f "$partial_dest" "$MODEL_DEST" || return 1
    echo "Downloaded model: $MODEL_DEST"
  fi

  if [[ -e "$MODEL_LINK" && ! -L "$MODEL_LINK" ]]; then
    echo "Refusing to replace a regular file at $MODEL_LINK." >&2
    return 1
  fi
  ln -sfn "$MODEL_DEST" "$MODEL_LINK" || return 1
  echo "Selected model: $MODEL_LINK -> $MODEL_NAME"
}

install_script() {
  mkdir -p "${HOME}/.local/bin"
  # setup-desktop.py installs the app into ~/.local/lib/whisper-dictation and writes
  # the dictate-toggle launcher.
  "$PYTHON" "${PROJECT_DIR}/setup-desktop.py"
  echo "Installed $BIN_DEST"
}

install_gnome_shortcut() {
  if ! need gsettings; then
    echo "gsettings not found; skipping GNOME shortcut setup."
    echo "Bind a shortcut to ${BIN_DEST} in your desktop's keyboard settings to start and stop dictation."
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
      gsettings set "$KEYBINDING_SCHEMA" command "\"$BIN_DEST --via-shortcut\"" &&
      gsettings set "$KEYBINDING_SCHEMA" binding "" &&
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
      --allow-unverified-model) ALLOW_UNVERIFIED_MODEL=1 ;;
      --help)
        echo "Usage: ./install.sh [--no-packages] [--http] [--no-model] [--allow-unverified-model]"
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

  if ! find_python || ! "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
    echo "Python 3.10 or newer is required." >&2
    return 1
  fi
  if [[ "$SKIP_MODEL" == 0 && "$SKIP_DOWNLOAD" == 0 ]]; then
    if ! install_model; then
      echo "Speech model installation failed. Clipboard+ will still install; retry the model in setup." >&2
    fi
  fi
  if [[ "$SKIP_MODEL" == 0 && -z "${DICTATION_WHISPER_BIN:-}" ]] && ! need whisper-cli && [[ ! -x "${HOME}/.local/opt/whisper.cpp-${WHISPER_VERSION}/build/bin/whisper-cli" ]]; then
    echo "Speech engine unavailable. Clipboard+ will install; re-run without --no-packages or set DICTATION_WHISPER_BIN for local dictation." >&2
  fi
  install_script
  install_gnome_shortcut

  if [[ -n "${DICTATION_QUICK_INSTALL:-}" ]]; then
    return 0 # The quick installer opens the app and says so.
  fi
  echo
  if [[ "$SKIP_MODEL" == 1 ]]; then
    echo "No speech model was installed: choose one (or your own AI service) during setup."
  fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
