const { test, expect } = require('@playwright/test');

const { resetE2E, bootstrapAdmin } = require('./helpers/gui');
const { paths, readAdminUsers, readJson, readText } = require('./helpers/runtime');

test.describe.configure({ mode: 'serial' });

test('save actions persist immediately on provider, channel, agent, settings, and profile pages', async ({ page, request }) => {
  await resetE2E(request);
  await bootstrapAdmin(page);

  await page.getByTestId('provider-select').selectOption('openrouter');
  await page.getByTestId('provider-model').fill('openai/gpt-4.1-mini');
  await page.getByTestId('provider-api-key').fill('per-page-openrouter-key');
  await page.getByTestId('provider-save').click();
  await expect(page).toHaveURL(/\/setup\/provider/);
  await expect(page.getByTestId('flash-message')).toContainText('Provider settings saved.');

  let config = readJson(paths.configPath);
  expect(config.agents.defaults.provider).toBe('openrouter');
  expect(config.agents.defaults.model).toBe('openai/gpt-4.1-mini');
  expect(config.providers.openrouter.apiKey).toBe('per-page-openrouter-key');

  await page.goto('/setup/channel');
  await page.getByTestId('channel-select').selectOption('telegram');
  await page.getByTestId('channel-field-token').fill('654321:ZYX');
  await page.getByTestId('channel-field-allow_from').fill('owner-a, owner-b');
  await page.getByTestId('channel-send-progress').check();
  await page.getByTestId('channel-save').click();
  await expect(page).toHaveURL(/\/setup\/channel/);
  await expect(page.getByTestId('flash-message')).toContainText('Channel settings saved.');

  config = readJson(paths.configPath);
  expect(config.channels.telegram.enabled).toBe(true);
  expect(config.channels.telegram.token).toBe('654321:ZYX');
  expect(config.channels.telegram.allowFrom).toEqual(['owner-a', 'owner-b']);
  expect(config.channels.sendProgress).toBe(true);

  await page.goto('/setup/agent');
  await page.getByTestId('agent-model').fill('openai/gpt-4.1-mini');
  await page.getByTestId('agent-provider').fill('openrouter');
  await page.getByTestId('agent-response-style').selectOption('brief');
  await page.getByTestId('agent-instruction').fill('# Per-page persistence\n- Saved from the Agent page.');
  await page.getByTestId('agent-save').click();
  await expect(page).toHaveURL(/\/setup\/agent/);
  await expect(page.getByTestId('flash-message')).toContainText('Agent settings saved.');

  config = readJson(paths.configPath);
  expect(config.agents.defaults.provider).toBe('openrouter');
  expect(config.agents.defaults.model).toBe('openai/gpt-4.1-mini');
  expect(readText(paths.agentsPath)).toContain('Per-page persistence');
  expect(readText(`${paths.workspaceDir}/USER.md`)).toContain('Brief and concise');

  await page.getByTestId('agent-finish').click();
  await expect(page).toHaveURL(/\/dashboard/);

  await page.goto('/settings');
  await page.getByTestId('settings-exec-timeout').fill('81');
  await page.getByTestId('settings-path-append').fill('/custom/e2e/bin');
  await page.getByTestId('settings-save').click();
  await expect(page.getByTestId('flash-message')).toContainText('Settings saved.');

  config = readJson(paths.configPath);
  expect(config.tools.exec.timeout).toBe(81);
  expect(config.tools.exec.pathAppend).toBe('/custom/e2e/bin');

  await page.goto('/profile');
  await page.getByTestId('profile-display-name').fill('Per Page Admin');
  await page.getByTestId('profile-save').click();
  await expect(page.getByTestId('flash-message')).toContainText('Profile updated.');

  const users = readAdminUsers();
  expect(users[0].display_name).toBe('Per Page Admin');
});
