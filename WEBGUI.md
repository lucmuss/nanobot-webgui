# WebGUI Deployment Guide

Current release target: `0.3.10`

This guide is the operator-facing companion to the main [README](./README.md). It explains how to run `nanobot-webgui` reliably in standalone mode, attached-to-existing-install mode, and Docker deployments.

## 1. What the WebGUI Manages

The WebGUI is a browser shell around the normal Nanobot runtime.

It manages:

- admin bootstrap and login
- provider, channel, and agent setup
- MCP inspect, install, test, enable, disable, and remove
- chat, uploads, logs, and validation
- memory documents such as `SOUL.md`, `USER.md`, `AGENTS.md`, and `TOOLS.md`
- optional Community Hub browsing and publishing flows

It does not replace the upstream Nanobot runtime. It operates it, and installs now pull `HKUDS/nanobot@main` directly.

## 2. Two Supported Deployment Modes

### Standalone mode

Use this if you want a fresh install:

```bash
pip install -e .
nanobot onboard
nanobot-webgui gui --host 0.0.0.0 --port 18791
```

This editable install fetches the latest upstream `nanobot` `main` branch and layers the GUI package on top.

### Existing-install mode

Use this if you already have a Nanobot workspace and config:

```bash
nanobot-webgui gui --config /path/to/config.json --workspace /path/to/workspace
```

The GUI reads and writes the selected Nanobot config and stores GUI-specific state nearby:

- `gui.sqlite3`
- `gui-state.json`
- `gui-session.secret`
- `media/`
- `logs/`

If you point the GUI at a production instance, make a backup first.

## 3. Minimal Local Startup

```bash
git clone https://github.com/lucmuss/nanobot-webgui.git
cd nanobot-webgui
pip install -e .[dev]
nanobot onboard
nanobot-webgui gui --host 0.0.0.0 --port 18791
```

That install path also uses the latest upstream `main` branch at install time.

Open:

- <http://127.0.0.1:18791/>

## 4. Docker Compose

The repository ships with a compose setup for:

- `nanobot-gateway`
- `nanobot-gui`
- optional `nanobot-community-hub`

Start:

```bash
docker compose up -d --build nanobot-gateway nanobot-gui
```

Stop:

```bash
docker compose down
```

Logs:

```bash
docker compose logs -f nanobot-gui
docker compose logs -f nanobot-gateway
```

Default ports:

- `18790` gateway
- `18791` WebGUI
- `18811` Community Hub

## 5. Persistent Data

Back up these regularly:

- `~/.nanobot/config.json`
- `~/.nanobot/gui.sqlite3`
- `~/.nanobot/gui-session.secret`
- `~/.nanobot/gui-state.json`
- `~/.nanobot/workspace/`
- `~/.nanobot/logs/`
- `~/.nanobot/media/`

If you use Docker, mount the persistent state explicitly, for example:

- `~/.nanobot:/root/.nanobot`

## 6. HTTPS and Session Security

For public or semi-public use:

1. Put the GUI behind HTTPS
2. Start the GUI with secure cookies
3. Protect access with at least one more outer control such as VPN, reverse-proxy auth, or IP restriction

Example:

```bash
nanobot-webgui gui --host 0.0.0.0 --port 18791 --secure-cookies
```

## 7. Community Hub Wiring

The WebGUI can use `nanobot-community-hub` for:

- `Discover MCP`
- `Search MCP`
- MCP detail pages with community signals
- stack import flows
- showcase import flows
- optional MCP publishing
- anonymous runtime telemetry

Typical environment split:

- GUI repo: `/srv/projects/agents/nanobot-dev-src`
- Hub repo: `/srv/projects/services/nanobot-hub`
- compose stack: `/srv/docker/ai-stack`

Typical URLs:

- internal API URL: `http://nanobot-community-hub:18811/api/v1`
- GUI public URL: `https://your-nanobot-gui.example.com`
- Hub public URL: `https://nanobot-hub.eu`

Required GUI-side settings:

- `NANOBOT_GUI_COMMUNITY_API_URL`
- `NANOBOT_GUI_COMMUNITY_PUBLIC_URL`

Publishing also needs:

- `NANOBOT_GUI_COMMUNITY_API_TOKEN`

That must match the Hub-side:

- `NANOBOT_HUB_API_TOKEN`

## 8. Cloudflare Tunnel Mapping

If you use `cloudflared`, a typical mapping is:

- `your-nanobot-gui.example.com` -> `http://host.docker.internal:18791`
- `nanobot-hub.eu` -> `http://host.docker.internal:18811`

## 9. Update Banner and Controlled Updates

The GUI can check GitHub releases and show a banner when a new version is available.

Example:

```bash
nanobot-webgui gui \
  --host 0.0.0.0 \
  --port 18791 \
  --update-check \
  --update-repo lucmuss/nanobot-webgui \
  --update-mode command \
  --update-command "/usr/local/bin/nanobot-webgui-update.sh"
```

Important:

- the GUI does not self-mutate inside the container
- `Update now` only runs the command you explicitly configure

Typical host update command:

```bash
#!/usr/bin/env bash
set -e
cd /srv/projects/agents/nanobot-webgui
git pull
cd /srv/docker/ai-stack
docker compose up -d --build nanobot-gui nanobot-gateway
```

## 10. MCP Repair Worker

The GUI can offload MCP runtime repair to a bounded worker command.

Example:

```bash
nanobot-webgui gui \
  --host 0.0.0.0 \
  --port 18791 \
  --repair-mode command \
  --repair-command "docker compose run --rm nanobot-repair-worker nanobot repair-worker"
```

Supported safe repair recipes include:

- `install_node`
- `install_uv`
- `install_python_build_tools`
- `install_docker_cli`

There is also a dangerous opt-in in Settings:

- `Enable Unrestricted Agent + Shell for MCP repair fallback`

Leave that disabled unless you intentionally accept that trust boundary.

## 11. Operator Runbook

### Health check

```bash
curl http://127.0.0.1:18791/health
```

### Restart GUI container

```bash
docker compose restart nanobot-gui
```

### Rebuild GUI after pulling changes

```bash
docker compose up -d --build nanobot-gui
```

### Run the full Python test suite

```bash
python3 -m pytest tests -q
```

### Run the browser smoke suite

```bash
npm run test:e2e:critical
```

## 12. Troubleshooting

### The page loads but onboarding never completes

Check:

- provider API key exists
- model is set
- the config file is writable
- the GUI is pointing at the intended workspace

### MCP install worked but the MCP stays unhealthy

Check:

- missing environment variables
- missing runtimes such as `node`, `uv`, or `python`
- endpoint or command copied incorrectly
- `Run MCP Test` output in the GUI registry

### Community pages are empty

Check:

- Community Hub URL
- GUI-to-Hub token configuration if write actions are expected
- Hub service health and API reachability

### Browser login behaves strangely behind a reverse proxy

Check:

- HTTPS is actually terminating before the browser session
- `--secure-cookies` matches your deployment mode
- proxy preserves the original host and scheme

## 13. Related Documents

- [README.md](./README.md)
- [GUI_TESTING.md](./GUI_TESTING.md)
- [SECURITY.md](./SECURITY.md)
- [COMMUNICATION.md](./COMMUNICATION.md)
