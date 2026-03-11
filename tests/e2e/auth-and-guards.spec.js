const { test, expect } = require('@playwright/test');

const { resetE2E, bootstrapAdmin, login, bootstrapAndCompleteSetup } = require('./helpers/gui');

test.describe.configure({ mode: 'serial' });

test('first-run guard blocks a second admin setup flow', async ({ page, request }) => {
  await resetE2E(request);
  await page.goto('/');
  await expect(page).toHaveURL(/\/setup\/admin/);

  await bootstrapAdmin(page);
  await page.goto('/setup/admin');
  await expect(page).toHaveURL(/\/setup\/provider/);

  await page.goto('/setup/provider');
  await page.getByTestId('topbar-logout').click();
  await expect(page).toHaveURL(/\/login/);

  await page.goto('/setup/admin');
  await expect(page).toHaveURL(/\/login/);
  await expect(page.getByTestId('login-submit')).toBeVisible();
});

test('protected routes redirect anonymous users to login after logout', async ({ page, request }) => {
  await bootstrapAndCompleteSetup(page, request);
  await page.getByTestId('topbar-logout').click();
  await expect(page).toHaveURL(/\/login/);

  for (const route of ['/dashboard', '/mcp', '/memory', '/settings', '/profile']) {
    await page.goto(route);
    await expect(page).toHaveURL(/\/login/);
  }
});

test('logout clears the session and relogin restores dashboard access', async ({ page, request }) => {
  await bootstrapAndCompleteSetup(page, request);
  await expect(page).toHaveURL(/\/dashboard/);

  await page.getByTestId('topbar-logout').click();
  await expect(page).toHaveURL(/\/login/);

  await page.goto('/dashboard');
  await expect(page).toHaveURL(/\/login/);

  await login(page);
  await expect(page).toHaveURL(/\/dashboard/);
  await expect(page.getByTestId('dashboard-system-info')).toBeVisible();
});

test('setup validation failure shows fix guidance for invalid provider, missing model, and missing API key', async ({ page, request }) => {
  await bootstrapAndCompleteSetup(page, request);

  await page.goto('/setup/provider');
  await page.getByTestId('provider-form').evaluate((form) => {
    const provider = form.querySelector('[data-testid="provider-select"]');
    const option = document.createElement('option');
    option.value = 'not-a-real-provider';
    option.textContent = 'not-a-real-provider';
    provider.appendChild(option);
    provider.value = 'not-a-real-provider';
    form.requestSubmit(form.querySelector('[data-testid="provider-save"]'));
  });
  await expect(page.getByTestId('flash-message')).toContainText('Choose a valid provider.');

  await page.goto('/setup/provider');
  await page.getByTestId('provider-form').evaluate((form) => {
    const provider = form.querySelector('[data-testid="provider-select"]');
    provider.value = 'openrouter';
    const model = form.querySelector('[data-testid="provider-model"]');
    model.removeAttribute('required');
    model.value = '';
    const apiKey = form.querySelector('[data-testid="provider-api-key"]');
    apiKey.value = '';
    form.requestSubmit(form.querySelector('[data-testid="provider-save"]'));
  });
  await expect(page).toHaveURL(/\/setup\/provider/);
  await expect(page.getByTestId('page-error')).toContainText(/model|required/i);

  await page.goto('/setup/provider');
  await page.getByTestId('provider-api-key').fill('');
  await page.getByTestId('provider-save').click();
  await expect(page.getByTestId('flash-message')).toContainText('Provider settings saved.');

  await page.goto('/settings');
  await page.getByTestId('settings-validate').click();
  await expect(page.getByTestId('validation-next-fix-action')).toBeVisible();
  await expect(page.getByTestId('validation-card-provider-credentials')).toBeVisible();
  await expect(page.getByTestId('validation-card-agent-runtime')).toContainText('Missing Authentication header');
  await expect(page.getByTestId('validation-action-provider-credentials')).toBeVisible();
});

test('direct deep links redirect back to provider setup while onboarding is incomplete', async ({ page, request }) => {
  await resetE2E(request);
  await bootstrapAdmin(page);

  await page.goto('/dashboard');
  await expect(page).toHaveURL(/\/setup\/provider/);

  await page.goto('/mcp');
  await expect(page).toHaveURL(/\/setup\/provider/);

  await page.goto('/chat');
  await expect(page).toHaveURL(/\/setup\/provider/);
});
