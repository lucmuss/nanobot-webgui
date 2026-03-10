const fs = require('fs');
const path = require('path');

const { test, expect } = require('@playwright/test');

const { bootstrapAndCompleteSetup, login } = require('./helpers/gui');
const {
  paths,
  ensureAvatarFixture,
  ensureTextFixture,
  readAdminUsers,
  createAdminUser,
} = require('./helpers/runtime');

test.describe.configure({ mode: 'serial' });

test('password changes and avatar replacement persist across logout and relogin', async ({ page, request }) => {
  await bootstrapAndCompleteSetup(page, request);

  await page.goto('/profile');
  await page.setInputFiles('[data-testid="profile-avatar"]', ensureAvatarFixture());
  await page.getByTestId('profile-save').click();
  await expect(page.getByTestId('flash-message')).toContainText('Profile updated.');

  const firstUsers = readAdminUsers();
  const firstAvatarPath = firstUsers[0].avatar_path;
  expect(firstAvatarPath).toMatch(/^avatars\//);
  expect(fs.existsSync(path.join(paths.mediaDir, firstAvatarPath))).toBe(true);

  await page.goto('/profile');
  await page.getByTestId('profile-password').fill('NanobotGuiTest!456');
  await page.getByTestId('profile-password-confirm').fill('NanobotGuiTest!456');
  await page.setInputFiles('[data-testid="profile-avatar"]', ensureAvatarFixture('avatar-replacement.png'));
  await page.getByTestId('profile-save').click();
  await expect(page.getByTestId('flash-message')).toContainText('Profile updated.');

  const secondUsers = readAdminUsers();
  const secondAvatarPath = secondUsers[0].avatar_path;
  expect(secondAvatarPath).toMatch(/^avatars\//);
  expect(secondAvatarPath).not.toBe(firstAvatarPath);
  expect(fs.existsSync(path.join(paths.mediaDir, secondAvatarPath))).toBe(true);

  await page.getByTestId('topbar-logout').click();
  await expect(page).toHaveURL(/\/login/);

  await page.getByTestId('login-identifier').fill('gui-admin');
  await page.getByTestId('login-password').fill('NanobotGuiTest!123');
  await page.getByTestId('login-submit').click();
  await expect(page.getByTestId('page-error')).toContainText('Sign-in failed');

  await login(page, { password: 'NanobotGuiTest!456' });
  await expect(page).toHaveURL(/\/dashboard/);
});

test('invalid avatar uploads are rejected with a readable validation error', async ({ page, request }) => {
  await bootstrapAndCompleteSetup(page, request);

  await page.goto('/profile');
  await page.setInputFiles('[data-testid="profile-avatar"]', ensureTextFixture());
  await page.getByTestId('profile-save').click();
  await expect(page.getByTestId('page-error')).toContainText('Avatar must be a PNG, JPEG, WEBP, or GIF image.');

  const users = readAdminUsers();
  expect(users[0].avatar_path).toBeNull();
});

test('duplicate username or email updates are rejected cleanly', async ({ page, request }) => {
  await bootstrapAndCompleteSetup(page, request);
  createAdminUser({
    username: 'existing-admin',
    email: 'existing-admin@example.com',
    password: 'ExistingAdmin!456',
  });

  await page.goto('/profile');
  await page.getByTestId('profile-username').fill('existing-admin');
  await page.getByTestId('profile-save').click();
  await expect(page.getByTestId('page-error')).toContainText('already in use');
  await expect(page.getByTestId('profile-email')).toHaveValue('gui-admin@example.com');

  await page.getByTestId('profile-username').fill('gui-admin');
  await page.getByTestId('profile-email').fill('existing-admin@example.com');
  await page.getByTestId('profile-save').click();
  await expect(page.getByTestId('page-error')).toContainText('already in use');

  const users = readAdminUsers();
  expect(users).toHaveLength(2);
  expect(users.find((user) => user.username === 'gui-admin')).toMatchObject({
    email: 'gui-admin@example.com',
  });
  expect(users.find((user) => user.username === 'existing-admin')).toMatchObject({
    email: 'existing-admin@example.com',
  });
});
