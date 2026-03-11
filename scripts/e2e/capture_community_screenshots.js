const { chromium } = require('@playwright/test');

async function main() {
  const base = process.env.NANOBOT_GUI_BASE_URL || 'http://127.0.0.1:18795';
  const outputDir = process.env.NANOBOT_GUI_SCREENSHOT_DIR || 'output/gui-screenshots';
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1080 } });

  await page.goto(`${base}/setup/admin`);
  await page.getByTestId('admin-setup-username').fill('gui-admin');
  await page.getByTestId('admin-setup-email').fill('gui-admin@example.com');
  await page.getByTestId('admin-setup-password').fill('NanobotGuiTest!123');
  await page.getByTestId('admin-setup-password-confirm').fill('NanobotGuiTest!123');
  await page.getByTestId('admin-setup-submit').click();

  await page.getByTestId('provider-select').selectOption('openrouter');
  await page.getByTestId('provider-model').fill('openai/gpt-4.1-mini');
  await page.getByTestId('provider-api-key').fill('e2e-openrouter-key');
  await page.getByTestId('provider-continue').click();

  await page.getByTestId('channel-select').selectOption('telegram');
  await page.getByTestId('channel-field-token').fill('123456:ABCDEF');
  await page.getByTestId('channel-field-allow_from').fill('owner-1');
  await page.getByTestId('channel-continue').click();

  await page.getByTestId('agent-model').fill('openai/gpt-4.1-mini');
  await page.getByTestId('agent-provider').fill('openrouter');
  await page.getByTestId('agent-instruction').fill('# Screenshot run\n- Keep stable.');
  await page.getByTestId('agent-finish').click();

  const targets = [
    ['/dashboard', 'dashboard.png'],
    ['/community/discover', 'community-discover.png'],
    ['/community/stacks', 'community-stacks.png'],
    ['/community/showcase', 'community-showcase.png'],
    ['/community/stats', 'community-stats.png'],
  ];

  for (const [url, file] of targets) {
    await page.goto(`${base}${url}`);
    await page.screenshot({ path: `${outputDir}/${file}`, fullPage: true });
  }

  await browser.close();
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
