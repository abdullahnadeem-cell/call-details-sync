#!/usr/bin/env bash
# Dedicated Chrome profile for the call-details sync, on your own computer.
#   signin  open a visible Chrome on that profile so you can sign in to Google Ads by hand (once)
#   start   run the same profile with a local DevTools port the sync attaches to
#   stop    quit that Chrome
# Then: GADS_CHROME_DEBUG_URL=127.0.0.1:9333 python -m src.run --date 2026-11-20 --dry-run
set -euo pipefail

PROFILE_DIR="${GADS_CHROME_PROFILE:-$HOME/.gads-call-sync/chrome-profile}"
PORT="${GADS_CHROME_PORT:-9333}"
URL="${GADS_REPORT_URL:-https://ads.google.com/}"
if [ -z "${GADS_CHROME_BIN:-}" ]; then
  if [ -x "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ]; then
    GADS_CHROME_BIN="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
  else
    GADS_CHROME_BIN="$(command -v google-chrome || command -v chromium || command -v chromium-browser || true)"
  fi
fi
[ -n "$GADS_CHROME_BIN" ] || { echo "Chrome not found; set GADS_CHROME_BIN" >&2; exit 1; }

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
    "$GADS_CHROME_BIN" --user-data-dir="$PROFILE_DIR" --no-first-run --no-default-browser-check "$URL" >/dev/null 2>&1 &
    echo "Chrome opened on a dedicated profile. Sign in to Google Ads by hand, open the Call details report once, then run: $0 start"
    ;;
  start)
    if port_up; then echo "ready: GADS_CHROME_DEBUG_URL=127.0.0.1:$PORT"; exit 0; fi
    running && stop
    mkdir -p "$PROFILE_DIR"
    "$GADS_CHROME_BIN" --user-data-dir="$PROFILE_DIR" --remote-debugging-port="$PORT" --remote-allow-origins='*' \
      --no-first-run --no-default-browser-check about:blank >/dev/null 2>&1 &
    for _ in $(seq 1 40); do
      if port_up; then echo "ready: GADS_CHROME_DEBUG_URL=127.0.0.1:$PORT"; exit 0; fi
      sleep 0.5
    done
    echo "Chrome did not open port $PORT" >&2; exit 1
    ;;
  stop) stop && echo "stopped" ;;
  *) echo "usage: $0 signin|start|stop" >&2; exit 2 ;;
esac
