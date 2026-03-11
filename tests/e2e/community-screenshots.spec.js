const fs = require('fs');
const path = require('path');
const { test, expect } = require('@playwright/test');
const { bootstrapAndCompleteSetup } = require('./helpers/gui');

const outputDir = path.join(process.cwd(), 'output', 'gui-screenshots');

async function capture(page, route, fileName, readyTestId) {
  await page.goto(route);
  await expect(page.getByTestId(readyTestId)).toBeVisible();
  await page.screenshot({ path: path.join(outputDir, fileName), fullPage: true });
}

test.describe('community screenshots', () => {
  test('capture community and dashboard pages', async ({ page, request }) => {
    fs.mkdirSync(outputDir, { recursive: true });

    await bootstrapAndCompleteSetup(page, request);

    await capture(page, '/dashboard', 'dashboard.png', 'dashboard-system-info');
    await capture(page, '/community/discover', 'community-discover.png', 'community-discover-form');
    await capture(page, '/community/mcp/context7', 'community-mcp-detail.png', 'topbar-chat-link');
    await capture(page, '/community/stacks', 'community-stacks.png', 'community-stacks-form');
    await capture(page, '/community/stacks/github-developer-stack', 'community-stack-detail.png', 'topbar-chat-link');
    await capture(page, '/community/showcase', 'community-showcase.png', 'community-showcase-form');
    await capture(page, '/community/stats', 'community-stats.png', 'nav-community-stats');
  });
});
