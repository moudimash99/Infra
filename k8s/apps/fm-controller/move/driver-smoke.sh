#!/usr/bin/env bash
# Run INSIDE fm-controller at the cut-over, before the first real job (auto-apply still off):
#   kubectl -n apps exec -i deploy/fm-controller -- bash -s < driver-smoke.sh
# Each driver, with run.sh's own flags for remote browsers, opens a page on one lane's browser and reads it back.
# No job sheet, no claim, no ledger: it checks the logins and the driver-to-browser path, nothing else.
set -u
cd /fm/fm-data
# agy's login is in the keyring, on the session bus the entrypoint started for the daemon: take its address.
DPID=$(pgrep -f 'scheduled.mjs daemon' | head -1)
[ -n "$DPID" ] && export "$(tr '\0' '\n' < /proc/$DPID/environ | grep '^DBUS_SESSION_BUS_ADDRESS=')"
[ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ] || . /tmp/dbus.env 2>/dev/null || echo "note: no session bus; agy may not find its login"
IPS=$(getent ahostsv4 "${FM_BROWSER_SERVICE:-fm-browser.apps.svc.cluster.local}" | awk '{print $1}' | sort -u)
[ -n "$IPS" ] || { echo "no ready browser pod"; exit 1; }
BURL="http://$(echo "$IPS" | head -1):8931/mcp"
ME=$(hostname -i | awk '{print $1}')
WORD="smoke-$RANDOM-$RANDOM"
node -e "require('http').createServer((q,r)=>{r.writeHead(200,{'content-type':'text/html'});r.end('<title>fm smoke</title><p>the word is $WORD</p>')}).listen(8932,'0.0.0.0')" &
SRV=$!
trap 'kill $SRV 2>/dev/null' EXIT
sleep 1
for u in $IPS; do node "$FM_WS/fm-agent/freemotion-night/browser-relay.mjs" --check "http://$u:8931/mcp" && echo "browser $u answers"; done
P="Use the browser tool to open http://$ME:8932/ and reply with only the word that follows 'the word is' on that page."
D=/tmp/fm-smoke; mkdir -p $D
printf '{"mcpServers":{"playwright":{"type":"http","url":"%s"}}}\n' "$BURL" > $D/claude.json
printf '{"mcpServers":{"playwright":{"type":"http","url":"%s","tools":["*"]}}}\n' "$BURL" > $D/copilot.json
ok() { if grep -q "$WORD" "$2"; then echo "PASS $1"; else echo "FAIL $1: $(tail -c 400 "$2" | tr '\n' ' ')"; fi; }
for a in 1 2 3; do
  CLAUDE_CONFIG_DIR=$HOME/.claude-account$a timeout 300 claude -p "$P" --model sonnet --dangerously-skip-permissions \
    --mcp-config $D/claude.json --strict-mcp-config --output-format json < /dev/null > $D/claude$a.out 2>&1
  ok "claude account$a" $D/claude$a.out
done
timeout 300 codex exec --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check -C /fm/fm-data --json \
  -c "mcp_servers.playwright={url=\"$BURL\",default_tools_approval_mode=\"approve\"}" "$P" < /dev/null > $D/codex.out 2>&1
ok codex $D/codex.out
timeout 300 copilot -p "$P" --allow-all --no-ask-user --no-color --output-format json --additional-mcp-config "@$D/copilot.json" < /dev/null > $D/copilot.out 2>&1
ok copilot $D/copilot.out
# agy: its global entry is the relay in remote mode (run.sh sets it); the relay reads FM_BROWSER_URL.
agy mcp list 2>/dev/null | grep -qE '^playwright[[:space:]].*browser-relay' || agy mcp add playwright node "$FM_WS/fm-agent/freemotion-night/browser-relay.mjs" > /dev/null 2>&1
FM_BROWSER_URL=$BURL timeout 300 agy -p "$P" --dangerously-skip-permissions --print-timeout 5m --output-format json < /dev/null > $D/agy.out 2>&1
ok agy $D/agy.out
gh auth status > $D/gh.out 2>&1 && echo "PASS gh" || echo "FAIL gh: $(tail -c 200 $D/gh.out)"
