# GUI Testing

This repository has two main testing layers:

1. Python-side route and persistence tests
2. Playwright browser E2E tests

The goal is to keep the WebGUI safe to refactor without breaking onboarding, MCP flows, memory editing, chat, or the Community integration.

## 1. What We Test

### Python test suite

The Python suite covers:

- auth and onboarding route behavior
- config persistence
- profile and settings persistence
- GUI-side MCP install, test, enable, disable, and registry behavior
- Community detail and import routes
- update banner logic

Run it locally:

```bash
python3 -m pytest tests -q
```

### Playwright E2E

The browser suite covers:

- admin bootstrap and login
- provider, channel, and agent setup
- dashboard actions
- chat runtime behavior
- MCP failure paths
- profile updates
- mobile regression checks
- a11y smoke coverage

Run the critical suite:

```bash
npm run test:e2e:critical
```

Run the full suite:

```bash
npm run test:e2e:full
```

Run the accessibility smoke checks:

```bash
npm run test:e2e:a11y
```

## 2. Docker-Friendly Test Path

If your local machine does not already have the required browser and system dependencies, use:

```bash
./scripts/e2e/run_playwright_in_docker.sh
```

This is the easiest way to reproduce CI-like Playwright runs locally.

## 3. Isolated E2E Runtime

Playwright starts a dedicated isolated GUI instance through:

```bash
python3 scripts/e2e/run_gui_e2e_server.py
```

That instance writes only into:

- `tmp/e2e/gui-runtime/`
- `tmp/e2e/workspace/`

It does not touch your real `~/.nanobot` installation.

## 4. Live MCP Canary

The repository also has a live canary path for real MCP repositories.

Run it locally:

```bash
python3 scripts/e2e/run_live_mcp_canary.py
```

Or via wrapper:

```bash
./scripts/e2e/run_real_mcp_smoke.sh
```

Useful variants:

```bash
./scripts/e2e/run_real_mcp_smoke.sh --list-cases
./scripts/e2e/run_real_mcp_smoke.sh --case chrome-devtools --case context7
FIRECRAWL_API_KEY=... GITHUB_MCP_PAT=... ./scripts/e2e/run_real_mcp_smoke.sh
```

## 5. GitHub Actions Workflows

The repository ships with two GUI-related workflows:

### `.github/workflows/gui-e2e.yml`

This workflow runs:

- Python 3.12
- Node 20
- `python -m compileall nanobot_webgui tests`
- `python -m pytest tests -q`
- `python -m build`
- Playwright E2E

Behavior:

- `pull_request`: critical Chromium suite
- `push` to `main`: full suite on Chromium and Firefox
- nightly schedule: full suite on Chromium and Firefox
- `workflow_dispatch`: choose `critical` or `full`

### `.github/workflows/gui-live-canary.yml`

This workflow runs the live MCP canary only on:

- nightly schedule
- manual dispatch

That keeps live network dependencies out of normal pull requests.

## 6. Stable Selectors

Important GUI elements should expose stable `data-testid` attributes.

When you touch:

- onboarding forms
- dashboard cards
- MCP actions
- chat controls
- memory controls

prefer extending the current `data-testid` pattern instead of switching tests back to text-based selectors.

## 7. Recommended Local Verification Before Release

Use this exact order:

```bash
python3 -m compileall nanobot_webgui
python3 -m pytest tests -q
python3 -m build
npm run test:e2e:critical
```

If you changed mobile layouts or MCP flows, also run:

```bash
npm run test:e2e:full
```

## 8. Artifacts

Generated during runs:

- Playwright artifacts: `test-results/artifacts/`
- page screenshots: `test-results/pages/`
- HTML report: `playwright-report/`
- live canary report: `test-results/live-mcp-canary.json`

Project screenshots used in the README and release notes live under:

- `output/gui-screenshots/desktop/`
- `output/gui-screenshots/mobile/`
