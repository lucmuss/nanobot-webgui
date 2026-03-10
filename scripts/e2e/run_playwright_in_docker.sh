#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
IMAGE="${NANOBOT_E2E_IMAGE:-ai-stack-nanobot-dev}"
PLAYWRIGHT_ARGS=("$@")
QUOTED_ARGS=""
BROWSER_TARGETS="$(echo "${NANOBOT_GUI_E2E_BROWSERS:-chromium}" | tr ',' ' ')"
INSTALL_FLAGS=""

if [[ ${#PLAYWRIGHT_ARGS[@]} -gt 0 ]]; then
  QUOTED_ARGS="$(printf '%q ' "${PLAYWRIGHT_ARGS[@]}")"
fi

if [[ "${NANOBOT_GUI_PLAYWRIGHT_WITH_DEPS:-1}" == "1" ]]; then
  INSTALL_FLAGS="--with-deps"
fi

docker run --rm \
  --entrypoint bash \
  -e PLAYWRIGHT_BROWSERS_PATH=/app/.playwright-browsers \
  -e NANOBOT_GUI_E2E_BROWSERS="${NANOBOT_GUI_E2E_BROWSERS:-chromium}" \
  -v "${REPO_ROOT}:/app" \
  -w /app \
  "${IMAGE}" \
  -lc "npm install >/dev/null 2>&1 && npx playwright install ${INSTALL_FLAGS} ${BROWSER_TARGETS} >/dev/null 2>&1 && npx playwright test ${QUOTED_ARGS}"
