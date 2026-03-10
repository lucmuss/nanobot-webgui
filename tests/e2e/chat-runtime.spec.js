const fs = require('fs');

const { test, expect } = require('@playwright/test');

const { bootstrapAndCompleteSetup } = require('./helpers/gui');
const { ensureTextFixture, paths } = require('./helpers/runtime');

test.describe.configure({ mode: 'serial' });

test('chat without a healthy agent points the user back to provider setup', async ({ page, request }) => {
  await bootstrapAndCompleteSetup(page, request, { apiKey: '' });

  await page.goto('/chat');
  await page.getByTestId('chat-message').fill('Hello from an unhealthy runtime');
  await page.getByTestId('chat-send').click();

  await expect(page).toHaveURL(/\/chat/);
  await expect(page.getByTestId('flash-message')).toContainText('Provider authentication failed');
  await expect(page.getByTestId('chat-error-card')).toContainText('Missing Authentication header');
  await expect(page.getByTestId('chat-error-card')).toContainText('Open Provider');
});

test('chat shows active MCPs, template prompts, tool activity, file upload, and clear history', async ({ page, request }) => {
  await bootstrapAndCompleteSetup(page, request);

  await page.goto('/mcp');
  await page.getByTestId('mcp-source').fill('https://github.com/example/echo-mcp');
  await page.getByTestId('mcp-install').click();
  await expect(page.getByTestId('flash-message')).toContainText('installed');
  await page.getByTestId('mcp-toggle-echo').click();
  await expect(page.getByTestId('flash-message')).toContainText('enabled for the main chat runtime');

  await page.goto('/chat');
  await expect(page.getByTestId('chat-active-mcp-servers-card')).toContainText('echo');
  await expect(page.getByTestId('chat-active-mcp-tools-card')).toContainText('echo_message');

  await page.getByTestId('chat-message').fill('Use the active MCP tool for this request.');
  await page.getByTestId('chat-send').click();
  await expect(page.getByTestId('flash-message')).toContainText('Response received.');
  await expect(page.getByTestId('chat-history')).toContainText('Used tool `echo_message` successfully.');
  await expect(page.getByTestId('chat-recent-tool-activity-card')).toContainText('Tool used: echo_message');

  await page.getByTestId('chat-template-input-repo_analyze').fill('https://github.com/example/repo');
  await page.getByTestId('chat-template-submit-repo_analyze').click();
  await expect(page.getByTestId('flash-message')).toContainText('Template prompt sent.');
  await expect(page.getByTestId('chat-history')).toContainText('Repository analysis ready');

  await page.getByTestId('chat-template-input-error_explain').fill('Missing Authentication header');
  await page.getByTestId('chat-template-submit-error_explain').click();
  await expect(page.getByTestId('chat-history')).toContainText('Plain-English error explanation ready');

  const uploadFixture = ensureTextFixture('chat-upload.txt', 'line one from upload\nline two from upload');
  await page.setInputFiles('[data-testid="chat-attachment"]', uploadFixture);
  await page.getByTestId('chat-upload-message').fill('Please inspect this uploaded file.');
  await page.getByTestId('chat-upload-submit').click();
  await expect(page.getByTestId('flash-message')).toContainText('Uploaded chat-upload.txt and sent it to chat.');
  await expect(page.getByTestId('chat-history')).toContainText('line one from upload');
  const uploadedFiles = fs.readdirSync(`${paths.workspaceDir}/uploads`);
  const uploadedName = uploadedFiles.find((name) => name.endsWith('chat-upload.txt'));
  expect(uploadedName).toBeTruthy();
  expect(fs.readFileSync(`${paths.workspaceDir}/uploads/${uploadedName}`, 'utf8')).toContain('line one from upload');

  await page.getByTestId('chat-clear').click();
  await expect(page.getByTestId('flash-message')).toContainText('Chat history cleared.');
  await expect(page.getByTestId('chat-history')).toContainText('No messages yet');
});
