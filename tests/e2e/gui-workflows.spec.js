const fs = require('fs');
const path = require('path');
const { test, expect } = require('@playwright/test');

const { resetE2E } = require('./helpers/gui');
const {
  paths,
  readJson,
  readText,
  readAdminUsers,
  ensureAvatarFixture,
  writeDiscoveryReport,
} = require('./helpers/runtime');

const admin = {
  username: 'gui-admin',
  email: 'gui-admin@example.com',
  password: 'NanobotGuiTest!123',
};

test.describe.configure({ mode: 'serial' });

test.beforeEach(async ({ request }) => {
  await resetE2E(request);
});

async function login(page) {
  await page.goto('/login');
  await page.getByTestId('login-identifier').fill(admin.username);
  await page.getByTestId('login-password').fill(admin.password);
  await page.getByTestId('login-submit').click();
}

async function ensureAuthenticated(page) {
  await page.goto('/');
  if (page.url().includes('/setup/admin')) {
    await page.getByTestId('admin-setup-username').fill(admin.username);
    await page.getByTestId('admin-setup-email').fill(admin.email);
    await page.getByTestId('admin-setup-password').fill(admin.password);
    await page.getByTestId('admin-setup-password-confirm').fill(admin.password);
    await page.getByTestId('admin-setup-submit').click();
    await expect(page).toHaveURL(/\/setup\/provider/);
    return;
  }

  if (page.url().includes('/login')) {
    await login(page);
  }
}

test('admin bootstrap persists the initial profile record', async ({ page }) => {
  await ensureAuthenticated(page);

  const users = readAdminUsers();
  expect(users).toHaveLength(1);
  expect(users[0]).toMatchObject({
    username: admin.username,
    email: admin.email,
  });

  await page.goto('/profile');
  await expect(page.getByTestId('profile-username')).toHaveValue(admin.username);
  await expect(page.getByTestId('profile-email')).toHaveValue(admin.email);
});

test('wizard saves provider, channel, agent, and instructions into the workspace', async ({ page }) => {
  await ensureAuthenticated(page);

  await page.goto('/setup/provider');
  await page.getByTestId('provider-select').selectOption('openrouter');
  await page.getByTestId('provider-model').fill('openai/gpt-4.1-mini');
  await page.getByTestId('provider-api-key').fill('test-openrouter-key');
  await page.getByTestId('provider-continue').click();
  await expect(page).toHaveURL(/\/setup\/channel/);

  await page.getByTestId('channel-select').selectOption('telegram');
  await page.getByTestId('channel-field-token').fill('123456:ABCDEF');
  await page.getByTestId('channel-field-allow_from').fill('owner-1, owner-2');
  await page.getByTestId('channel-send-progress').check();
  await page.getByTestId('channel-send-tool-hints').check();
  await page.getByTestId('channel-continue').click();
  await expect(page).toHaveURL(/\/setup\/agent/);

  const instructions = '# GUI E2E Instructions\n- Answer clearly.\n- Mention the GUI smoke test when asked.';
  await page.getByTestId('agent-model').fill('openai/gpt-4.1-mini');
  await page.getByTestId('agent-provider').fill('openrouter');
  await page.getByTestId('agent-instruction').fill(instructions);
  await page.getByTestId('agent-response-style').selectOption('brief');
  await page.getByTestId('agent-tools-enabled').check();
  await page.getByTestId('agent-restrict-workspace').check();
  await page.getByTestId('agent-finish').click();
  await expect(page).toHaveURL(/\/dashboard/);

  const config = readJson(paths.configPath);
  expect(config.agents.defaults.provider).toBe('openrouter');
  expect(config.agents.defaults.model).toBe('openai/gpt-4.1-mini');
  expect(config.providers.openrouter.apiKey).toBe('test-openrouter-key');
  expect(config.channels.telegram.enabled).toBe(true);
  expect(config.channels.telegram.token).toBe('123456:ABCDEF');
  expect(config.channels.telegram.allowFrom).toEqual(['owner-1', 'owner-2']);
  expect(config.channels.sendProgress).toBe(true);
  expect(config.channels.sendToolHints).toBe(true);
  expect(readText(paths.agentsPath)).toContain('GUI E2E Instructions');
});

test('settings and memory editor persist values to config.json and MEMORY.md', async ({ page }) => {
  await ensureAuthenticated(page);

  await page.goto('/settings');
  await page.getByTestId('settings-exec-timeout').fill('75');
  await page.getByTestId('settings-path-append').fill('/usr/local/bin');
  await page.getByTestId('settings-tools-enabled').check();
  await page.getByTestId('settings-restrict-workspace').check();
  await page.getByTestId('settings-send-progress').uncheck();
  await page.getByTestId('settings-send-tool-hints').check();
  await page.getByTestId('settings-save').click();
  await expect(page.getByTestId('flash-message')).toContainText('Settings saved.');

  await page.goto('/memory?doc=memory');
  const memoryText = '# Memory\n- GUI E2E marker: 2026-03-10\n- Persist this exact line.';
  await page.getByTestId('memory-content').fill(memoryText);
  await page.getByTestId('memory-save').click();
  await expect(page.getByTestId('flash-message')).toContainText('saved');

  const config = readJson(paths.configPath);
  expect(config.tools.enabled).toBe(true);
  expect(config.tools.restrictToWorkspace).toBe(true);
  expect(config.tools.exec.timeout).toBe(75);
  expect(config.tools.exec.pathAppend).toBe('/usr/local/bin');
  expect(config.channels.sendProgress).toBe(false);
  expect(config.channels.sendToolHints).toBe(true);
  expect(readText(paths.memoryPath)).toContain('GUI E2E marker: 2026-03-10');
});

