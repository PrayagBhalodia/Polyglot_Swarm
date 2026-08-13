#!/usr/bin/env bash
#
# Local verification gate for Track A.
#
# This is the "exit 0 before you commit" check the swarm agent MUST pass. It
# runs, in order: byte-compilation, an optional type check (mypy, if present),
# and the full unittest suite. Any failure aborts with a non-zero exit code so
# broken code never reaches a commit.
#
# Usage:  ./scripts/verify.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT/src"

PY="${PYTHON:-python3}"

echo "==> [1/3] Byte-compiling all sources (compileall)"
"$PY" -m compileall -q src tests scripts

echo "==> [2/3] Type checking (mypy, optional)"
if command -v mypy >/dev/null 2>&1; then
    mypy --strict --ignore-missing-imports src
else
    echo "    mypy not installed; skipping (install it for strict type checks)"
fi

echo "==> [3/3] Running unit tests (unittest discover)"
"$PY" -m unittest discover -s tests -t . -v

echo ""
echo "==> VERIFICATION PASSED (exit 0) — safe to commit."
