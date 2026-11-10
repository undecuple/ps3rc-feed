#!/bin/sh
# Headless browser check of index.html over file:// (no web URL is opened).
# Set NODE and CHROMIUM to override discovery; set NO_SANDBOX=1 only if the browser cannot start otherwise.
cd "$(dirname "$0")/.." || exit 1
NODE=${NODE:-$(command -v node || ls -d /nix/store/*-nodejs-2*/bin/node 2>/dev/null | head -n1)}
CHROMIUM=${CHROMIUM:-$(command -v chromium || command -v google-chrome-stable || ls -d /nix/store/*-chromium-[0-9]*/bin/chromium 2>/dev/null | head -n1)}
[ -x "$NODE" ] || { echo "no node found"; exit 2; }
[ -x "$CHROMIUM" ] || { echo "no chromium found"; exit 2; }
exec "$NODE" tools/browser_check.mjs "$CHROMIUM" ${NO_SANDBOX:+--no-sandbox}
