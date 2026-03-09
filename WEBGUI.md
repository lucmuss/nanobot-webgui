# WebGUI Deployment Guide

This guide is for running the `nanobot` WebGUI as a stable self-hosted service.

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

## Reverse Proxy

Put the GUI behind HTTPS. The important part is that the browser reaches the GUI over TLS, then start the GUI with secure cookies enabled:

```bash
nanobot gui --host 0.0.0.0 --port 18791 --secure-cookies
```

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
