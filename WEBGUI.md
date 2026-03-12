# WebGUI Deployment Guide

Current release target: `0.3.2`

This guide is for running the `nanobot` WebGUI as a stable self-hosted service.

## What This GUI Is

This project can be used in two ways:

### Standalone mode

You install `nanobot-webgui`, run `nanobot onboard`, then use the GUI as the main setup and operations surface.

### Existing-install mode

You already have a Nanobot installation and want the GUI to manage that same config and workspace.

Example:

```bash
nanobot gui --config /path/to/config.json --workspace /path/to/workspace
```

In this mode, the GUI reads and writes the selected Nanobot config, and also creates GUI-specific files next to it:

- `gui.sqlite3`
- `gui-state.json`
- `gui-session.secret`
- `media/`
- `logs/`

Make a backup first if you connect the GUI to a production Nanobot instance.

## Architecture

Recommended shape:

1. `nanobot-gateway` for the headless runtime
2. `nanobot-gui` for browser-based admin
3. persistent `~/.nanobot` volume
4. HTTPS reverse proxy in front of the GUI

The GUI can run without the gateway, but the cleanest production setup keeps both services available.

## Docker Compose

The repository ships with a basic compose file that exposes:

- `18790` for `nanobot-gateway`
- `18791` for `nanobot-gui`
- `18811` for `nanobot-community-hub`

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
docker compose logs -f nanobot-community-hub
```

## Community Hub Integration

The GUI can now consume real community data from a separate FastAPI/Jinja2/HTMX service called `nanobot-community-hub`.

In the current `ai-stack` deployment, the intended wiring is:

- GUI internal community API: `http://nanobot-community-hub:18811/api/v1`
- GUI public URL: `https://your-nanobot-gui.example.com`
- Hub public URL: `https://nanobot-community-hub.kolibri-kollektiv.eu`
- Hub database: shared PostgreSQL from `apps-stack`, reachable as `postgres:5432` on `apps-shared`

The GUI settings page includes three community toggles:

- `Receive community recommendations`
- `Show community marketplace stats`
- `Share anonymous MCP runtime metrics`
- `Allow this GUI to publish MCP repository entries to the community hub`

The main community-backed user flows are now:

- `Community -> Discover MCP -> Install from Community`
- `Community -> MCP Stacks -> Import Stack`
- `Community -> Showcase -> Import Setup`
- local MCP detail -> `Publish to Community Hub`

Recommended config from the Hub is shown in:

- community MCP detail pages
- local MCP detail pages when the local repository matches a Hub entry

### Community write authentication

Read-only community browsing only needs:

- `NANOBOT_GUI_COMMUNITY_API_URL`
- `NANOBOT_GUI_COMMUNITY_PUBLIC_URL`

Publishing from the GUI needs an additional trusted service token:

- `NANOBOT_GUI_COMMUNITY_API_TOKEN`

That token must match the Hub-side:

- `NANOBOT_HUB_API_TOKEN`

The Hub itself also supports browser admin authentication:

- first-run bootstrap: `/setup/admin`
- later login: `/login`
- moderation and controlled write flows: `/admin`

With that publishing toggle enabled, users can:

- submit a GitHub MCP repository directly from `Community -> Discover MCP`
- publish a locally installed MCP from its MCP detail page

Telemetry is intentionally limited to technical MCP runtime fields such as:

- MCP slug
- success / error code
- transport
- timeout bucket
- retries
- Nanobot version

It must not send prompts, local paths, API keys, or chat contents.

### Cloudflare Tunnel Mapping

If you use the existing `cloudflared` service in `ai-stack`, create these public hostname mappings in the Cloudflare dashboard:

- `your-nanobot-gui.example.com` -> `http://host.docker.internal:18791`
- `nanobot-community-hub.kolibri-kollektiv.eu` -> `http://host.docker.internal:18811`

That is enough for this stack because the services already publish fixed local ports and the Cloudflare tunnel container can reach `host.docker.internal`.

Important:

- Docker Compose does not automatically keep the GUI on the newest version
- if you use `build:`, you still need `git pull` and a rebuild
- if you use published images, you still need `docker compose pull` and restart/recreate

## Reverse Proxy

