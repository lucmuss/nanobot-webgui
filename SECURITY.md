# Security Policy

## Reporting a Vulnerability

If you discover a security issue in `nanobot-webgui` or its packaged Nanobot runtime integration:

1. Do not open a public issue for the exploit details
2. Prefer a private GitHub security advisory
3. Include:
   - affected version
   - deployment shape
   - reproduction steps
   - practical impact
   - suggested mitigation if you have one

## Security Priorities for Operators

The WebGUI is an operations surface. That makes these controls especially important:

### 1. Protect the state directory

The GUI stores admin auth and runtime state under the Nanobot home directory.

Protect at least:

- `~/.nanobot/config.json`
- `~/.nanobot/gui.sqlite3`
- `~/.nanobot/gui-session.secret`
- `~/.nanobot/gui-state.json`
- `~/.nanobot/workspace/`

Recommended permissions:

```bash
chmod 700 ~/.nanobot
chmod 600 ~/.nanobot/config.json
chmod 600 ~/.nanobot/gui-session.secret
```

### 2. Never commit secrets

Do not store real secrets in:

- Markdown documentation
- screenshots
- example `.env` files committed to Git
- issue comments or release notes

### 3. Use HTTPS for public access

If the GUI is reachable outside localhost:

- terminate TLS in front of it
- run the GUI with `--secure-cookies`
- prefer VPN or reverse-proxy auth in addition to the built-in login

### 4. Restrict channel access

For production channels, always use allowlists where supported.

Examples:

- Telegram user IDs
- WhatsApp numbers with country code

### 5. Treat repair and shell features as privileged

The GUI can manage MCP runtime repair flows. That is powerful and should be handled carefully.

Safe pattern:

- use bounded repair recipes
- run them through an explicit operator-owned worker command

Dangerous pattern:

- enabling unrestricted shell fallback without a strong trust boundary

If you do not explicitly need that path, leave it disabled.

### 6. Review logs and uploads

Logs and uploaded files can contain sensitive information.

Protect:

- `~/.nanobot/logs/`
- `~/.nanobot/media/`

## Browser and Session Security

The WebGUI includes:

- admin bootstrap flow
- signed browser sessions
- protected routes behind login

Best practice:

- create one initial admin
- use a strong password
- rotate it if a device is compromised
- do not share the GUI session storage between unrelated deployments

## Dependency Hygiene

Keep both Python and Node dependencies current.

Recommended checks:

```bash
pip install pip-audit
pip-audit
```

```bash
cd bridge
npm audit
```

## Production Checklist

Before exposing the GUI more broadly, verify:

- HTTPS is enabled
- secrets are stored outside Git
- the Nanobot home directory is persistent and private
- backups exist
- update procedure is documented
- unrestricted repair fallback is disabled unless intentionally required
- Community Hub tokens are scoped and not reused broadly

## Incident Response

If you suspect compromise:

1. revoke affected API keys
2. rotate GUI admin credentials
3. review logs for suspicious access or shell actions
4. restore from a known-good backup if needed
5. update to the latest release
