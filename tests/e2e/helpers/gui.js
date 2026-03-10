const { expect } = require('@playwright/test');

const admin = {
  username: 'gui-admin',
  email: 'gui-admin@example.com',
  password: 'NanobotGuiTest!123',
};

async function resetE2E(request) {
  const response = await request.post('/__e2e/reset');
  expect(response.ok()).toBeTruthy();
}

async function bootstrapAdmin(page, overrides = {}) {
  const creds = { ...admin, ...overrides };
  await page.goto('/setup/admin');
  await page.getByTestId('admin-setup-username').fill(creds.username);
  await page.getByTestId('admin-setup-email').fill(creds.email);
  await page.getByTestId('admin-setup-password').fill(creds.password);
  await page.getByTestId('admin-setup-password-confirm').fill(creds.password);
  await page.getByTestId('admin-setup-submit').click();
  await expect(page).toHaveURL(/\/setup\/provider/);
  return creds;
}

async function login(page, overrides = {}) {
  const creds = { ...admin, ...overrides };
  await page.goto('/login');
  await page.getByTestId('login-identifier').fill(creds.username);
  await page.getByTestId('login-password').fill(creds.password);
  await page.getByTestId('login-submit').click();
  return creds;
}

async function completeSetup(
  page,
  {
    provider = 'openrouter',
    model = 'openai/gpt-4.1-mini',
    apiKey = 'e2e-openrouter-key',
    channel = 'telegram',
    token = '123456:ABCDEF',
    allowFrom = 'owner-1',
    finish = true,
  } = {},
) {
  await page.goto('/setup/provider');
  await page.getByTestId('provider-select').selectOption(provider);
  await page.getByTestId('provider-model').fill(model);
  await page.getByTestId('provider-api-key').fill(apiKey);
  await page.getByTestId('provider-continue').click();
  await expect(page).toHaveURL(/\/setup\/channel/);

  await page.getByTestId('channel-select').selectOption(channel);
  await page.getByTestId('channel-field-token').fill(token);
  await page.getByTestId('channel-field-allow_from').fill(allowFrom);
  await page.getByTestId('channel-continue').click();
  await expect(page).toHaveURL(/\/setup\/agent/);

  await page.getByTestId('agent-model').fill(model);
  await page.getByTestId('agent-provider').fill(provider);
  await page.getByTestId('agent-instruction').fill(
    '# E2E Instructions\n- Keep responses deterministic.\n- Mention the test harness when relevant.',
  );
  if (finish) {
    await page.getByTestId('agent-finish').click();
    await expect(page).toHaveURL(/\/dashboard/);
  } else {
    await page.getByTestId('agent-save').click();
    await expect(page).toHaveURL(/\/setup\/agent/);
  }
}

async function bootstrapAndCompleteSetup(page, request, options = {}) {
  await resetE2E(request);
  await bootstrapAdmin(page, options.admin);
  await completeSetup(page, options);
}

module.exports = {
  admin,
  resetE2E,
  bootstrapAdmin,
  login,
  completeSetup,
  bootstrapAndCompleteSetup,
};
