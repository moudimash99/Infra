#!/usr/bin/env bash
# RECORD: ran on 2026-11-21 (the fm-pool#7 memory fix, live since ~11:18). The worktrees and memory-diet branches it
# names were removed on 2026-11-22 after their commits reached main, so it does not run as is any more.
# Deploy the fm-controller memory fixes (moudimash99/fm-pool#7). Run from the PC in Git Bash:
#   bash /c/Users/Moudimash99/Documents/Coding/fm/.worktrees/deploy-memory-fix.sh
# It stops at the first problem. It never touches a round: it refuses while run.sh runs, and restarts only the
# three score loops and the hub agent (no pod restart, no image, no manifest).
set -euo pipefail
export MSYS_NO_PATHCONV=1
FM=C:/Users/Moudimash99/Documents/Coding/fm   # Windows form: with MSYS_NO_PATHCONV, git.exe can't read /c/...
WT=$FM/.worktrees
SRV=moudimash99@192.168.1.116
X() { kubectl --context homelab -n apps exec deploy/fm-controller -- sh -c "$1"; }

echo "== 0. no round running"
[ "$(X 'ps -eo args | grep -c "[r]un.sh" || true')" = 0 ] || { echo "a round is running: try again between rounds"; exit 1; }

echo "== 1. code: the bare repos on the server, then the live checkouts (fast-forward only)"
for r in fm-pool fm-orchestrator fm-agent; do
  git -C "$WT/$r-memory" push -q "$SRV:/srv/apps/freemotion/git/$r.git" memory-diet:main
  ssh "$SRV" "git -C /srv/apps/freemotion/ws/$r pull --ff-only -q && echo \"   $r: \$(git -C /srv/apps/freemotion/ws/$r log --oneline -1)\""
done

echo "== 2. the score loops: stop the old ones (old code), start the new ones through the daemon (cap 384 MB, keeper)"
for n in "" flash gemma; do X "cd /fm/fm-data && node /fm/ws/fm-pool/freemotion-night/score-loop.mjs ${n:+--name $n} --stop"; done
X 'touch /fm/fm-data/tmp/fm/scheduled/run-now-loops'

echo "== 3. the hub agent, capped, on the server's own data (sends nothing)"
if ! X 'cd /fm/fm-data && node --max-old-space-size=384 /fm/ws/fm-agent/freemotion-night/hub-agent.mjs --dry-run'; then
  echo "the capped agent's dry run failed: the old agent is left running (the loops are already on the new code)"
  exit 1
fi

echo "== 4. the agent: SIGTERM (it drops its lock), then the daemon starts it again, capped"
X 'p=$(ps -eo pid,args | awk "/[s]cheduled.mjs agent/{print \$1}"); [ -n "$p" ] && kill -TERM $p; for i in 1 2 3 4 5 6 7 8 9 10; do kill -0 $p 2>/dev/null || break; sleep 1; done; touch /fm/fm-data/tmp/fm/scheduled/run-now-agent'

echo "== 5. check (after 90 s)"
sleep 90
X 'ps -eo pid,ppid,rss,args | grep -E "[s]core-loop.mjs|[h]ub-agent.mjs|[s]cheduled.mjs agent" | cut -c1-150'
X 'cd /fm/fm-data/tmp/fm && node -e "for (const n of [\"\", \"-flash\", \"-gemma\"]) { try { const s = JSON.parse(require(\"fs\").readFileSync(\"score-loop\" + n + \"-status.json\", \"utf8\")); console.log(\"loop\" + (n || \"-main\"), s.state, JSON.stringify(s.memMB || \"no memMB yet\")); } catch (e) { console.log(\"loop\" + n, e.message); } }"; tail -3 hub/agent.log; tail -n 5 scheduled/loop-*.err 2>/dev/null || true'
kubectl --context homelab -n apps top pod -l app=fm-controller

echo "== 6. the PC's main checkouts follow (fast-forward; fm-pool's uncommitted make-pool.mjs edits stay)"
for r in fm-pool fm-orchestrator fm-agent; do git -C "$FM/$r" merge --ff-only -q memory-diet && echo "   $r: $(git -C "$FM/$r" log --oneline -1)"; done
echo "done"
