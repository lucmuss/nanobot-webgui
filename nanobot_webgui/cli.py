"""CLI overlay that adds the WebGUI commands on top of upstream nanobot."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

import typer

from nanobot import __logo__
from nanobot.cli.commands import app as upstream_app

from nanobot_webgui.repair_worker import run_repair_recipe

app = upstream_app


def _utc_now() -> str:
    """Return a compact UTC timestamp string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _resolve_gateway_state_path(config_path: Path, explicit_path: str | None = None) -> Path:
    """Return the shared gateway heartbeat file path."""
    candidate = (explicit_path or os.getenv("NANOBOT_GUI_GATEWAY_STATE_PATH", "")).strip()
    if candidate:
        return Path(candidate).expanduser().resolve()
    return config_path.parent / "gateway-status.json"


def _write_gateway_state(state_path: Path, payload: dict[str, object]) -> None:
    """Persist the gateway heartbeat payload atomically."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = state_path.with_suffix(state_path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    temp_path.replace(state_path)


@app.command("gateway-supervisor")
def gateway_supervisor(
    port: int | None = typer.Option(None, "--port", "-p", help="Gateway port"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
    state_path: str | None = typer.Option(
        None,
        "--state-path",
        help="Optional shared heartbeat JSON path used by the GUI to track gateway availability.",
    ),
    heartbeat_interval: int = typer.Option(
        5,
        "--heartbeat-interval",
        min=1,
        help="Seconds between heartbeat refreshes written by the supervisor.",
    ),
):
    """Run the upstream gateway while publishing a shared liveness heartbeat."""
    from nanobot.cli.commands import gateway as upstream_gateway

    config_path = (
        Path(config).expanduser().resolve()
        if config
        else (Path.home() / ".nanobot" / "config.json")
    )
    state_file = _resolve_gateway_state_path(config_path, state_path)
    workspace_value = str(Path(workspace).expanduser()) if workspace else ""
    base_payload: dict[str, object] = {
        "kind": "nanobot_gateway",
        "pid": os.getpid(),
        "config_path": str(config_path),
        "workspace": workspace_value,
        "port": port or 0,
        "started_at": _utc_now(),
    }
    stop_event = threading.Event()

    def publish(state: str, *, error: str = "") -> None:
        payload = {
            **base_payload,
            "state": state,
            "updated_at": _utc_now(),
        }
        if error:
            payload["last_error"] = error
        _write_gateway_state(state_file, payload)

    def heartbeat_loop() -> None:
        while not stop_event.wait(heartbeat_interval):
            publish("running")

    publish("starting")
    thread = threading.Thread(target=heartbeat_loop, name="nanobot-gateway-heartbeat", daemon=True)
    thread.start()
    try:
        publish("running")
        upstream_gateway(
            port=port,
            workspace=workspace,
            verbose=verbose,
            config=str(config_path),
        )
        publish("stopped")
    except KeyboardInterrupt:
        publish("stopped")
        raise
    except BaseException as exc:
        message = str(exc).strip() or exc.__class__.__name__
        publish("error", error=message)
        raise
    finally:
        stop_event.set()
        thread.join(timeout=1)


@app.command()
def gui(
    host: str = typer.Option("127.0.0.1", "--host", help="GUI bind host"),
    port: int = typer.Option(18791, "--port", "-p", help="GUI port"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
    public_url: str | None = typer.Option(None, "--public-url", help="Optional public GUI URL shown in the dashboard"),
    secure_cookies: bool = typer.Option(
        False,
        "--secure-cookies/--insecure-cookies",
        help="Mark GUI session cookies as HTTPS-only (recommended behind TLS).",
    ),
    gateway_health_url: str | None = typer.Option(
        None,
        "--gateway-health-url",
        help="Optional health endpoint used by the dashboard to probe a separate gateway",
    ),
    gateway_state_path: str | None = typer.Option(
        None,
        "--gateway-state-path",
        help="Optional shared heartbeat JSON path used when the gateway does not expose an HTTP health endpoint.",
    ),
    restart_mode: str = typer.Option(
        "",
        "--restart-mode",
        help="Restart strategy for the top-bar button: disabled, self, or command.",
    ),
    restart_command: str | None = typer.Option(
        None,
        "--restart-command",
        help="Command to run when restart is requested. Use with --restart-mode command.",
    ),
    update_check: bool = typer.Option(
        True,
        "--update-check/--no-update-check",
        help="Check GitHub for newer GUI releases at login and at most once per day.",
    ),
    update_repo: str = typer.Option(
        "lucmuss/nanobot-webgui",
        "--update-repo",
        help="GitHub owner/repo used for update checks.",
    ),
    update_mode: str = typer.Option(
        "",
        "--update-mode",
        help="Update strategy for the top-bar banner: disabled or command.",
    ),
    update_command: str | None = typer.Option(
        None,
        "--update-command",
        help="Command to run when a GUI update is requested. Use with --update-mode command.",
    ),
    repair_mode: str = typer.Option(
        "",
        "--repair-mode",
        help="Repair strategy for MCP runtime fixes: disabled or command.",
    ),
    repair_command: str | None = typer.Option(
        None,
        "--repair-command",
        help="Command to run when an MCP repair is requested. Use with --repair-mode command.",
    ),
    community_api_url: str | None = typer.Option(
        None,
        "--community-api-url",
        help="Optional community hub API base URL, for example http://nanobot-community-hub:18811/api/v1",
    ),
    community_public_url: str | None = typer.Option(
        None,
        "--community-public-url",
        help="Optional public community hub URL shown in the GUI, for example https://nanobot-hub.eu",
    ),
    community_api_token: str | None = typer.Option(
        None,
        "--community-api-token",
        help="Optional admin API token for authenticated hub write actions such as publish and moderated submissions.",
    ),
    instance_name: str = typer.Option("nanobot-dev", "--instance-name", help="GUI instance label"),
):
    """Start the nanobot web GUI."""
    import uvicorn
    from rich.console import Console

    from nanobot_webgui.app import GUISettings, create_gui_app

    console = Console()
    config_path = (
        Path(config).expanduser().resolve()
        if config
        else (Path.home() / ".nanobot" / "config.json")
    )
    gateway_url = gateway_health_url or os.getenv("NANOBOT_GUI_GATEWAY_HEALTH_URL")
    gateway_state = gateway_state_path or os.getenv("NANOBOT_GUI_GATEWAY_STATE_PATH")
    public_url_value = (public_url or os.getenv("NANOBOT_GUI_PUBLIC_URL", "")).strip()
    restart_mode_value = (restart_mode or os.getenv("NANOBOT_GUI_RESTART_MODE", "")).strip().lower()
    restart_command_value = (restart_command or os.getenv("NANOBOT_GUI_RESTART_COMMAND", "")).strip()
    update_mode_value = (update_mode or os.getenv("NANOBOT_GUI_UPDATE_MODE", "")).strip().lower()
    update_command_value = (update_command or os.getenv("NANOBOT_GUI_UPDATE_COMMAND", "")).strip()
    update_repo_value = (update_repo or os.getenv("NANOBOT_GUI_UPDATE_REPO", "")).strip()
    repair_mode_value = (repair_mode or os.getenv("NANOBOT_GUI_REPAIR_MODE", "")).strip().lower()
    repair_command_value = (repair_command or os.getenv("NANOBOT_GUI_REPAIR_COMMAND", "")).strip()
    community_api_url_value = (community_api_url or os.getenv("NANOBOT_GUI_COMMUNITY_API_URL", "")).strip()
    community_public_url_value = (
        (community_public_url or os.getenv("NANOBOT_GUI_COMMUNITY_PUBLIC_URL", "")).strip()
        or "https://nanobot-hub.eu"
    )
    community_api_token_value = (community_api_token or os.getenv("NANOBOT_GUI_COMMUNITY_API_TOKEN", "")).strip()

    if not restart_mode_value:
        restart_mode_value = "command" if restart_command_value else "disabled"
    if restart_mode_value not in {"disabled", "self", "command"}:
        console.print("[red]Error: --restart-mode must be one of disabled, self, or command.[/red]")
        raise typer.Exit(1)
    if restart_mode_value == "command" and not restart_command_value:
        console.print("[red]Error: --restart-command is required when --restart-mode command is used.[/red]")
        raise typer.Exit(1)

    if not update_mode_value:
        update_mode_value = "command" if update_command_value else "disabled"
    if update_mode_value not in {"disabled", "command"}:
        console.print("[red]Error: --update-mode must be one of disabled or command.[/red]")
        raise typer.Exit(1)
    if update_mode_value == "command" and not update_command_value:
        console.print("[red]Error: --update-command is required when --update-mode command is used.[/red]")
        raise typer.Exit(1)
    if not repair_mode_value:
        repair_mode_value = "command" if repair_command_value else "disabled"
    if repair_mode_value not in {"disabled", "command"}:
        console.print("[red]Error: --repair-mode must be one of disabled or command.[/red]")
        raise typer.Exit(1)
    if repair_mode_value == "command" and not repair_command_value:
        console.print("[red]Error: --repair-command is required when --repair-mode command is used.[/red]")
        raise typer.Exit(1)

    settings = GUISettings(
        config_path=config_path,
        workspace=workspace,
        host=host,
        port=port,
        instance_name=instance_name,
        public_url=public_url_value or None,
        gateway_health_url=gateway_url,
        gateway_state_path=(gateway_state or "").strip() or None,
        https_only_cookies=secure_cookies,
        restart_mode=restart_mode_value,
        restart_command=restart_command_value or None,
        update_check_enabled=update_check,
        update_repo=update_repo_value,
        update_mode=update_mode_value,
        update_command=update_command_value or None,
        repair_mode=repair_mode_value,
        repair_command=repair_command_value or None,
        community_api_url=community_api_url_value or None,
        community_public_url=community_public_url_value or None,
        community_api_token=community_api_token_value or None,
    )
    app_instance = create_gui_app(settings)

    console.print(f"{__logo__} Starting nanobot GUI on http://{host}:{port}")
    console.print(f"[dim]Config: {config_path}[/dim]")
    if workspace:
        console.print(f"[dim]Workspace: {Path(workspace).expanduser()}[/dim]")
    if gateway_url:
        console.print(f"[dim]Gateway probe: {gateway_url}[/dim]")
    if gateway_state:
        console.print(f"[dim]Gateway heartbeat: {Path(gateway_state).expanduser()}[/dim]")
    if secure_cookies:
        console.print("[dim]Session cookies: HTTPS-only[/dim]")
    if restart_mode_value == "self":
        console.print("[dim]Restart button: GUI self-restart (requires restart policy or supervisor)[/dim]")
    elif restart_mode_value == "command":
        console.print(f"[dim]Restart button: command -> {restart_command_value}[/dim]")
    else:
        console.print("[dim]Restart button: disabled[/dim]")
    if update_check:
        console.print(f"[dim]Update checks: enabled for {update_repo_value or 'no repository'}[/dim]")
    else:
        console.print("[dim]Update checks: disabled[/dim]")
    if update_mode_value == "command":
        console.print(f"[dim]Update action: command -> {update_command_value}[/dim]")
    else:
        console.print("[dim]Update action: disabled[/dim]")
    if repair_mode_value == "command":
        console.print(f"[dim]Repair action: command -> {repair_command_value}[/dim]")
    else:
        console.print("[dim]Repair action: disabled[/dim]")

    uvicorn.run(app_instance, host=host, port=port)


@app.command("repair-worker")
def repair_worker(
    recipe: str = typer.Option("", "--recipe", help="Repair recipe to execute."),
    allow_unrestricted: bool = typer.Option(
        False,
        "--allow-unrestricted",
        help="Allow the unrestricted_agent_shell recipe to execute.",
    ),
    shell_command: str = typer.Option(
        "",
        "--shell-command",
        help="Shell command used only by the unrestricted_agent_shell recipe.",
    ),
    plan_json: str = typer.Option(
        "",
        "--plan-json",
        help="Optional repair plan JSON. Falls back to NANOBOT_REPAIR_PLAN_JSON.",
    ),
):
    """Run one MCP repair recipe from a bounded worker process."""
    from rich.console import Console

    console = Console()
    raw_plan = (plan_json or os.getenv("NANOBOT_REPAIR_PLAN_JSON", "")).strip()
    payload: dict[str, object] = {}
    if raw_plan:
        try:
            parsed = json.loads(raw_plan)
        except json.JSONDecodeError:
            console.print("[red]Error: repair plan JSON is invalid.[/red]")
            raise typer.Exit(1)
        if not isinstance(parsed, dict):
            console.print("[red]Error: repair plan JSON must be an object.[/red]")
            raise typer.Exit(1)
        payload = parsed

    recipe_value = recipe.strip() or str(payload.get("recommended_recipe", "")).strip() or os.getenv("NANOBOT_REPAIR_RECIPE", "").strip()
    shell_value = (
        shell_command.strip()
        or str(payload.get("shell_command", "")).strip()
        or os.getenv("NANOBOT_REPAIR_SHELL_COMMAND", "").strip()
    )
    allow_unrestricted_value = allow_unrestricted or os.getenv("NANOBOT_REPAIR_ALLOW_UNRESTRICTED", "").strip() in {"1", "true", "yes", "on"}

    if not recipe_value:
        console.print("[red]Error: no repair recipe was provided.[/red]")
        raise typer.Exit(1)

    try:
        result = run_repair_recipe(
            recipe_value,
            allow_unrestricted=allow_unrestricted_value,
            shell_command=shell_value,
        )
    except ValueError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(1)

    if result.get("log"):
        console.print(str(result["log"]))
    if result.get("ok"):
        console.print(f"[green]Repair recipe completed:[/green] {recipe_value}")
        raise typer.Exit(0)
    console.print(f"[red]Repair recipe failed:[/red] {result.get('error', 'Unknown error')}")
    raise typer.Exit(1)
