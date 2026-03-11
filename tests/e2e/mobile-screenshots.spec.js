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

test.describe('mobile screenshots', () => {
  test('capture mobile webgui pages', async ({ page, request }) => {
    fs.mkdirSync(outputDir, { recursive: true });

    await page.setViewportSize({ width: 390, height: 844 });
    await bootstrapAndCompleteSetup(page, request);

    await capture(page, '/dashboard', 'dashboard-mobile.png', 'dashboard-system-info');
    await capture(page, '/chat', 'chat-mobile.png', 'chat-live-shell');
    await capture(page, '/mcp', 'mcp-mobile.png', 'mcp-search-form');
    await capture(page, '/memory', 'memory-mobile.png', 'memory-form');
    await capture(page, '/settings', 'settings-mobile.png', 'settings-form');
    await capture(page, '/profile', 'profile-mobile.png', 'profile-form');
    await capture(page, '/community/discover', 'community-discover-mobile.png', 'community-discover-form');
    await capture(page, '/community/stacks', 'community-stacks-mobile.png', 'community-stacks-form');
    await capture(page, '/community/showcase', 'community-showcase-mobile.png', 'community-showcase-form');
    await capture(page, '/community/stats', 'community-stats-mobile.png', 'nav-community-stats');
  });
});
