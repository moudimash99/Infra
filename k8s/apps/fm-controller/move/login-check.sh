#!/usr/bin/env bash
# Right after login day, in the fm-login pod (recreated first, so the logins have survived a fresh start):
#   kubectl -n apps exec -i fm-login -- bash -s < login-check.sh
# One tiny call per CLI, no browser, no job: does each login work from the home volume, as the controller will see it?
set -u
cd /tmp
. /tmp/dbus.env 2>/dev/null || echo "note: no /tmp/dbus.env; agy may not find its login"
P='Reply with exactly the word OK and nothing else.'
check() { local name=$1; shift; local out; out=$(timeout 180 "$@" 2>&1 < /dev/null); if grep -q 'OK' <<< "$out"; then echo "PASS $name"; else echo "FAIL $name: $(tail -c 300 <<< "$out" | tr '\n' ' ')"; fi; }
for a in 1 2 3; do
  check "claude account$a" env CLAUDE_CONFIG_DIR=$HOME/.claude-account$a claude -p "$P" --model haiku --tools "" --strict-mcp-config --no-session-persistence
done
check codex codex exec --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox "$P"
check copilot copilot -p "$P" --allow-all --no-ask-user --no-color
check agy agy -p "$P" --dangerously-skip-permissions --print-timeout 2m
if gh auth status > /tmp/gh.out 2>&1; then echo "PASS gh ($(grep -o 'account [^ ]*' /tmp/gh.out | head -1))"; else echo "FAIL gh: $(tail -c 200 /tmp/gh.out)"; fi
ls -la "$HOME" | grep -E '\.claude-account|\.codex|\.copilot|\.gemini|\.config' | awk '{print "  " $1, $3, $NF}'
