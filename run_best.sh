#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
OFFLINE=()
if [[ "${1:-}" == "--offline" ]]; then
  OFFLINE=(--offline)
fi

echo
echo "===== LOTTO 6aus49 ====="
"$PYTHON" lotto.py "${OFFLINE[@]}" best --runs 1000 --top 5

echo
echo "===== EUROJACKPOT 5/50 + 2/12 ====="
"$PYTHON" eurojackpot.py "${OFFLINE[@]}" best --runs 1000 --top 5
