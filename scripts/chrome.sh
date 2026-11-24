#!/bin/bash
# Dedicated Chrome profile for the call-details sync.
#   signin  open plain Chrome on the profile so a person can sign in to Google by hand
#   start   run the same profile with a local debugging port for the script to attach to
#   stop    quit the dedicated Chrome
set -euo pipefail

PROFILE_DIR="${TCA_CHROME_PROFILE:-$HOME/.tca-call-sync/chrome-profile}"
PORT="${TCA_CHROME_PORT:-9333}"
CHROME="${TCA_CHROME_BIN:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"

running() { pgrep -f -- "--user-data-dir=$PROFILE_DIR" >/dev/null 2>&1; }
port_up() { curl -s --max-time 2 "http://127.0.0.1:$PORT/json/version" >/dev/null 2>&1; }

stop() {
  pkill -f -- "--user-data-dir=$PROFILE_DIR" 2>/dev/null || true
  for _ in $(seq 1 20); do running || return 0; sleep 0.5; done
  echo "Chrome did not quit" >&2; return 1
}

case "${1:-}" in
  signin)
    running && stop
    mkdir -p "$PROFILE_DIR"
    "$CHROME" --user-data-dir="$PROFILE_DIR" --no-first-run --no-default-browser-check \
      "https://ads.google.com/" >/dev/null 2>&1 &
    echo "Chrome opened. Sign in by hand, then run: $0 start"
    ;;
  start)
    if port_up; then echo "ready on port $PORT"; exit 0; fi
    running && stop
    mkdir -p "$PROFILE_DIR"
    "$CHROME" --user-data-dir="$PROFILE_DIR" --remote-debugging-port="$PORT" \
      --no-first-run --no-default-browser-check about:blank >/dev/null 2>&1 &
    for _ in $(seq 1 40); do
      if port_up; then echo "ready on port $PORT"; exit 0; fi
      sleep 0.5
    done
    echo "Chrome did not open port $PORT" >&2; exit 1
    ;;
  stop)
    stop && echo "stopped"
    ;;
  *)
    echo "usage: $0 signin|start|stop" >&2; exit 2
    ;;
esac
