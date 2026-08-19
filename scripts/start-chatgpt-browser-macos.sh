#!/bin/sh
set -eu

CWS_BROWSER_PROFILE_DIR=${CWS_BROWSER_PROFILE_DIR:-"$PWD/data/chrome-profile"}
CWS_CDP_PORT=${CWS_CDP_PORT:-9222}
CHROME_BINARY="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

if [ ! -x "$CHROME_BINARY" ]; then
  echo "Google Chrome was not found at: $CHROME_BINARY" >&2
  exit 1
fi

mkdir -p "$CWS_BROWSER_PROFILE_DIR"

exec "$CHROME_BINARY" \
  --remote-debugging-address=0.0.0.0 \
  --remote-debugging-port="$CWS_CDP_PORT" \
  --remote-allow-origins='*' \
  --user-data-dir="$CWS_BROWSER_PROFILE_DIR" \
  https://chatgpt.com/ "$@"
