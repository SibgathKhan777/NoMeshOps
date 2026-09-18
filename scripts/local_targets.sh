#!/usr/bin/env bash
# Local mode: start two Docker containers that play the role of EC2 targets (no AWS account needed).
#   nomeshops-ubuntu22  ubuntu:22.04        (no python, no git -> deterministic rules bootstrap it)
#   nomeshops-al2023    amazonlinux:2023    (python 3.9, no git, no pip)
# Usage: ./scripts/local_targets.sh          # start
#        ./scripts/local_targets.sh --stop   # remove
set -euo pipefail
if [ "${1:-}" = "--stop" ]; then
  docker rm -f nomeshops-ubuntu22 nomeshops-al2023 2>/dev/null || true; echo "removed"; exit 0
fi
start() { docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null | grep -q true && echo "$1 already running" || \
          { docker rm -f "$1" >/dev/null 2>&1 || true; docker run -d --name "$1" --hostname "$1" "$2" sleep infinity >/dev/null && echo "started $1 ($2)"; }; }
start nomeshops-ubuntu22 ubuntu:22.04
start nomeshops-al2023 amazonlinux:2023
echo
echo "Now run with local backends, e.g.:"
echo "  EXECUTOR=docker KB_BACKEND=local STORE_BACKEND=local LLM_BACKEND=none \\"
echo "    python -m cli.demo deploy --repo https://github.com/SibgathKhan777/nomeshops-sample.git --instance nomeshops-ubuntu22"
