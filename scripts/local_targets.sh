#!/usr/bin/env bash
# Local mode: start Docker containers that play the role of real cloud target machines.
# Six targets, spanning the default OS image of five popular clouds/hosts and three package
# managers (apt-get, dnf, apk), so the agent proves itself on more than just AWS.
#
#   nomeshops-ubuntu22      AWS EC2            Ubuntu 22.04      apt-get   py 3.10
#   nomeshops-al2023        AWS EC2            Amazon Linux 2023 dnf       py 3.9
#   nomeshops-gcp-debian    Google Cloud       Debian 12         apt-get   py 3.11
#   nomeshops-azure-ubuntu  Azure/DigitalOcean Ubuntu 24.04      apt-get   py 3.12
#   nomeshops-rocky         Oracle Cloud/on-prem Rocky Linux 9   dnf       py 3.9
#   nomeshops-alpine        Fly.io/edge/VPS    Alpine 3.20       apk       py 3.12
#
# Usage: ./scripts/local_targets.sh          # start all six
#        ./scripts/local_targets.sh --stop   # remove all six
set -euo pipefail
NAMES="nomeshops-ubuntu22 nomeshops-al2023 nomeshops-gcp-debian nomeshops-azure-ubuntu nomeshops-rocky nomeshops-alpine"
if [ "${1:-}" = "--stop" ]; then
  docker rm -f $NAMES 2>/dev/null || true; echo "removed"; exit 0
fi
start() { docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null | grep -q true && echo "$1 already running" || \
          { docker rm -f "$1" >/dev/null 2>&1 || true; docker run -d --name "$1" --hostname "$1" "$2" sleep infinity >/dev/null && echo "started $1 ($2)"; }; }
start nomeshops-ubuntu22     ubuntu:22.04
start nomeshops-al2023       amazonlinux:2023
start nomeshops-gcp-debian   debian:12
start nomeshops-azure-ubuntu ubuntu:24.04
start nomeshops-rocky        rockylinux:9
start nomeshops-alpine       alpine:3.20
echo
echo "Now run with local backends, e.g.:"
echo "  EXECUTOR=docker KB_BACKEND=local STORE_BACKEND=local LLM_BACKEND=none \\"
echo "    python -m cli.demo deploy --repo https://github.com/SibgathKhan777/nomeshops-sample.git --instance nomeshops-gcp-debian"
