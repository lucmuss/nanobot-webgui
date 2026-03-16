#!/bin/bash
# Count the installed upstream nanobot core lines (excluding channels/, cli/, providers/ adapters)
set -euo pipefail
cd "$(dirname "$0")" || exit 1

root=$(python3 - <<'PY'
import inspect
from pathlib import Path
import nanobot

print(Path(inspect.getfile(nanobot)).resolve().parent)
PY
)

echo "nanobot core agent line count"
echo "================================"
echo ""
echo "Using installed upstream source: $root"
echo ""

for dir in agent agent/tools bus config cron heartbeat session utils; do
  count=$(find "$root/$dir" -maxdepth 1 -name "*.py" -exec cat {} + | wc -l)
  printf "  %-16s %5s lines\n" "$dir/" "$count"
done

root_count=$(cat "$root/__init__.py" "$root/__main__.py" | wc -l)
printf "  %-16s %5s lines\n" "(root)" "$root_count"

echo ""
total=$(find "$root" -name "*.py" ! -path "*/channels/*" ! -path "*/cli/*" ! -path "*/providers/*" | xargs cat | wc -l)
echo "  Core total:     $total lines"
echo ""
echo "  (excludes: channels/, cli/, providers/)"
