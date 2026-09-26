#!/bin/sh
set -eu

display_file="$(mktemp -t whisper-dictation-display.XXXXXX)"
xvfb_pid=""
cleanup() {
  if [ -n "$xvfb_pid" ]; then
    kill "$xvfb_pid" 2>/dev/null || true
    wait "$xvfb_pid" 2>/dev/null || true
  fi
  rm -f -- "$display_file"
}
trap cleanup EXIT INT TERM

# Xvfb writes the selected free display number only after it is ready.
Xvfb -displayfd 3 -screen 0 1280x1024x24 -nolisten tcp 3>"$display_file" &
xvfb_pid=$!
attempt=0
while [ ! -s "$display_file" ]; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 50 ] || ! kill -0 "$xvfb_pid" 2>/dev/null; then
    echo "Virtual display did not start." >&2
    exit 1
  fi
  sleep 0.1
done
DISPLAY=":$(sed -n '1p' "$display_file")"
export DISPLAY
"$@"
