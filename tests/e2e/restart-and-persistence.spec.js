const { test, expect } = require('@playwright/test');

const { bootstrapAndCompleteSetup } = require('./helpers/gui');
const { paths, readAdminUsers, readJson, readText } = require('./helpers/runtime');

test.describe.configure({ mode: 'serial' });

test('restart keeps config, profile, memory, and installed MCP state intact', async ({ page, request }) => {
  await bootstrapAndCompleteSetup(page, request);

  await page.goto('/settings');
  await page.getByTestId('settings-exec-timeout').fill('75');
  await page.getByTestId('settings-path-append').fill('/usr/local/bin/e2e');
  await page.getByTestId('settings-save').click();
  await expect(page.getByTestId('flash-message')).toContainText('Settings saved.');

  await page.goto('/memory?doc=memory');
  await page.getByTestId('memory-content').fill('# Restart Memory\n- persistence-check');
  await page.getByTestId('memory-save').click();
  await expect(page.getByTestId('flash-message')).toContainText('saved');

  await page.goto('/profile');
  await page.getByTestId('profile-display-name').fill('Restart Persistence Admin');
  await page.getByTestId('profile-save').click();
  await expect(page.getByTestId('flash-message')).toContainText('Profile updated.');

  await page.goto('/mcp');
  await page.getByTestId('mcp-source').fill('https://github.com/example/echo-mcp');
  await page.getByTestId('mcp-install').click();
  await expect(page.getByTestId('flash-message')).toContainText('installed');

  const beforeConfig = readJson(paths.configPath);
  const beforeState = readJson(paths.statePath);
  const beforeUsers = readAdminUsers();
  const beforeMemory = readText(paths.memoryPath);

  await page.getByTestId('topbar-restart').click();
  await expect(page.locator('h1')).toContainText('Restart');

  await page.goto('/dashboard');
  await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible();

  const afterConfig = readJson(paths.configPath);
  const afterState = readJson(paths.statePath);
  const afterUsers = readAdminUsers();
  const afterMemory = readText(paths.memoryPath);

  expect(afterConfig.tools.exec.timeout).toBe(beforeConfig.tools.exec.timeout);
  expect(afterConfig.tools.exec.pathAppend).toBe(beforeConfig.tools.exec.pathAppend);
  expect(afterConfig.tools.mcpServers.echo.command).toBe(beforeConfig.tools.mcpServers.echo.command);
  expect(afterUsers[0].display_name).toBe(beforeUsers[0].display_name);
  expect(afterMemory).toBe(beforeMemory);
  expect(afterState.mcp_registry.echo.install_dir).toBe(beforeState.mcp_registry.echo.install_dir);
  await expect(page.getByTestId('dashboard-mcp-card-echo')).toContainText('echo');
});
