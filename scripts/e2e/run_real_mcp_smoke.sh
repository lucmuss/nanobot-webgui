#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
IMAGE="${NANOBOT_E2E_IMAGE:-ai-stack-nanobot-dev}"

cd "${REPO_ROOT}"

if python3 - <<'PY' >/dev/null 2>&1
import pathlib
import sys

root = pathlib.Path.cwd()
sys.path.insert(0, str(root))

import nanobot  # noqa: F401
import pydantic_settings  # noqa: F401
PY
then
  python3 scripts/e2e/run_live_mcp_canary.py "$@"
  exit 0
fi

if command -v docker >/dev/null 2>&1 && docker image inspect "${IMAGE}" >/dev/null 2>&1; then
  docker run --rm \
    --entrypoint python3 \
    -e FIRECRAWL_API_KEY="${FIRECRAWL_API_KEY:-}" \
    -e GITHUB_MCP_PAT="${GITHUB_MCP_PAT:-}" \
    -v "${REPO_ROOT}:/app" \
    -w /app \
    "${IMAGE}" \
    scripts/e2e/run_live_mcp_canary.py "$@"
  exit 0
fi

cat >&2 <<EOF
Unable to run the real MCP smoke-test.

Neither of these execution paths is ready:
- local Python environment with repo dependencies installed
- Docker image '${IMAGE}'

Fix one of them and rerun:
- pip install -e .[dev]
- or build the dev image and rerun with NANOBOT_E2E_IMAGE if needed
EOF
exit 1
