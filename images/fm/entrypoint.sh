#!/bin/bash
# Free Motion's container (Dockerfile): `controller` or `browser`; anything else runs as given.
#   controller  scheduled.mjs daemon, Task Scheduler's stand-in, with every script and driver CLI under it
#   browser     one lane's Camoufox behind @playwright/mcp on :8931, working folder /fm/fm-data (the same path as the
#               controller's, so file names and uploads mean the same file on both sides)
# A virtual screen for both: some scripts open a visible window (liveness-browser), and a browser can run headful.
set -e
export DISPLAY=:99
Xvfb :99 -screen 0 1680x1050x24 -nolisten tcp > /tmp/xvfb.log 2>&1 &

case "${1:-controller}" in
  controller)
    # agy keeps its sign-in in the Secret Service: a session bus and gnome-keyring, its keyring in $HOME (a volume).
    eval "$(dbus-launch --sh-syntax)"
    export DBUS_SESSION_BUS_ADDRESS
    printf '%s' "${FM_KEYRING_PASSWORD:-free-motion}" | gnome-keyring-daemon --unlock --components=secrets > /dev/null 2>&1 || true
    # The disguised Camoufox's config, with this machine's browser and add-on (the PC's copy names Windows paths).
    if [ -f /fm/fm-data/config/playwright-mcp-camoufox.json ]; then
      node /usr/local/lib/fm/camoufox-config.mjs --in /fm/fm-data/config/playwright-mcp-camoufox.json || echo "camoufox config not rewritten"
    fi
    exec node /fm/ws/fm-orchestrator/freemotion-night/scheduled.mjs daemon
    ;;
  browser)
    node /usr/local/lib/fm/camoufox-config.mjs --in /fm/fm-data/config/playwright-mcp-camoufox.json --out /tmp/camoufox.json
    exec playwright-mcp --config /tmp/camoufox.json --port 8931 --host 0.0.0.0 \
      --allowed-hosts "${FM_ALLOWED_HOSTS:-*}" --user-data-dir /profile
    ;;
  *)
    exec "$@"
    ;;
esac
