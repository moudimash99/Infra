# Moving Free Motion from the PC to k3s (the cut-over)

Plan: fm-orchestrator#1 and `~/.claude/plans/declarative-purring-pelican.md`, step 6. Free Motion must never run on the
PC and the cluster at once: two writers of the never-apply-twice ledger could apply twice to one employer.

## Scripts

| Script | Where it runs | What it does |
|---|---|---|
| `login-check.sh` | fm-login pod, after the sign-ins | one tiny call per CLI from the home volume: claude ×3, codex, copilot, agy, gh |
| `fm-copy.sh` | PC (Git Bash) | fm-data to the server by tar over ssh; `--final` first clears the server's round state |
| `fm-profiles.sh` | PC (Git Bash) | the PC's two lane browser profiles (site sessions) into the fm-browser pods' folders on the Acer |
| `driver-smoke.sh` | inside fm-controller | each driver opens a page on a lane's browser with run.sh's flags: no job, no ledger |

Run a copy of a script, never the file itself while editing it: bash reads a script as it goes.

The copy leaves behind:
- machine files (`fm setup --machine` writes them on the server);
- the PC's browser config (it carried a whole session's environment; the server has a cleaned one);
- the PC's saved Windows PATH and account folders (`tmp/fm/burn/env.json`, `night-start.json`);
- locks;
- the lane profiles (`fm-profiles.sh` moves them);
- the website sessions (not moved).

## Before

- [x] Sign-ins done in fm-login. Recreate the pod, then `login-check.sh`: every line PASS. Done 2026-11-11 (2, 3 and agy were at their usage limits, so the login reached the account).
- [x] The Claude account for the gateway is chosen: set `GATEWAY_CLAUDE_DIR` in `k8s/apps/llm-gateway`. Account 1 (0303ddd).
- [x] fm-core, fm-orchestrator and fm-agent main hold the moved guard, the Windows-path guard and the codex fix. The server checkouts (`/srv/apps/freemotion/ws`) are pulled. Done 2026-11-11 06:11 (0e3fc2f, 06d3c2d, ccf7714).

## Cut-over

1. **PC:** turn auto-apply off first: `node fm-orchestrator/freemotion-night/burn.mjs off`. Once the guard is set, the PC's commands refuse. Wait until no `run.sh` or `full-night.sh` runs; the round in hand is never killed.
2. **PC:** disable every `\career-ops\` task and `\career-ops-test\night test`. Stop the score loops and the hub agent.
3. **PC:** `node fm-core/lib/moved.mjs set --to "k3s (fm-controller on the server)"`. `moved.mjs status` must say "blocked".
4. **PC:** `bash fm-copy.sh --final`. Look at the server-only files it lists. It must print `"enabled": false`.
5. **PC:** `bash fm-profiles.sh` (the browser pods at 0 replicas).
6. **Cluster:**
   - `kubectl -n apps scale sts fm-browser-acer --replicas=2` and wait for Ready;
   - then `kubectl -n apps scale deploy fm-controller --replicas=1`.
7. **Controller:** in `kubectl -n apps exec -it deploy/fm-controller -- bash`:
   - `cd /fm/fm-data && node ../ws/fm-core/bin/fm.mjs setup --machine`
   - `node ../ws/fm-core/bin/fm.mjs doctor`
   - then `driver-smoke.sh` from the PC: `kubectl -n apps exec -i deploy/fm-controller -- bash -s < driver-smoke.sh`. Every line PASS.
8. **Gateway:** `kubectl -n apps rollout restart deploy/llm-gateway`, if it still answers 503.
9. **One real round of 1 job:** `node /fm/ws/fm-orchestrator/freemotion-night/burn.mjs round --jobs 1` in the controller. Then check:
   - the job went through to the site's final step;
   - the dashboard shows the round, still as machine "pc";
   - `kubectl top pod` keeps the controller under 3 GiB.
10. **Auto-apply on, from the cluster:** `burn.mjs on` in the controller.

## Rollback

1. Scale fm-controller and fm-browser-acer to 0.
2. Copy fm-data back to the PC: tar over ssh, the other way.
3. On the PC: `node fm-core/lib/moved.mjs clear`, re-enable the tasks, then `burn.mjs on`.
