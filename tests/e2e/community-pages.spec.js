const { test, expect } = require('@playwright/test');
const { bootstrapAndCompleteSetup } = require('./helpers/gui');

test.describe('community pages', () => {
  test('render marketplace, stacks, showcase, and stats using the configured hub', async ({ page, request }) => {
    await bootstrapAndCompleteSetup(page, request);

    await page.goto('/community/discover');
    await expect(page.getByTestId('nav-community-discover')).toHaveClass(/active/);
    await expect(page.getByTestId('community-discover-form')).toBeVisible();
    await expect(page.getByTestId('community-discover-language')).toBeVisible();
    await expect(page.getByTestId('community-discover-min-reliability')).toBeVisible();
    await expect(page.getByTestId('community-submit-card')).toBeVisible();
    await expect(page.getByTestId('community-mcp-card-context7')).toBeVisible();

    await page.goto('/community/stacks');
    await expect(page.getByTestId('nav-community-stacks')).toHaveClass(/active/);
    await expect(page.getByTestId('community-stack-submit-card')).toBeVisible();
    await expect(page.getByTestId('community-stack-card-github-developer-stack')).toBeVisible();
    await page.getByTestId('community-stack-card-github-developer-stack').getByText('View Stack').click();
    await expect(page.getByTestId('community-stack-detail-import')).toBeVisible();

    await page.goto('/community/showcase');
    await expect(page.getByTestId('nav-community-showcase')).toHaveClass(/active/);
    await expect(page.getByTestId('community-showcase-submit-card')).toBeVisible();
    await expect(page.getByTestId('community-showcase-card-ai-research-assistant')).toBeVisible();
    await expect(page.getByTestId('community-showcase-import-ai-research-assistant')).toBeVisible();

    await page.goto('/community/stats');
    await expect(page.getByTestId('nav-community-stats')).toHaveClass(/active/);
    await expect(page.getByText('Top MCP Servers')).toBeVisible();
  });
});
