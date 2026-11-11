#!/usr/bin/env bash
# Copy the PC's fm-data to the server's /srv/apps/freemotion/fm-data (the k3s move, 2026-11-11). Git Bash has no rsync,
# so this is tar over ssh. Files land as uid 1000 (moudimash99 on the server), the pods' user.
#   bash fm-copy.sh            the pre-copy: everything that moves, while the PC still runs (later passes overwrite)
#   bash fm-copy.sh --final    at the cut-over, drained: first remove the server's round state, which the PC may have
#                              deleted since (stop files, locks, pid and order files), then copy again
# Left on the PC: the drivers' scratch folder, machine files that fm setup --machine writes for the server, the PC's
# browser config (it carried a whole session's environment; the server has a cleaned one), lane browser profiles (they
# go to the fm-browser pods), the dashboard's test sandbox, the website sessions (not moved: the user's choice).
set -euo pipefail
SRC=/c/Users/Moudimash99/Documents/Coding/fm/fm-data
DEST=/srv/apps/freemotion/fm-data
SERVER=moudimash99@192.168.1.116
cd "$SRC"
SKIP_TOP=' .playwright-mcp .claude .codex .mcp.json AGENTS.md CLAUDE.md CODEX.md GEMINI.md lib .career-ops-locks '
TOP=()
for e in * .[!.]*; do [ -e "$e" ] || continue; case "$SKIP_TOP" in *" $e "*) ;; *) TOP+=("$e") ;; esac; done
EX=(--exclude=config/playwright-mcp-camoufox.json
    --exclude=tmp/fm/browser-profile-2 --exclude=tmp/fm/browser-profile-3 --exclude=tmp/fm/browser-profile-4
    --exclude=tmp/fm/browser-profile-codex --exclude=tmp/fm/hub-test
    --exclude=tmp/web-llm/profile --exclude=tmp/web-llm/gemini-cookies
    # Settings saved for the PC's own processes: its Windows PATH and account folders (burn's timer, the last night's
    # start), and the lanes' MCP files (each round writes its own).
    --exclude=tmp/fm/burn/env.json --exclude=tmp/fm/night/night-start.json --exclude='tmp/fm/night/mcp-*.json'
    # Locks (folders "<file>.lock" beside what they guard, pid files): one held on the PC means nothing on the server.
    --exclude='*.lock' --exclude='*.lock.recover' --exclude='*.pid'
    --exclude=cache2 --exclude=startupCache --exclude='*.pyc' --exclude=__pycache__)
if [ "${1:-}" = --final ]; then
  ssh "$SERVER" "cd $DEST && rm -rf tmp/fm/night tmp/fm/scheduled tmp/fm/hub tmp/fm/loop tmp/fm/gate tmp/fm/burn* && mkdir -p .career-ops-locks && echo 'round state cleared on the server'"
fi
start=$SECONDS
tar -cf - "${EX[@]}" "${TOP[@]}" | gzip -1 | ssh -o Compression=no "$SERVER" "mkdir -p $DEST/.career-ops-locks && tar -xzf - -C $DEST && du -sm $DEST | cut -f1"
echo "copied in $(( SECONDS - start )) s (MB on the server above)"
if [ "${1:-}" = --final ]; then
  # A file the PC deleted after the pre-copy is still on the server. Round state was cleared above; for data/ (the
  # records), list what only the server has, to look at before anything starts.
  find data -type f ! -path '*.lock/*' | sort > /tmp/fm-pc-data.txt
  ssh "$SERVER" "cd $DEST && find data -type f ! -path '*.lock/*' | sort" | comm -13 /tmp/fm-pc-data.txt - > /tmp/fm-server-only.txt
  echo "files in data/ only on the server: $(wc -l < /tmp/fm-server-only.txt)"; head -20 /tmp/fm-server-only.txt
  ssh "$SERVER" "grep -o '\"enabled\": *[a-z]*' $DEST/data/burn-settings.json" | sed 's/^/auto-apply in the server copy: /'
fi
