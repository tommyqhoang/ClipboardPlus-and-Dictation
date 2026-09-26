#!/usr/bin/env bash
# Run inside a bare distribution container (see the linux-distro-install CI job):
#   docker run --rm -v "$PWD:/src:ro" fedora:latest bash /src/tests/verify-install.sh
set -euo pipefail
cp -r /src /app
cd /app
./install.sh --no-model
python="${HOME}/.local/share/whisper-dictation/venv/bin/python"
"$python" -c "import importlib.util, PIL, tkinter, gi, Xlib; assert importlib.util.find_spec('pystray')"
# The clipboard history: its modules import and its store works in the private environment.
"$python" - <<'PY'
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path.home() / ".local/lib"))
import clipservice, clipsync, clipui, clipwatch_linux  # noqa: E401,F401
import clipstore

store = clipstore.Store(pathlib.Path(tempfile.mkdtemp()) / "clipboard")
assert store.add_text("hello") is not None
store.close()
PY
command -v whisper-cli >/dev/null || test -x "${HOME}"/.local/opt/whisper.cpp-*/build/bin/whisper-cli
echo "install verified"
