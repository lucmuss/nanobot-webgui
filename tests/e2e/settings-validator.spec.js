const { test, expect } = require('@playwright/test');

const { bootstrapAdmin, bootstrapAndCompleteSetup, resetE2E } = require('./helpers/gui');
const { paths, readJson, writeJson } = require('./helpers/runtime');

test.describe.configure({ mode: 'serial' });

async function openValidatedSettings(page) {
  await page.goto('/settings');
  await page.getByTestId('settings-validate').click();
  await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible();
}

test('settings validator routes setup completion recovery to provider setup', async ({ page, request }) => {
  await resetE2E(request);
  await bootstrapAdmin(page);

  await openValidatedSettings(page);
  const setupCard = page.getByTestId('validation-card-setup-completion');
  await expect(setupCard).toContainText('Finish the wizard before using MCP automation.');
  await page.getByTestId('validation-action-setup-completion').click();
  await expect(page).toHaveURL(/\/setup\/provider/);
});

test('settings validator fix buttons route to the correct pages across provider, agent, settings, MCP, status, and workspace cards', async ({ page, request }) => {
  await bootstrapAndCompleteSetup(page, request);

  let config = readJson(paths.configPath);
  config.providers.openrouter.apiKey = '';
  writeJson(paths.configPath, config);

  await openValidatedSettings(page);
  const providerCard = page.getByTestId('validation-card-provider-credentials');
  await expect(providerCard).toContainText('missing credentials');
  await page.getByTestId('validation-action-provider-credentials').click();
  await expect(page).toHaveURL(/\/setup\/provider/);

  await openValidatedSettings(page);
  const agentCard = page.getByTestId('validation-card-agent-runtime');
  await expect(agentCard).toContainText('Missing Authentication header');
  await page.getByTestId('validation-action-agent-runtime').click();
  await expect(page).toHaveURL(/\/setup\/agent/);

  await openValidatedSettings(page);
  await page.getByTestId('validation-action-workspace-path').click();
  await expect(page).toHaveURL(/\/setup\/agent/);

  config = readJson(paths.configPath);
  config.providers.openrouter.apiKey = 'e2e-openrouter-key';
  config.tools.enabled = false;
  config.tools.exec.timeout = 0;
  writeJson(paths.configPath, config);

  await openValidatedSettings(page);
  const toolsCard = page.getByTestId('validation-card-tool-execution');
  await expect(toolsCard).toContainText('disabled');
  await page.getByTestId('validation-action-tool-execution').click();
  await expect(page).toHaveURL(/\/settings/);

  config = readJson(paths.configPath);
  config.tools.enabled = true;
  config.tools.exec.timeout = 60;
  writeJson(paths.configPath, config);

  await page.goto('/mcp');
  await page.getByTestId('mcp-source').fill('https://github.com/example/echo-mcp');
  await page.getByTestId('mcp-install').click();
  await expect(page.getByTestId('flash-message')).toContainText('installed');

  await openValidatedSettings(page);
  const mcpCard = page.getByTestId('validation-card-mcp-runtime');
  await expect(mcpCard).toContainText('Install and test MCP servers before enabling them for chat.');
  await page.getByTestId('validation-action-mcp-runtime').click();
  await expect(page).toHaveURL(/\/mcp/);

  await openValidatedSettings(page);
  await page.getByTestId('validation-action-gateway-health').click();
  await expect(page).toHaveURL(/\/status/);
});
