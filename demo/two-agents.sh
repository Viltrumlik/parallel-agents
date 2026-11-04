#!/usr/bin/env bash
# Try it yourself: two agents share a throwaway repo.
#   bash demo/two-agents.sh
set -u
PA="python3 $(cd "$(dirname "$0")/.." && pwd)/skills/parallel-agents/scripts/pa.py"
REPO="$(mktemp -d)"
trap 'rm -rf "$REPO"' EXIT
cd "$REPO" && git init -q && mkdir -p src/billing tests/billing && touch src/billing/views.py

step() { printf '\n\033[1;34m[%s]\033[0m $ pa %s\n' "$1" "${*:2}"; $PA "${@:2}"; }

step claude claim src/billing/ --as claude-7f2c --why "add refund endpoint"
step codex  claim src/billing/views.py --as codex-a91e --why "fix currency rounding"
step codex  claim tests/billing/ --as codex-a91e --why "write billing tests"
step you    status
step claude note "refunds done, tests still missing" --as claude-7f2c
step claude release --as claude-7f2c
step codex  claim src/billing/views.py --as codex-a91e --why "fix currency rounding"
step codex  log -n 1