test('agent response style updates USER.md and explicit USER.md edits persist', async ({ page }) => {
  await ensureAuthenticated(page);

  await page.goto('/setup/agent');
  await page.getByTestId('agent-response-style').selectOption('brief');
  await page.getByTestId('agent-save').click();
  await expect(page.getByTestId('flash-message')).toContainText('Agent settings saved.');
  await expect(readText(path.join(paths.workspaceDir, 'USER.md'))).toContain('- [x] Brief and concise');

  await page.goto('/memory?doc=user');
  const userDoc = '# User Profile\n- Prefers concise responses.\n- Explicit USER.md persistence marker.';
  await page.getByTestId('memory-content').fill(userDoc);
  await page.getByTestId('memory-save').click();
  await expect(page.getByTestId('flash-message')).toContainText('saved');
  expect(readText(path.join(paths.workspaceDir, 'USER.md'))).toContain('Explicit USER.md persistence marker.');

  await page.goto('/memory?doc=memory');
  await page.goto('/memory?doc=user');
  await expect(page.getByTestId('memory-content')).toContainText('Explicit USER.md persistence marker.');
});

test('profile updates persist account fields and avatar media', async ({ page }) => {
  await ensureAuthenticated(page);

  await page.goto('/profile');
  await page.getByTestId('profile-display-name').fill('GUI Test Admin');
  await page.getByTestId('profile-username').fill('gui-admin-updated');
  await page.getByTestId('profile-email').fill('gui-updated@example.com');
  const avatarInput = page.getByTestId('profile-avatar');
  await avatarInput.setInputFiles(ensureAvatarFixture());
  await expect
    .poll(async () =>
      avatarInput.evaluate((element) => ({
        count: element.files?.length ?? 0,
        name: element.files?.[0]?.name || null,
      })),
    )
    .toEqual({ count: 1, name: 'avatar.png' });
  await page.getByTestId('profile-save').click();
  await expect(page.getByTestId('flash-message')).toContainText('Profile updated.');

  admin.username = 'gui-admin-updated';
  admin.email = 'gui-updated@example.com';

  const users = readAdminUsers();
  expect(users[0].username).toBe('gui-admin-updated');
  expect(users[0].email).toBe('gui-updated@example.com');
  expect(users[0].display_name).toBe('GUI Test Admin');
  expect(users[0].avatar_path).toMatch(/^avatars\//);
  expect(fs.existsSync(path.join(paths.mediaDir, users[0].avatar_path))).toBe(true);

  await page.goto('/dashboard');
  await expect(page.locator('.profile-chip')).toContainText('GUI Test Admin');
});

test('crawls authenticated pages, screenshots them, and records interactive elements', async ({ page }) => {
  await ensureAuthenticated(page);

  fs.mkdirSync(paths.pageShotsDir, { recursive: true });
  const report = [];
  const pageLinks = new Set(['/dashboard']);

  await page.goto('/dashboard');
  const navLinks = await page.locator('aside .nav a[href]').evaluateAll((elements) =>
    elements.map((element) => element.getAttribute('href')).filter(Boolean),
  );
  for (const href of navLinks) {
    pageLinks.add(href);
  }

  for (const href of pageLinks) {
    await page.goto(href);
    await page.waitForLoadState('networkidle');

    const inputs = await page.locator('input, textarea, select').evaluateAll((elements) =>
      elements.map((element) => ({
        tag: element.tagName.toLowerCase(),
        name: element.getAttribute('name') || '',
        type: element.getAttribute('type') || '',
        visible: !!(element.offsetParent || element === document.activeElement),
      })),
    );
    const buttons = await page.locator('button, [role=\"button\"], input[type=\"submit\"]').evaluateAll((elements) =>
      elements.map((element) => ({
        text: (element.textContent || '').trim(),
        disabled: !!element.disabled,
      })),
    );
    const links = await page.locator('a[href]').evaluateAll((elements) =>
      elements.map((element) => ({
        text: (element.textContent || '').trim(),
        href: element.getAttribute('href') || '',
      })),
    );

    const safeName = href.replace(/[^a-z0-9]+/gi, '_').replace(/^_+|_+$/g, '') || 'root';
    await page.screenshot({
      path: path.join(paths.pageShotsDir, `${safeName}.png`),
      fullPage: true,
    });

    report.push({
      href,
      title: await page.title(),
      inputs,
      buttons,
      links,
    });
  }

  writeDiscoveryReport({
    generatedAt: new Date().toISOString(),
    pageCount: report.length,
    pages: report,
  });

  expect(report.length).toBeGreaterThanOrEqual(8);
  expect(fs.existsSync(path.join(paths.reportDir, 'gui-discovery-report.json'))).toBe(true);
});
