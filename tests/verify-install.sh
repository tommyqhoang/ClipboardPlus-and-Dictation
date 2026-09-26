#!/usr/bin/env bash
# Run inside a bare distribution container (see the linux-distro-install CI job):
#   docker run --rm -v "$PWD:/src:ro" fedora:latest bash /src/tests/verify-install.sh
set -euo pipefail
cp -r /src /app
cd /app
./install.sh --no-model
python="${HOME}/.local/share/whisper-dictation/venv/bin/python"
"$python" -c "import importlib.util, PIL, tkinter, gi; assert importlib.util.find_spec('pystray')"
command -v whisper-cli >/dev/null || test -x "${HOME}"/.local/opt/whisper.cpp-*/build/bin/whisper-cli
echo "install verified"
