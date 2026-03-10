const { defineConfig, devices } = require('@playwright/test');

const port = process.env.NANOBOT_GUI_E2E_PORT || '18795';
const baseURL = process.env.NANOBOT_GUI_BASE_URL || `http://127.0.0.1:${port}`;
const browserList = (process.env.NANOBOT_GUI_E2E_BROWSERS || 'chromium')
  .split(',')
  .map((value) => value.trim().toLowerCase())
  .filter(Boolean);

const browserProjects = {
  chromium: {
    name: 'chromium',
    use: {
      ...devices['Desktop Chrome'],
      browserName: 'chromium',
    },
  },
  firefox: {
    name: 'firefox',
    use: {
      ...devices['Desktop Firefox'],
      browserName: 'firefox',
    },
  },
};

module.exports = defineConfig({
  testDir: './tests/e2e',
  timeout: 90000,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  outputDir: 'test-results/artifacts',
  reporter: [
    ['list'],
    ['html', { outputFolder: 'playwright-report', open: 'never' }],
  ],
  use: {
    baseURL,
    headless: true,
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    trace: 'retain-on-failure',
  },
  projects: browserList.map((browserName) => browserProjects[browserName]).filter(Boolean),
  webServer: process.env.NANOBOT_GUI_BASE_URL
    ? undefined
    : {
        command: 'python3 scripts/e2e/run_gui_e2e_server.py',
        url: `${baseURL}/health`,
        reuseExistingServer: false,
        timeout: 120000,
      },
});
