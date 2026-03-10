const fs = require('fs');

const { test, expect } = require('@playwright/test');

const { bootstrapAndCompleteSetup } = require('./helpers/gui');
const { paths, readJson } = require('./helpers/runtime');

test.describe.configure({ mode: 'serial' });

test('invalid repository URLs fail with a clear MCP error', async ({ page, request }) => {
  await bootstrapAndCompleteSetup(page, request);

  await page.goto('/mcp');
  await page.getByTestId('mcp-source').fill('https://example.com/not-github');
  await page.getByTestId('mcp-inspect').click();
  await expect(page.getByTestId('page-error')).toContainText('Only direct GitHub repository URLs are supported right now.');
});

test('duplicate MCP install reuses the same record and remove cleans config plus checkout', async ({ page, request }) => {
  await bootstrapAndCompleteSetup(page, request);

  await page.goto('/mcp');
  await page.getByTestId('mcp-source').fill('https://github.com/example/echo-mcp');
  await page.getByTestId('mcp-install').click();
  await expect(page.getByTestId('flash-message')).toContainText('installed');

  const firstState = readJson(paths.statePath);
  const installDir = firstState.mcp_registry.echo.install_dir;
  expect(fs.existsSync(installDir)).toBe(true);

  await page.goto('/mcp');
  await page.getByTestId('mcp-source').fill('https://github.com/example/echo-mcp');
  await page.getByTestId('mcp-install').click();
  await expect(page.getByTestId('mcp-remove-form-echo')).toHaveCount(1);

  const secondState = readJson(paths.statePath);
  expect(secondState.mcp_registry.echo.install_dir).toBe(installDir);
  expect(fs.existsSync(installDir)).toBe(true);

  await page.getByTestId('mcp-remove-echo').click();
  await expect(page.getByTestId('flash-message')).toContainText("MCP server 'echo' removed.");
  await expect(page.getByTestId('mcp-remove-form-echo')).toHaveCount(0);

  const config = readJson(paths.configPath);
  const state = readJson(paths.statePath);
  expect(config.tools.mcpServers.echo).toBeUndefined();
  expect(state.mcp_registry.echo).toBeUndefined();
  expect(fs.existsSync(installDir)).toBe(false);
});

test('MCPs with missing secrets cannot be enabled until the configuration is fixed and retested', async ({ page, request }) => {
  await bootstrapAndCompleteSetup(page, request);

  await page.goto('/mcp');
  await page.getByTestId('mcp-source').fill('https://github.com/example/secret-mcp');
  await page.getByTestId('mcp-install').click();
  await expect(page.getByTestId('flash-message')).toContainText('needs configuration');
  await expect(page).toHaveURL(/\/mcp\/secret/);
  await expect(page.getByTestId('mcp-detail-cards')).toContainText('Needs configuration');

  await page.getByTestId('mcp-detail-toggle').click();
  await expect(page.getByTestId('flash-message')).toContainText('must pass a test before it can be enabled');

  await expect(page.getByTestId('mcp-env-FAKE_API_KEY')).toBeVisible();
  await page.getByTestId('mcp-env-FAKE_API_KEY').fill('super-secret-value');
  await page.getByTestId('mcp-detail-save').click();
  await expect(page.getByTestId('flash-message')).toContainText("saved. Run the test before enabling it.");

  await page.getByTestId('mcp-detail-test').click();
  await expect(page.getByTestId('flash-message')).toContainText('is active with 2 tool(s)');
  await expect(page.getByTestId('mcp-detail-cards')).toContainText('fetch_secret, echo_message');

  await page.getByTestId('mcp-detail-toggle').click();
  await expect(page.getByTestId('flash-message')).toContainText('enabled for the main chat runtime');

  await page.goto('/chat');
  await expect(page.getByTestId('chat-active-mcp-servers-card')).toContainText('secret');
  await expect(page.getByTestId('chat-active-mcp-tools-card')).toContainText('fetch_secret');
});

test('failed MCP probes render a readable error card with the next action', async ({ page, request }) => {
  await bootstrapAndCompleteSetup(page, request);

  await page.goto('/mcp');
  await page.getByTestId('mcp-source').fill('https://github.com/example/failing-mcp');
  await page.getByTestId('mcp-install').click();
  await expect(page.getByTestId('flash-message')).toContainText('runtime probe failed');
  await expect(page).toHaveURL(/\/mcp\/failing/);
  await expect(page.getByTestId('mcp-error-card')).toContainText('Action failed');
  await expect(page.getByTestId('mcp-error-card')).toContainText('Fixture MCP startup failed: simulated crash');
  await expect(page.getByTestId('mcp-error-card')).toContainText('Open Logs');
});
