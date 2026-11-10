#!/bin/sh
# Build the lab image on the server and load it on the Acer (no registry yet; the image goes through the PC, the
# server has no key for the Acer). Run from the Infra repo root on the PC: sh lab/build.sh [TAG].
set -e
TAG=${1:-$(date +%Y-%m-%d-%H%M)}
SERVER=moudimash99@192.168.1.116
ACER=moudimash99@192.168.1.93
tar -c --exclude=__pycache__ --exclude=tests -C lab Dockerfile requirements.txt app \
  | ssh $SERVER "rm -rf ~/build/lab && mkdir -p ~/build/lab && tar -x -C ~/build/lab"
ssh $SERVER "cd ~/build/lab && docker build -q -t lab:$TAG ."
ssh $SERVER "docker save lab:$TAG" | ssh $ACER "sudo k3s ctr -n k8s.io images import -"
echo "loaded docker.io/library/lab:$TAG on the Acer"
