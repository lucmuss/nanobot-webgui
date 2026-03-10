const { test, expect } = require('@playwright/test');
const AxeBuilder = require('@axe-core/playwright').default;

const { resetE2E, bootstrapAndCompleteSetup } = require('./helpers/gui');

function highImpactViolations(results) {
  return results.violations.filter((violation) => ['serious', 'critical'].includes(violation.impact));
}

async function expectNoHighImpactViolations(page, scopeLabel) {
  const results = await new AxeBuilder({ page }).disableRules(['color-contrast']).analyze();
  const violations = highImpactViolations(results);
  expect(
    violations,
    `${scopeLabel} should not have serious or critical accessibility violations`,
  ).toEqual([]);
}

test.describe.configure({ mode: 'serial' });

test('admin setup page passes the a11y smoke scan', async ({ page, request }) => {
  await resetE2E(request);
  await page.goto('/setup/admin');
  await expectNoHighImpactViolations(page, 'Admin setup');
});

test('dashboard, MCP, and chat pass the authenticated a11y smoke scan', async ({ page, request }) => {
  await bootstrapAndCompleteSetup(page, request);

  await page.goto('/dashboard');
  await expectNoHighImpactViolations(page, 'Dashboard');

  await page.goto('/mcp');
  await expectNoHighImpactViolations(page, 'MCP');

  await page.goto('/chat');
  await expectNoHighImpactViolations(page, 'Chat');
});
