const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');

const repoRoot = path.resolve(__dirname, '..', '..', '..');
const tempRoot = path.join(repoRoot, 'tmp', 'e2e');
const runtimeDir = path.join(tempRoot, 'gui-runtime');
const workspaceDir = path.join(tempRoot, 'workspace');
const reportDir = path.join(repoRoot, 'test-results');

const paths = {
  repoRoot,
  tempRoot,
  runtimeDir,
  workspaceDir,
  configPath: path.join(runtimeDir, 'config.json'),
  dbPath: path.join(runtimeDir, 'gui.sqlite3'),
  statePath: path.join(runtimeDir, 'gui-state.json'),
  mediaDir: path.join(runtimeDir, 'media'),
  memoryPath: path.join(workspaceDir, 'memory', 'MEMORY.md'),
  agentsPath: path.join(workspaceDir, 'AGENTS.md'),
  reportDir,
  pageShotsDir: path.join(reportDir, 'pages'),
};

function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, 'utf8'));
}

function writeJson(filePath, value) {
  fs.writeFileSync(filePath, JSON.stringify(value, null, 2), 'utf8');
}

function readText(filePath) {
  return fs.readFileSync(filePath, 'utf8');
}

function readAdminUsers(dbPath = paths.dbPath) {
  const script = [
    'import json, sqlite3, sys',
    'conn = sqlite3.connect(sys.argv[1])',
    'conn.row_factory = sqlite3.Row',
    'rows = [dict(row) for row in conn.execute("SELECT username, email, display_name, avatar_path FROM admin_users ORDER BY id")]',
    'print(json.dumps(rows))',
  ].join('; ');
  return JSON.parse(execFileSync('python3', ['-c', script, dbPath], { encoding: 'utf8' }));
}

function createAdminUser(
  {
    username,
    email,
    password = 'SecondaryAdmin!123',
  },
  dbPath = paths.dbPath,
) {
  const script = [
    'import sys',
    'import sqlite3',
    'from pathlib import Path',
    'sys.path.insert(0, sys.argv[1])',
    'from nanobot.gui.auth import AuthService, _hash_password',
    'db_path = Path(sys.argv[2])',
    'service = AuthService(db_path, db_path.with_name("gui-secret.txt"))',
    'service.init_db()',
    'conn = sqlite3.connect(db_path)',
    'conn.execute("INSERT INTO admin_users (username, email, display_name, password_hash) VALUES (?, ?, ?, ?)", (sys.argv[3], sys.argv[4].lower(), sys.argv[3], _hash_password(sys.argv[5])))',
    'conn.commit()',
    'conn.close()',
  ].join('; ');
  execFileSync('python3', ['-c', script, paths.repoRoot, dbPath, username, email, password], {
    encoding: 'utf8',
  });
}

function ensureAvatarFixture(name = 'avatar.png') {
  const fixtureDir = path.join(tempRoot, 'fixtures');
  const fixturePath = path.join(fixtureDir, name);
  if (!fs.existsSync(fixturePath)) {
    fs.mkdirSync(fixtureDir, { recursive: true });
    const variants = {
      'avatar.png':
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9pSdz+gAAAAASUVORK5CYII=',
      'avatar-replacement.png':
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBASL0sWQAAAAASUVORK5CYII=',
    };
    fs.writeFileSync(fixturePath, Buffer.from(variants[name] || variants['avatar.png'], 'base64'));
  }
  return fixturePath;
}

function ensureTextFixture(name = 'not-an-image.txt', content = 'plain text fixture for E2E tests') {
  const fixtureDir = path.join(tempRoot, 'fixtures');
  const fixturePath = path.join(fixtureDir, name);
  if (!fs.existsSync(fixturePath)) {
    fs.mkdirSync(fixtureDir, { recursive: true });
    fs.writeFileSync(fixturePath, content, 'utf8');
  }
  return fixturePath;
}

function writeDiscoveryReport(report) {
  fs.mkdirSync(paths.reportDir, { recursive: true });
  fs.mkdirSync(paths.pageShotsDir, { recursive: true });
  fs.writeFileSync(
    path.join(paths.reportDir, 'gui-discovery-report.json'),
    JSON.stringify(report, null, 2),
    'utf8',
  );
}

module.exports = {
  paths,
  readJson,
  writeJson,
  readText,
  readAdminUsers,
  createAdminUser,
  ensureAvatarFixture,
  ensureTextFixture,
  writeDiscoveryReport,
};
