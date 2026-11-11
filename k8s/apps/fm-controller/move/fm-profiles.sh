#!/usr/bin/env bash
# At the cut-over (drained: no browser open on the PC), the PC's two lane browser profiles, with their site sessions,
# become the two fm-browser pods' profiles on the Acer (k8s/apps/fm-browser). Each pod's folder is emptied first.
# Lane 1 on the PC was playwright-mcp's default profile; lane 2 had its own in fm-data. Caches and lock files stay
# behind. The pods must be at 0 replicas while this runs.
set -euo pipefail
ACER=moudimash99@192.168.1.93
DEST=/srv/apps/freemotion-browser
LOCAL=$(cygpath -u "$LOCALAPPDATA")
copy() {  # <profile folder on the PC> <pod>
  [ -d "$1" ] || { echo "missing: $1"; exit 1; }
  local start=$SECONDS
  tar -cf - -C "$1" --exclude=cache2 --exclude=startupCache --exclude=parent.lock --exclude=.parentlock --exclude=lock . \
    | gzip -1 | ssh -o Compression=no "$ACER" "d=$DEST/$2; [ -d \"\$d\" ] && find \"\$d\" -mindepth 1 -delete; mkdir -p \"\$d\" && chmod 700 \"\$d\" && tar -xzf - -C \"\$d\" && echo \"$2: \$(du -sm \"\$d\" | cut -f1) MB\""
  echo "  in $(( SECONDS - start )) s"
}
kubectl -n apps get sts fm-browser-acer -o jsonpath='{.spec.replicas}' | grep -qx 0 || { echo "fm-browser-acer is not at 0 replicas; stopping"; exit 1; }
copy "$LOCAL/ms-playwright-mcp/mcp-firefox-5930757" fm-browser-acer-0
copy /c/Users/Moudimash99/Documents/Coding/fm/fm-data/tmp/fm/browser-profile-2 fm-browser-acer-1
