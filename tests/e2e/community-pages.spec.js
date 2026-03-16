const { test, expect } = require('@playwright/test');
const { bootstrapAndCompleteSetup } = require('./helpers/gui');

test.describe('community pages', () => {
  test('render marketplace, stacks, showcase, and stats using the configured hub', async ({ page, request }) => {
    await bootstrapAndCompleteSetup(page, request);

    await page.goto('/community/discover');
    await expect(page.getByTestId('nav-community-discover')).toHaveClass(/active/);
    await expect(page.getByTestId('community-mcp-results-list')).toBeVisible();

    await page.goto('/community/search/mcp');
    await expect(page.getByTestId('nav-community-search-mcp')).toHaveClass(/active/);
    await expect(page.getByTestId('community-search-mcp-form')).toBeVisible();

    await page.goto('/community/publish/mcp');
    await expect(page.getByTestId('nav-community-publish-mcp')).toHaveClass(/active/);
    await expect(page.getByTestId('community-publish-mcp-panel')).toBeVisible();

    await page.goto('/community/stacks');
    await expect(page.getByTestId('nav-community-stacks')).toHaveClass(/active/);
    await expect(page.getByTestId('community-stack-results-list')).toBeVisible();

    await page.goto('/community/search/stacks');
    await expect(page.getByTestId('nav-community-search-stacks')).toHaveClass(/active/);
    await expect(page.getByTestId('community-search-stacks-form')).toBeVisible();

    await page.goto('/community/publish/stack');
    await expect(page.getByTestId('nav-community-publish-stack')).toHaveClass(/active/);
    await expect(page.getByTestId('community-publish-stack-panel')).toBeVisible();

    await page.goto('/community/showcase');
    await expect(page.getByTestId('nav-community-showcase')).toHaveClass(/active/);
    await expect(page.getByTestId('community-showcase-submit-card')).toBeVisible();

    await page.goto('/community/stats');
    await expect(page.getByTestId('nav-community-stats')).toHaveClass(/active/);
    await expect(page.getByRole('heading', { name: 'Community Stats' })).toBeVisible();
  });
});