Put the GUI behind HTTPS. The important part is that the browser reaches the GUI over TLS, then start the GUI with secure cookies enabled:

```bash
nanobot gui --host 0.0.0.0 --port 18791 --secure-cookies
```

## GUI Update Banner

The GUI can check GitHub for a newer release at login and at most once per day. If a newer version exists, a banner appears with:

- `View release notes`
- `Update now`

`Update now` does not self-mutate the container. It only runs an explicitly configured command for your deployment.

Example:

```bash
nanobot gui \
  --host 0.0.0.0 \
  --port 18791 \
  --update-check \
  --update-repo lucmuss/nanobot-webgui \
  --update-mode command \
  --update-command "/usr/local/bin/nanobot-webgui-update.sh"
```

Typical host script responsibilities:

- `git pull && docker compose up -d --build`
- or `docker compose pull && docker compose up -d`

Do not mount broad host-control interfaces into the GUI unless you intentionally accept that trust boundary.

### Typical update script

If you deploy from source:

```bash
#!/usr/bin/env bash
set -e
cd /srv/projects/agents/nanobot-webgui
git pull
cd /srv/docker/ai-stack
docker compose up -d --build nanobot-gui nanobot-gateway
```

If you deploy from published images:

```bash
#!/usr/bin/env bash
set -e
cd /srv/docker/ai-stack
docker compose pull nanobot-gui nanobot-gateway
docker compose up -d nanobot-gui nanobot-gateway
```

## MCP Repair Worker

The GUI can now offload MCP runtime repairs to an explicit worker command instead of trying to mutate the runtime directly inside the web process.

Recommended pattern:

```bash
nanobot gui \
  --host 0.0.0.0 \
  --port 18791 \
  --repair-mode command \
  --repair-command "docker compose run --rm nanobot-repair-worker nanobot repair-worker"
```

What the GUI does:

- detects missing runtimes from the MCP install/test metadata
- suggests supported recipes like `install_node`, `install_uv`, or `install_python_build_tools`
- starts the configured repair worker
- keeps the MCP disabled until you retest it

Recommended operator model:

- keep the GUI process simple and stable
- run repairs in a separate worker or sidecar command
- only enable MCPs after repair + test pass

Dangerous mode:

- The Settings page contains `Enable Unrestricted Agent + Shell for MCP repair fallback`.
- When enabled, the AI repair planner may return `unrestricted_agent_shell` and a shell command.
- That path is intentionally dangerous and should only be used in trusted self-hosted deployments with a clear sandbox boundary.

If you do not explicitly want this, leave it off.

## Persistent Data

Back up these paths regularly:

- `~/.nanobot/config.json`
- `~/.nanobot/gui.sqlite3`
- `~/.nanobot/gui-session.secret`
- `~/.nanobot/gui-state.json`
- `~/.nanobot/workspace/`
- `~/.nanobot/logs/`
- `~/.nanobot/media/`

## First Production Bring-Up

1. Start the GUI.
2. Open the WebGUI.
3. Create the single admin account.
4. Configure provider credentials.
5. Run `Validate Setup`.
6. Install and test at least one MCP.
7. Enable only the MCPs that passed testing.
8. Confirm `Chat` works before exposing the service more broadly.

If you are attaching the GUI to an existing Nanobot install, add:

9. Verify that the chosen `config.json` and workspace are the correct ones before saving changes.

## Recommended Operational Rules

- Keep `Safe Mode` enabled for first-time operators.
- Use the built-in validation page before debugging by hand.
- Prefer MCP installation through the GUI so metadata and runtime status stay in sync.
- Do not expose the GUI publicly without HTTPS and strong credentials.
- Keep GUI and gateway on a persistent volume so sessions and MCP installs survive restarts.

## Publish-Ready Notes

Before release, verify:

```bash
python3 -m compileall nanobot/gui
pytest tests/test_commands.py tests/test_config_paths.py tests/test_gui_config_service.py
docker compose up -d --build nanobot-gateway nanobot-gui
curl http://127.0.0.1:18791/health
```

## Upstream Reference

This WebGUI layer is based on the official `nanobot` project:

- <https://github.com/HKUDS/nanobot>
