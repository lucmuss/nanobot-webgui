const path = require('path');

const { test, expect } = require('@playwright/test');

const { bootstrapAdmin, resetE2E } = require('./helpers/gui');
const { paths, readText } = require('./helpers/runtime');

test.describe.configure({ mode: 'serial' });

test('save buttons stay on the same wizard step and refresh keeps the saved values', async ({ page, request }) => {
  await resetE2E(request);
  await bootstrapAdmin(page);

  await page.getByTestId('provider-select').selectOption('openrouter');
  await page.getByTestId('provider-model').fill('openai/gpt-4.1-mini');
  await page.getByTestId('provider-api-key').fill('wizard-save-key');
  await page.getByTestId('provider-save').click();
  await expect(page).toHaveURL(/\/setup\/provider/);
  await page.reload();
  await expect(page.getByTestId('provider-model')).toHaveValue('openai/gpt-4.1-mini');
  await expect(page.getByTestId('provider-api-key')).toHaveValue('wizard-save-key');

  await page.getByTestId('provider-continue').click();
  await expect(page).toHaveURL(/\/setup\/channel/);
  await page.getByTestId('channel-select').selectOption('telegram');
  await page.getByTestId('channel-field-token').fill('777:save-flow');
  await page.getByTestId('channel-field-allow_from').fill('wizard-owner');
  await page.getByTestId('channel-save').click();
  await expect(page).toHaveURL(/\/setup\/channel/);
  await page.reload();
  await expect(page.getByTestId('channel-field-token')).toHaveValue('777:save-flow');

  await page.getByTestId('channel-continue').click();
  await expect(page).toHaveURL(/\/setup\/agent/);
  await page.getByTestId('agent-instruction').fill('# Wizard Save\n- persisted');
  await page.getByTestId('agent-save').click();
  await expect(page).toHaveURL(/\/setup\/agent/);
  await page.reload();
  await expect(page.getByTestId('agent-instruction')).toContainText('Wizard Save');
});

test('memory switching, reset to template, and safe mode toggling keep the UI predictable', async ({ page, request }) => {
  await resetE2E(request);
  await bootstrapAdmin(page);

  await page.goto('/memory?doc=soul');
  await page.getByTestId('memory-content').fill('# Soul\n- custom soul marker');
  await page.getByTestId('memory-save').click();
  await expect(page.getByTestId('flash-message')).toContainText('saved');
  expect(readText(path.join(paths.workspaceDir, 'SOUL.md'))).toContain('custom soul marker');

  await page.goto('/memory?doc=tools');
  await page.getByTestId('memory-content').fill('# Tools\n- custom tools marker');
  await page.getByTestId('memory-save').click();
  await expect(readText(path.join(paths.workspaceDir, 'TOOLS.md'))).toContain('custom tools marker');

  await page.goto('/memory?doc=soul');
  await page.getByTestId('memory-reset').click();
  await expect(page.getByTestId('flash-message')).toContainText('reset to the bundled template');
  expect(readText(path.join(paths.workspaceDir, 'SOUL.md'))).not.toContain('custom soul marker');

  await page.goto('/setup/provider');
  await expect(page.getByTestId('provider-advanced')).not.toHaveAttribute('open', '');
  await page.getByTestId('topbar-safe-mode').click();
  await expect(page.getByTestId('flash-message')).toContainText('Safe Mode disabled.');
  await page.goto('/setup/provider');
  await expect(page.getByTestId('provider-advanced')).toHaveAttribute('open', '');
  await page.getByTestId('topbar-safe-mode').click();
  await expect(page.getByTestId('flash-message')).toContainText('Safe Mode enabled.');
});

test('mobile viewport keeps dashboard, chat, and setup pages usable', async ({ page, request }) => {
  await resetE2E(request);
  await page.setViewportSize({ width: 390, height: 844 });
  await bootstrapAdmin(page);

  await page.goto('/setup/provider');
  await expect(page.getByRole('heading', { name: 'Provider', exact: true })).toBeVisible();
  await expect(page.getByText('nanobot-e2e')).toBeVisible();
  await expect(page.getByTestId('provider-model')).toBeVisible();
  await expect(page.getByTestId('topbar-mobile-toolbar')).toBeVisible();
  await expect(page.getByTestId('topbar-desktop-toolbar')).toBeHidden();

  await page.goto('/dashboard');
  await expect(page).toHaveURL(/\/setup\/provider/);

  await page.evaluate(() => {
    const form = document.querySelector('[data-testid="provider-form"]');
    document.querySelector('[data-testid="provider-select"]').value = 'openrouter';
    document.querySelector('[data-testid="provider-model"]').value = 'openai/gpt-4.1-mini';
    document.querySelector('[data-testid="provider-api-key"]').value = 'mobile-key';
    const action = document.createElement('input');
    action.type = 'hidden';
    action.name = 'action';
    action.value = 'next';
    form.appendChild(action);
    form.submit();
  });
  await expect(page.getByTestId('topbar-mobile-toolbar')).toBeVisible();
  await page.locator('.topbar-mobile-system summary').click();
  await expect(page.getByRole('button', { name: /Turn Safe Mode/i })).toBeVisible();
  await page.goto('/chat');
  await expect(page).toHaveURL(/\/setup\/provider/);
});
