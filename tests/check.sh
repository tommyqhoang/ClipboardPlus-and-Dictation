#!/bin/sh
# Everything CI (.github/workflows/quality.yml) checks, run here first: minutes of
# local feedback instead of a push and a wait for GitHub Actions.
#
#   sh tests/check.sh          lint, format, types for Linux, macOS and Windows, shell
#                              scripts, and the tests with coverage (about a minute)
#   sh tests/check.sh --full   also Python 3.10 and a bare install on each Linux
#                              distribution CI uses, in Docker (about ten minutes)
#
# Installed as a pre-push hook with: git config core.hooksPath .githooks
set -eu
cd "$(dirname "$0")/.."
export DO_NOT_TRACK=1 DICTATION_TELEMETRY=0

full=0
if [ "${1:-}" = "--full" ]; then
  full=1
fi
failed=""
step() { printf '\n== %s\n' "$1"; }
fail() { failed="$failed\n  $1"; }

# The tests need Tk, which some Pythons (Homebrew's) lack.
python="${PYTHON:-}"
if [ -z "$python" ]; then
  for candidate in python3 /usr/bin/python3; do
    if "$candidate" -c "import tkinter, coverage" 2>/dev/null; then
      python="$candidate"
      break
    fi
  done
fi
if [ -z "$python" ]; then
  echo "No Python with tkinter and coverage: pip install -r requirements-dev.txt, or set PYTHON." >&2
  exit 1
fi

# A tool from requirements-dev.txt, or the same one inside a container.
tool() {
  name="$1" image="$2"
  shift 2
  if command -v "$name" >/dev/null 2>&1; then
    "$name" "$@"
  elif command -v docker >/dev/null 2>&1; then
    docker run --rm -v "$PWD:/mnt:ro" -w /mnt "$image" "$@"
  else
    echo "$name is not installed and there is no Docker to run it in." >&2
    return 1
  fi
}

SCRIPTS="bin/dictate-toggle bootstrap.sh install.sh uninstall.sh tests/check.sh tests/with-xvfb.sh tests/verify-install.sh"
DISTROS="ubuntu:latest debian:stable fedora:latest archlinux:latest opensuse/tumbleweed:latest"

if [ "$full" = 1 ]; then
  # The slow part starts first and runs alongside everything else.
  if ! command -v docker >/dev/null 2>&1; then
    echo "--full needs Docker." >&2
    exit 1
  fi
  logs="$(mktemp -d -t clipboardplus-check.XXXXXX)"
  for image in $DISTROS; do
    log="$logs/$(echo "$image" | tr '/:' '__').log"
    (docker run --rm -v "$PWD:/src:ro" "$image" bash /src/tests/verify-install.sh >"$log" 2>&1 && echo ok >>"$log") &
  done
  (docker run --rm -v "$PWD:/src:ro" python:3.10 sh -c \
    'cp -r /src /app && cd /app && python -m unittest discover -s tests && python -m compileall -q lib setup-desktop.py' \
    >"$logs/python-3.10.log" 2>&1 && echo ok >>"$logs/python-3.10.log") &
fi

step "ruff"
ruff check lib tests tools setup-desktop.py || fail "ruff check"
ruff format --check lib tests tools setup-desktop.py || fail "ruff format"

# CI type-checks on each operating system; --platform does all three from here.
for platform in linux darwin win32; do
  step "mypy --strict ($platform)"
  mypy --strict --platform "$platform" lib tools setup-desktop.py || fail "mypy ($platform)"
done

step "shellcheck and shfmt"
# shellcheck disable=SC2086 # The list is split into file names on purpose.
tool shellcheck koalaman/shellcheck:stable $SCRIPTS || fail "shellcheck"
# shellcheck disable=SC2086
tool shfmt mvdan/shfmt:latest -d -i 2 -ci $SCRIPTS || fail "shfmt"

step "tests and coverage"
rm -f .coverage .coverage.*
tests="$python -m coverage run -m unittest discover -s tests && $python -m coverage combine -q && $python -m coverage report"
if command -v Xvfb >/dev/null 2>&1; then
  sh tests/with-xvfb.sh sh -c "$tests" || fail "tests or coverage"
else
  sh -c "$tests" || fail "tests or coverage"
fi
"$python" -m compileall -q lib setup-desktop.py || fail "compileall"

if [ "$full" = 1 ]; then
  step "Python 3.10 and distribution installs (Docker)"
  wait
  for log in "$logs"/*.log; do
    name="$(basename "$log" .log)"
    if [ "$(tail -n 1 "$log")" = "ok" ]; then
      echo "  ok      $name"
    else
      echo "  FAILED  $name: $log"
      tail -n 5 "$log" | sed 's/^/          /'
      fail "$name"
    fi
  done
fi

if [ -n "$failed" ]; then
  printf '\nFailed:%b\n' "$failed" >&2
  exit 1
fi
printf '\nAll checks passed.\n'
