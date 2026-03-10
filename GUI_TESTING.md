# GUI E2E Testing

This project includes a Playwright workflow for automated browser testing of the WebGUI.
It also includes Python-side backend integration tests for the GUI services and route layer.

## What It Covers

The E2E suite is focused on the parts that are easy to break and important for real use:

- admin bootstrap and login
- provider, channel, and agent wizard flow
- `config.json` persistence checks after GUI changes
- `AGENTS.md` and `MEMORY.md` persistence checks after editor changes
- profile updates, including avatar upload
- authenticated page crawl with screenshots and an interactive-element report

In practice that means the suite verifies:

- one-time admin creation and later login reuse
- changes written back into `tmp/e2e/gui-runtime/config.json`
- markdown editor changes written into the isolated workspace files
- profile identity changes and avatar uploads written into `gui.sqlite3` and `media/avatars`

The backend integration layer verifies:

- route-driven persistence without a real browser
- profile, settings, and auth data written into the isolated GUI database
- MCP install, test, enable, and registry persistence using local fixture MCP servers

## Local MCP Fixtures

Stable MCP regression coverage is built on local fixtures under:

- `tests/fixtures/mcp/echo-mcp`
- `tests/fixtures/mcp/secret-mcp`
- `tests/fixtures/mcp/failing-mcp`
- `tests/fixtures/mcp/manifest-npm`
- `tests/fixtures/mcp/workspace-playwright`
- `tests/fixtures/mcp/remote-github`

These fixtures let the GUI tests verify MCP install, test, enable, remove, and error handling without depending on live third-party repositories.

## Stable Selectors

The GUI now exposes stable `data-testid` attributes on the important interactive elements.

That means the Playwright suite is intentionally not coupled to:

- visible button labels
- translated copy
- small layout changes
- section reshuffles inside the existing pages

When you add or refactor important controls, prefer extending the existing `data-testid` pattern instead of switching tests back to text selectors.

## Files

- [`playwright.config.js`](./playwright.config.js)
- [`scripts/e2e/run_gui_e2e_server.py`](./scripts/e2e/run_gui_e2e_server.py)
- [`scripts/e2e/run_live_mcp_canary.py`](./scripts/e2e/run_live_mcp_canary.py)
- [`scripts/e2e/run_playwright_in_docker.sh`](./scripts/e2e/run_playwright_in_docker.sh)
- [`tests/e2e/gui-workflows.spec.js`](./tests/e2e/gui-workflows.spec.js)
- [`tests/test_gui_backend_integration.py`](./tests/test_gui_backend_integration.py)
- [`tests/test_gui_mcp_service.py`](./tests/test_gui_mcp_service.py)

## Install

Recommended when your host already has Python 3.11+, Node 20+, and browser dependencies:

Python side:

```bash
pip install -e .[dev]
```

Node side:

```bash
npm install
npm run test:e2e:install
```

Recommended when your host does not have the full GUI toolchain:

```bash
./scripts/e2e/run_playwright_in_docker.sh
```

## Run

Headless:

```bash
npm run test:e2e
```

Critical smoke / PR subset:

```bash
npm run test:e2e:critical
```

Full functional suite:

```bash
npm run test:e2e:full
```

Accessibility smoke:

```bash
npm run test:e2e:a11y
```

Backend integration tests:

```bash
python3 -m pytest tests/test_gui_backend_integration.py tests/test_gui_mcp_service.py -q
```

Live MCP canary:

```bash
python3 scripts/e2e/run_live_mcp_canary.py
```

Convenience wrapper for the official MCP smoke set:

```bash
./scripts/e2e/run_real_mcp_smoke.sh
```

The wrapper prefers the local Python environment and automatically falls back to the existing dev Docker image (`ai-stack-nanobot-dev`) when local repo dependencies are not installed.

List or filter official cases:

```bash
./scripts/e2e/run_real_mcp_smoke.sh --list-cases
./scripts/e2e/run_real_mcp_smoke.sh --case chrome-devtools --case context7
```

Authenticated retests use environment variables automatically:

```bash
FIRECRAWL_API_KEY=... GITHUB_MCP_PAT=... ./scripts/e2e/run_real_mcp_smoke.sh
```

Headless via Docker helper:

```bash
./scripts/e2e/run_playwright_in_docker.sh
```

Headed:

```bash
npm run test:e2e:headed
```

Pass normal Playwright flags through Docker:

```bash
./scripts/e2e/run_playwright_in_docker.sh -g "profile updates"
./scripts/e2e/run_playwright_in_docker.sh --headed
```

Use a different browser matrix locally:

```bash
NANOBOT_GUI_E2E_BROWSERS=chromium,firefox ./scripts/e2e/run_playwright_in_docker.sh
```

The Docker helper installs Playwright browser dependencies by default. If your local image already has the needed system libraries and you want faster reruns, you can opt out explicitly:

```bash
NANOBOT_GUI_PLAYWRIGHT_WITH_DEPS=0 ./scripts/e2e/run_playwright_in_docker.sh
```

HTML report:

```bash
npm run test:e2e:report
```

## CI Strategy

The repository runs functional GUI E2E in GitHub Actions with a split that matches active UI work:

- `pull_request`: critical smoke / regression suite
- `push` to `main`: full functional E2E suite across Chromium and Firefox
- nightly schedule: full functional E2E suite across Chromium and Firefox
- `workflow_dispatch`: manual choice between `critical` and `full`

Each functional GUI workflow also runs:

- `tests/test_gui_backend_integration.py`
- `tests/test_gui_mcp_service.py`

Separately, a live canary workflow runs only on nightly schedule and manual dispatch. That job checks real official MCP repositories without putting live network dependencies into every pull request.

This is intentionally not a visual-regression pipeline. Visual comparisons should be introduced only after the GUI layout is more stable.

## How the Isolated Test Instance Works

The Playwright config starts a dedicated GUI test instance automatically through:

```bash
python3 scripts/e2e/run_gui_e2e_server.py
```

That script creates a clean isolated instance under:

- `tmp/e2e/gui-runtime/`
- `tmp/e2e/workspace/`

Nothing in your normal `~/.nanobot` instance is touched.

## Artifacts

Generated during runs:

- screenshots: `test-results/pages/`
- Playwright artifacts: `test-results/artifacts/`
- discovery report: `test-results/gui-discovery-report.json`
- live canary report: `test-results/live-mcp-canary.json`
- HTML report: `playwright-report/`

## Optional: Target an Existing GUI Instead

If you already have a GUI instance running and want Playwright to hit that instead:

```bash
NANOBOT_GUI_BASE_URL=http://127.0.0.1:18791 npm run test:e2e
```

In that mode Playwright does not start the isolated local server.
