"""FastAPI-powered web GUI for nanobot."""

from __future__ import annotations

import html
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import httpx
from fastapi import BackgroundTasks, FastAPI, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from nanobot import __version__
from nanobot.config.schema import MCPServerConfig
from nanobot.gui.agent_service import GUIAgentService
from nanobot.gui.auth import AdminUser, AuthService
from nanobot.gui.community_service import GUICommunityService
from nanobot.gui.config_service import GUIConfigService
from nanobot.gui.error_utils import explain_error
from nanobot.gui.mcp_service import GUIMCPService, _append_log
from nanobot.gui.repair_worker import REPAIR_RECIPE_DETAILS, supported_repair_recipes
from nanobot.providers.registry import PROVIDERS
from nanobot.utils.helpers import safe_filename


_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


CHANNEL_DEFINITIONS: dict[str, dict[str, Any]] = {
    "none": {
        "label": "No channel yet",
        "description": "Finish provider and agent setup first. You can enable a channel later.",
        "fields": [],
    },
    "telegram": {
        "label": "Telegram",
        "description": "The quickest mobile-friendly starting point.",
        "fields": [
            {"name": "token", "label": "Bot token", "type": "password", "placeholder": "123456:ABCDEF"},
            {"name": "allow_from", "label": "Allow from", "type": "list", "placeholder": "123456789,@username"},
            {"name": "proxy", "label": "Proxy", "type": "text", "placeholder": "socks5://127.0.0.1:1080"},
            {"name": "reply_to_message", "label": "Reply with quote", "type": "bool"},
        ],
    },
    "whatsapp": {
        "label": "WhatsApp",
        "description": "Bridge-based connection through a QR login flow.",
        "fields": [
            {"name": "bridge_url", "label": "Bridge URL", "type": "text", "placeholder": "ws://localhost:3001"},
            {"name": "bridge_token", "label": "Bridge token", "type": "password"},
            {"name": "allow_from", "label": "Allow from", "type": "list", "placeholder": "+49123456789"},
        ],
    },
    "discord": {
        "label": "Discord",
        "description": "Bot token plus optional user or group access rules.",
        "fields": [
            {"name": "token", "label": "Bot token", "type": "password"},
            {"name": "allow_from", "label": "Allow from", "type": "list", "placeholder": "123456789012345678"},
            {"name": "gateway_url", "label": "Gateway URL", "type": "text"},
            {"name": "group_policy", "label": "Group policy", "type": "select", "options": ["mention", "open"]},
        ],
    },
    "slack": {
        "label": "Slack",
        "description": "Socket Mode with bot token and app token.",
        "fields": [
            {"name": "bot_token", "label": "Bot token", "type": "password"},
            {"name": "app_token", "label": "App token", "type": "password"},
            {"name": "allow_from", "label": "Allow from", "type": "list", "placeholder": "U0123456789"},
            {"name": "group_policy", "label": "Group policy", "type": "select", "options": ["mention", "open", "allowlist"]},
        ],
    },
    "matrix": {
        "label": "Matrix",
        "description": "Homeserver, access token, and bot user identity.",
        "fields": [
            {"name": "homeserver", "label": "Homeserver", "type": "text", "placeholder": "https://matrix.org"},
            {"name": "access_token", "label": "Access token", "type": "password"},
            {"name": "user_id", "label": "User ID", "type": "text", "placeholder": "@bot:matrix.org"},
            {"name": "allow_from", "label": "Allow from", "type": "list"},
            {"name": "group_policy", "label": "Group policy", "type": "select", "options": ["open", "mention", "allowlist"]},
        ],
    },
    "email": {
        "label": "Email",
        "description": "IMAP and SMTP automation for inbox workflows.",
        "fields": [
            {"name": "imap_host", "label": "IMAP host", "type": "text"},
            {"name": "imap_username", "label": "IMAP username", "type": "text"},
            {"name": "imap_password", "label": "IMAP password", "type": "password"},
            {"name": "smtp_host", "label": "SMTP host", "type": "text"},
            {"name": "smtp_username", "label": "SMTP username", "type": "text"},
            {"name": "smtp_password", "label": "SMTP password", "type": "password"},
            {"name": "from_address", "label": "From address", "type": "text"},
            {"name": "allow_from", "label": "Allow from", "type": "list"},
        ],
    },
}

CHAT_TEMPLATE_DEFINITIONS: list[dict[str, str]] = [
    {
        "key": "repo_analyze",
        "label": "Analyze Repository",
        "description": "Explain structure, risks, and next steps for a repo URL or local path.",
        "placeholder": "https://github.com/owner/repo or /workspace/project",
        "submit_label": "Analyze",
    },
    {
        "key": "error_explain",
        "label": "Explain Error",
        "description": "Turn a raw error into a plain-language explanation and next actions.",
        "placeholder": "Paste the error message or stack trace headline",
        "submit_label": "Explain",
    },
    {
        "key": "file_summarize",
        "label": "Summarize File",
        "description": "Summarize one file in the workspace and highlight what matters.",
        "placeholder": "uploads/example.log or /workspace/src/app.py",
        "submit_label": "Summarize",
    },
]


@dataclass(slots=True)
class GUISettings:
    """Runtime settings for the GUI instance."""

    config_path: Path
    workspace: str | None = None
    host: str = "127.0.0.1"
    port: int = 18791
    instance_name: str = "nanobot-dev"
    public_url: str | None = None
    gateway_health_url: str | None = None
    https_only_cookies: bool = False
    restart_mode: str = "disabled"
    restart_command: str | None = None
    update_check_enabled: bool = True
    update_repo: str = "lucmuss/nanobot-webgui"
    update_check_interval_hours: int = 24
    update_mode: str = "disabled"
    update_command: str | None = None
    repair_mode: str = "disabled"
    repair_command: str | None = None
    community_api_url: str | None = None
    community_public_url: str | None = None
    community_timeout_seconds: int = 8


def create_gui_app(settings: GUISettings) -> FastAPI:
    """Create the FastAPI app for the nanobot web GUI."""
    config_service = GUIConfigService(settings.config_path, settings.workspace)
    config_service.ensure_instance()

    auth_service = AuthService(
        db_path=config_service.runtime_dir / "gui.sqlite3",
        secret_path=config_service.runtime_dir / "gui-session.secret",
    )
    auth_service.init_db()
    session_secret = auth_service.ensure_session_secret()

    gui_logger = _setup_logger(config_service.runtime_dir / "logs" / "gui.log")
    agent_service = GUIAgentService(config_service, gui_logger)
    mcp_service = GUIMCPService(config_service, gui_logger)
    mcp_service.ai_plan_builder = agent_service.plan_mcp_install
    mcp_service.ai_repair_planner = agent_service.plan_mcp_repair
    community_service = GUICommunityService(
        api_url=settings.community_api_url,
        public_url=settings.community_public_url,
        timeout_seconds=settings.community_timeout_seconds,
    )

    app = FastAPI(title="nanobot GUI", version=__version__)
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret,
        session_cookie="nanobot_gui_session",
        same_site="lax",
        https_only=settings.https_only_cookies,
    )
    app.mount("/media", StaticFiles(directory=str(config_service.media_dir)), name="media")

    app.state.settings = settings
    app.state.config_service = config_service
    app.state.auth_service = auth_service
    app.state.agent_service = agent_service
    app.state.mcp_service = mcp_service
    app.state.community_service = community_service
    app.state.gui_logger = gui_logger

    async def run_agent_health_check() -> dict[str, Any]:
        """Run and persist a provider/agent health check."""
        result = await agent_service.check_runtime()
        config_service.set_agent_health(result)
        if result.get("ok"):
            record_usage(
                source="health_check",
                provider=str(result.get("provider", "")),
                model=str(result.get("model", "")),
                usage=result.get("usage"),
                note="Agent health check",
            )
        return result

    def render_mcp_page(
        request: Request,
        user: AdminUser,
        *,
        config,
        query: str = "",
        preview: dict[str, Any] | None = None,
        error: str | None = None,
        status_code: int = 200,
        manual_form: dict[str, str] | None = None,
    ) -> HTMLResponse:
        """Render the MCP page with merged install metadata."""
        return _render(
            request,
            "mcp.html",
            {
                "title": "MCP",
                "nav_active": "mcp",
                "user": user,
                "query": query,
                "preview": preview,
                "installed_servers": _build_mcp_server_cards(config, config_service),
                "agent_health": config_service.get_agent_health(),
                "error": error,
                "manual_form": manual_form,
            },
            status_code=status_code,
        )

    def store_error(raw_error: str, *, context: str, server_name: str | None = None) -> dict[str, str]:
        """Persist a friendly user-facing error summary for the dashboard and chat."""
        payload = explain_error(raw_error, context=context, server_name=server_name)
        payload["context"] = context
        payload["at"] = _utc_now()
        if server_name:
            payload["server_name"] = server_name
        config_service.set_last_error(payload)
        return payload

    def clear_error() -> None:
        """Clear the stored GUI error after a successful action."""
        config_service.clear_last_error()

    async def resolve_community_match(repo_url: str) -> dict[str, Any] | None:
        """Resolve one repository against the configured community hub."""
        prefs = config_service.get_community_preferences()
        if not prefs.get("receive_recommendations") or not community_service.enabled:
            return None
        try:
            return await community_service.resolve_repository(repo_url)
        except Exception:
            gui_logger.exception("community_repo_resolve_failed repo=%s", repo_url)
            return None

    async def fetch_community_overview() -> dict[str, Any]:
        """Load a compact community overview payload when enabled."""
        prefs = config_service.get_community_preferences()
        if not prefs.get("show_marketplace_stats") or not community_service.enabled:
            return {}
        try:
            return await community_service.overview()
        except Exception:
            gui_logger.exception("community_overview_failed")
            return {}

    async def send_community_telemetry(record: dict[str, Any]) -> None:
        """Send one anonymous MCP runtime event to the community hub when enabled."""
        prefs = config_service.get_community_preferences()
        if not prefs.get("share_anonymous_metrics") or not community_service.enabled:
            return
        community_slug = str(record.get("community_slug", "")).strip()
        if not community_slug:
            return
        try:
            await community_service.ingest_telemetry(
                {
                    "mcp_slug": community_slug,
                    "version": str(record.get("version", "")),
                    "success": str(record.get("status", "")).strip() == "active",
                    "error_code": _community_error_code(str(record.get("last_error", "")).strip()),
                    "latency_ms": 0,
                    "transport": str(record.get("transport", "") or ""),
                    "timeout_bucket": _community_timeout_bucket(record.get("tool_timeout")),
                    "retries": 0,
                    "instance_hash": settings.instance_name,
                    "nanobot_version": __version__,
                }
            )
        except Exception:
            gui_logger.exception("community_telemetry_failed slug=%s", community_slug)

    def record_usage(
        *,
        source: str,
        provider: str,
        model: str,
        usage: dict[str, Any] | None,
        note: str = "",
    ) -> None:
        """Persist one usage event when token data is available."""
        usage = usage or {}
        if not usage:
            return
        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        total_tokens = int(usage.get("total_tokens", 0) or 0)
        if prompt_tokens <= 0 and completion_tokens <= 0 and total_tokens <= 0:
            return
        config_service.record_usage_event(
            {
                "timestamp": _utc_now(),
                "source": source,
                "provider": provider,
                "model": model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "estimated_cost_usd": _estimate_usage_cost(
                    provider=provider,
                    model=model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                ),
                "note": note,
            }
        )

    async def dispatch_chat_message(
        request: Request,
        user: AdminUser,
        message: str,
        *,
        source: str,
        success_flash: str,
        note: str = "",
    ) -> RedirectResponse:
        """Send one main-chat message and persist dashboard and usage state."""
        try:
            result = await agent_service.send_message(user, message)
            clear_error()
            config_service.set_last_successful_chat(
                {
                    "at": _utc_now(),
                    "user_message": message[:240],
                    "assistant_preview": str(result["content"])[:240],
                }
            )
            record_usage(
                source=source,
                provider=str(result.get("provider", "")),
                model=str(result.get("model", "")),
                usage=result.get("usage"),
                note=note,
            )
        except ValueError as exc:
            gui_logger.exception("chat_send_failed user=%s", user.username)
            friendly = store_error(str(exc), context="provider")
            _set_flash(request, friendly["title"] + ": " + friendly["next_action"], level="error")
            return RedirectResponse("/chat", status_code=303)
        except Exception as exc:
            gui_logger.exception("chat_send_failed user=%s", user.username)
            friendly = store_error(str(exc), context="general")
            _set_flash(request, friendly["title"] + ": " + friendly["next_action"], level="error")
            return RedirectResponse("/chat", status_code=303)

        _set_flash(request, success_flash)
        return RedirectResponse("/chat", status_code=303)

    def render_mcp_detail_page(
        request: Request,
        user: AdminUser,
        *,
        config,
        server_name: str,
        community_item: dict[str, Any] | None = None,
        mcp_test_history: list[dict[str, Any]] | None = None,
        error: str | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        """Render the MCP detail and test page."""
        server = config.tools.mcp_servers.get(server_name)
        if server is None:
            return RedirectResponse("/mcp", status_code=303)
        card = _build_mcp_server_card(server_name, server, config_service)
        record = config_service.get_mcp_record(server_name)
        last_error = record.get("friendly_error") if record.get("last_error") else {}
        unrestricted_enabled = config_service.is_unrestricted_agent_shell_enabled()
        repair_preview = {
            "supported": bool(card.get("repair_available_recipes"))
            or (unrestricted_enabled and bool(card.get("last_error"))),
            "recommended_recipe": str(card.get("repair_recipe", "")).strip()
            or (card.get("repair_available_recipes") or ["unrestricted_agent_shell" if unrestricted_enabled and card.get("last_error") else ""])[0],
        }
        community_preferences = config_service.get_community_preferences()
        return _render(
            request,
            "mcp_detail.html",
            {
                "title": f"MCP {server_name}",
                "nav_active": "mcp",
                "user": user,
                "server": card,
                "server_name": server_name,
                "server_form": {
                    "transport": server.type or "",
                    "command": server.command,
                    "args": ", ".join(server.args),
                    "url": server.url,
                    "env_json": json.dumps(server.env or {}, indent=2),
                    "headers_json": json.dumps(server.headers or {}, indent=2),
                    "tool_timeout": server.tool_timeout,
                },
                "env_fields": [
                    {
                        "name": env_name,
                        "value": (server.env or {}).get(env_name, ""),
                        "required": env_name in card.get("required_env", []),
                    }
                    for env_name in [*card.get("required_env", []), *[item for item in card.get("optional_env", []) if item not in card.get("required_env", [])]]
                ],
                "mcp_test_history": mcp_test_history or [],
                "mcp_last_error": last_error,
                "community_item": community_item or {},
                "community_preferences": community_preferences,
                "publish_form": _default_mcp_publish_form(card, user),
                "repair_action": _get_repair_action(settings, repair_preview),
                "repair_recipe_details": REPAIR_RECIPE_DETAILS,
                "error": error,
            },
            status_code=status_code,
        )

    @app.get("/", response_class=HTMLResponse)
    async def root(request: Request):
        if not auth_service.has_admin():
            return RedirectResponse("/setup/admin", status_code=303)

        user = _current_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        if not config_service.is_setup_complete():
            return RedirectResponse("/setup/provider", status_code=303)
        return RedirectResponse("/dashboard", status_code=303)

    @app.get("/health")
    async def health():
        config = config_service.load()
        return JSONResponse(
            {
                "ok": True,
                "version": __version__,
                "instance": settings.instance_name,
                "configPath": str(settings.config_path),
                "workspace": str(config.workspace_path),
                "hasAdmin": auth_service.has_admin(),
                "setupComplete": config_service.is_setup_complete(),
            }
        )

    @app.get("/setup/admin", response_class=HTMLResponse)
    async def setup_admin_page(request: Request):
        user = _current_admin(request, auth_service)
        if auth_service.has_admin():
            if user is not None:
                if config_service.is_setup_complete():
                    return RedirectResponse("/dashboard", status_code=303)
                return RedirectResponse("/setup/provider", status_code=303)
            return RedirectResponse("/login", status_code=303)

        return _render(
            request,
            "setup_admin.html",
            {
                "title": "Create Admin",
                "hide_shell": True,
            },
        )

    @app.post("/setup/admin", response_class=HTMLResponse)
    async def setup_admin_submit(request: Request):
        if auth_service.has_admin():
            return RedirectResponse("/login", status_code=303)

        form = await request.form()
        username = str(form.get("username", "")).strip()
        email = str(form.get("email", "")).strip()
        password = str(form.get("password", ""))
        password_confirm = str(form.get("password_confirm", ""))

        if password != password_confirm:
            return _render(
                request,
                "setup_admin.html",
                {
                    "title": "Create Admin",
                    "hide_shell": True,
                    "error": "Passwords do not match.",
                    "form_data": {"username": username, "email": email},
                },
                status_code=400,
            )

        try:
            admin = auth_service.create_admin(username=username, email=email, password=password)
        except ValueError as exc:
            return _render(
                request,
                "setup_admin.html",
                {
                    "title": "Create Admin",
                    "hide_shell": True,
                    "error": str(exc),
                    "form_data": {"username": username, "email": email},
                },
                status_code=400,
            )

        config_service.set_setup_complete(False)
        request.session["admin_id"] = admin.id
        gui_logger.info("admin_created username=%s email=%s", admin.username, admin.email)
        _set_flash(request, "Admin account created. Continue with the setup wizard.")
        return RedirectResponse("/setup/provider", status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if not auth_service.has_admin():
            return RedirectResponse("/setup/admin", status_code=303)

        user = _current_admin(request, auth_service)
        if user is not None:
            if config_service.is_setup_complete():
                return RedirectResponse("/dashboard", status_code=303)
            return RedirectResponse("/setup/provider", status_code=303)

        return _render(
            request,
            "login.html",
            {
                "title": "Sign In",
                "hide_shell": True,
            },
        )

    @app.post("/login", response_class=HTMLResponse)
    async def login_submit(request: Request):
        if not auth_service.has_admin():
            return RedirectResponse("/setup/admin", status_code=303)

        form = await request.form()
        identifier = str(form.get("identifier", "")).strip()
        password = str(form.get("password", ""))

        admin = auth_service.authenticate(identifier, password)
        if admin is None:
            return _render(
                request,
                "login.html",
                {
                    "title": "Sign In",
                    "hide_shell": True,
                    "error": "Sign-in failed. Check your username/email and password.",
                    "form_data": {"identifier": identifier},
                },
                status_code=401,
            )

        request.session["admin_id"] = admin.id
        gui_logger.info("login_success username=%s", admin.username)
        _ensure_update_status(settings, config_service, gui_logger, user_present=True)
        if config_service.is_setup_complete():
            return RedirectResponse("/dashboard", status_code=303)
        return RedirectResponse("/setup/provider", status_code=303)

    @app.post("/logout")
    async def logout(request: Request):
        user = _current_admin(request, auth_service)
        if user is not None:
            gui_logger.info("logout username=%s", user.username)
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.post("/actions/restart", response_class=HTMLResponse)
    async def restart_instance(request: Request, background_tasks: BackgroundTasks):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        form = await request.form()
        next_url = str(form.get("next", request.url.path)).strip() or "/status"
        restart_action = _get_restart_action(settings)
        if not restart_action["enabled"]:
            _set_flash(request, restart_action["description"], level="error")
            return RedirectResponse(next_url, status_code=303)

        config_service.set_last_restart_at(_utc_now())
        gui_logger.warning(
            "instance_restart_requested by=%s mode=%s",
            user.username,
            restart_action["mode"],
        )
        if restart_action["mode"] == "command":
            background_tasks.add_task(
                _run_restart_command,
                str(restart_action["command"]),
                gui_logger,
            )
            title = "Restart requested"
            message = "The configured restart action is running now. This page will keep checking for the GUI to come back."
        else:
            background_tasks.add_task(_restart_process, gui_logger)
            title = "Restarting GUI"
            message = "The nanobot GUI process is restarting now. This page will keep checking for the GUI to come back."
        return _render_reconnect_page(title=title, message=message, redirect_url="/", status_code=202)

    @app.post("/actions/update", response_class=HTMLResponse)
    async def update_instance(request: Request, background_tasks: BackgroundTasks):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        update_status = _ensure_update_status(settings, config_service, gui_logger)
        update_action = _get_update_action(settings, update_status)
        if not update_action["enabled"]:
            _set_flash(request, update_action["description"], level="error")
            return RedirectResponse("/dashboard", status_code=303)
        if not update_status.get("available"):
            _set_flash(request, "No newer GUI version is available right now.", level="info")
            return RedirectResponse("/dashboard", status_code=303)

        requested_at = _utc_now()
        config_service.set_update_status(
            {
                **update_status,
                "enabled": True,
                "updating": True,
                "last_update_request_at": requested_at,
                "last_update_error": "",
            }
        )
        gui_logger.warning(
            "instance_update_requested by=%s repo=%s target=%s mode=%s",
            user.username,
            update_status.get("repo", ""),
            update_status.get("tag_name") or update_status.get("latest_version", ""),
            update_action["mode"],
        )
        background_tasks.add_task(
            _run_update_command,
            str(update_action["command"]),
            gui_logger,
            config_service,
            str(update_status.get("latest_version", "")),
        )
        return _render_reconnect_page(
            title="Updating GUI",
            message="The configured update action is running now. This page will keep checking for the GUI to come back.",
            redirect_url="/",
            status_code=202,
        )

    @app.post("/actions/test-agent")
    async def test_agent_runtime(request: Request):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        form = await request.form()
        next_url = str(form.get("next", "/mcp")).strip() or "/mcp"
        result = await run_agent_health_check()
        if result["ok"]:
            clear_error()
            _set_flash(
                request,
                f"Agent runtime is healthy on {result['provider']} / {result['model']} ({result['latency_ms']} ms).",
            )
        else:
            store_error(str(result.get("error", "Unknown error")), context="provider")
            _set_flash(
                request,
                "Agent runtime check failed: " + str(result.get("error", "Unknown error")),
                level="error",
            )
        return RedirectResponse(next_url, status_code=303)

    @app.post("/actions/toggle-safe-mode")
    async def toggle_safe_mode(request: Request):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        form = await request.form()
        next_url = str(form.get("next", "/dashboard")).strip() or "/dashboard"
        next_value = not config_service.is_safe_mode()
        config_service.set_safe_mode(next_value)
        gui_logger.info("safe_mode_toggled by=%s enabled=%s", user.username, next_value)
        _set_flash(
            request,
            f"Safe Mode {'enabled' if next_value else 'disabled'}.",
        )
        return RedirectResponse(next_url, status_code=303)

    @app.get("/profile", response_class=HTMLResponse)
    async def profile_page(request: Request):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        return _render(
            request,
            "profile.html",
            {
                "title": "Profile",
                "nav_active": "profile",
                "user": user,
                "profile_form": {
                    "username": user.username,
                    "email": user.email,
                    "display_name": user.display_name,
                },
            },
        )

    @app.post("/profile", response_class=HTMLResponse)
    async def profile_submit(request: Request):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        form = await request.form()
        username = str(form.get("username", "")).strip()
        email = str(form.get("email", "")).strip()
        display_name = str(form.get("display_name", "")).strip()
        password = str(form.get("password", ""))
        password_confirm = str(form.get("password_confirm", ""))
        avatar_value = form.get("avatar")

        if password and password != password_confirm:
            return _render(
                request,
                "profile.html",
                {
                    "title": "Profile",
                    "nav_active": "profile",
                    "user": user,
                    "error": "Passwords do not match.",
                    "profile_form": {
                        "username": username,
                        "email": email,
                        "display_name": display_name,
                    },
                },
                status_code=400,
            )

        avatar_path = None
        if getattr(avatar_value, "filename", "") and getattr(avatar_value, "file", None):
            try:
                avatar_path = _store_avatar(avatar_value, config_service.avatars_dir)
            except ValueError as exc:
                return _render(
                    request,
                    "profile.html",
                    {
                        "title": "Profile",
                        "nav_active": "profile",
                        "user": user,
                        "error": str(exc),
                        "profile_form": {
                            "username": username,
                            "email": email,
                            "display_name": display_name,
                        },
                    },
                    status_code=400,
                )

        try:
            updated_user = auth_service.update_admin(
                user.id,
                username=username,
                email=email,
                display_name=display_name,
                password=password or None,
                avatar_path=avatar_path,
            )
        except ValueError as exc:
            return _render(
                request,
                "profile.html",
                {
                    "title": "Profile",
                    "nav_active": "profile",
                    "user": user,
                    "error": str(exc),
                    "profile_form": {
                        "username": username,
                        "email": email,
                        "display_name": display_name,
                    },
                },
                status_code=400,
            )

        request.session["admin_id"] = updated_user.id
        gui_logger.info("profile_updated username=%s", updated_user.username)
        _set_flash(request, "Profile updated.")
        return RedirectResponse("/profile", status_code=303)

    @app.get("/setup/provider", response_class=HTMLResponse)
    async def setup_provider_page(request: Request):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        config = config_service.load()
        provider_options = [
            {"value": spec.name, "label": spec.label, "oauth": spec.is_oauth}
            for spec in PROVIDERS
        ]
        selected_provider = config.agents.defaults.provider
        if selected_provider == "auto":
            selected_provider = config.get_provider_name() or "openrouter"
        provider_cfg = getattr(config.providers, selected_provider, None)

        return _render(
            request,
            "setup_provider.html",
            {
                "title": "Provider",
                "nav_active": "provider",
                "user": user,
                "provider_options": provider_options,
                "selected_provider": selected_provider,
                "model": config.agents.defaults.model,
                "api_key": provider_cfg.api_key if provider_cfg else "",
                "api_base": provider_cfg.api_base if provider_cfg and provider_cfg.api_base else "",
                "extra_headers": json.dumps(provider_cfg.extra_headers or {}, indent=2) if provider_cfg else "{}",
                "safe_mode": config_service.is_safe_mode(),
            },
        )

    @app.post("/setup/provider", response_class=HTMLResponse)
    async def setup_provider_submit(request: Request):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        form = await request.form()
        action = str(form.get("action", "next")).strip() or "next"
        provider_name = str(form.get("provider", "")).strip()
        model = str(form.get("model", "")).strip()
        api_key = str(form.get("api_key", "")).strip()
        api_base = str(form.get("api_base", "")).strip()
        extra_headers_raw = str(form.get("extra_headers", "")).strip() or "{}"

        if provider_name not in {spec.name for spec in PROVIDERS}:
            _set_flash(request, "Choose a valid provider.", level="error")
            return RedirectResponse("/setup/provider", status_code=303)

        if not model:
            provider_options = [
                {"value": spec.name, "label": spec.label, "oauth": spec.is_oauth}
                for spec in PROVIDERS
            ]
            return _render(
                request,
                "setup_provider.html",
                {
                    "title": "Provider",
                    "nav_active": "provider",
                    "user": user,
                    "provider_options": provider_options,
                    "selected_provider": provider_name,
                    "model": model,
                    "api_key": api_key,
                    "api_base": api_base,
                    "extra_headers": extra_headers_raw,
                    "error": "Model is required.",
                    "safe_mode": config_service.is_safe_mode(),
                },
                status_code=400,
            )

        try:
            extra_headers = _parse_json_object(extra_headers_raw, field_name="Extra headers")
        except ValueError as exc:
            provider_options = [
                {"value": spec.name, "label": spec.label, "oauth": spec.is_oauth}
                for spec in PROVIDERS
            ]
            return _render(
                request,
                "setup_provider.html",
                {
                    "title": "Provider",
                    "nav_active": "provider",
                    "user": user,
                    "provider_options": provider_options,
                    "selected_provider": provider_name,
                    "model": model,
                    "api_key": api_key,
                    "api_base": api_base,
                    "extra_headers": extra_headers_raw,
                    "error": str(exc),
                    "safe_mode": config_service.is_safe_mode(),
                },
                status_code=400,
            )

        config = config_service.load()
        config.agents.defaults.provider = provider_name
        if model:
            config.agents.defaults.model = model

        provider_cfg = getattr(config.providers, provider_name)
        provider_spec = next((spec for spec in PROVIDERS if spec.name == provider_name), None)
        if not (provider_spec and provider_spec.is_oauth):
            provider_cfg.api_key = api_key
        provider_cfg.api_base = api_base or None
        provider_cfg.extra_headers = extra_headers or None
        config_service.save(config)
        config_service.set_setup_complete(False)
        agent_service.invalidate()
        gui_logger.info("provider_saved by=%s provider=%s", user.username, provider_name)

        if action == "save":
            _set_flash(request, "Provider settings saved.")
            return RedirectResponse("/setup/provider", status_code=303)
        return RedirectResponse("/setup/channel", status_code=303)

    @app.get("/partials/channel-fields", response_class=HTMLResponse)
    async def channel_fields_partial(request: Request, channel: str = Query("none")):
        user = _require_admin(request, auth_service)
        if user is None:
            return HTMLResponse("", status_code=401)

        config = config_service.load()
        selected = channel if channel in CHANNEL_DEFINITIONS else "none"
        values = _channel_values(config, selected)
        return _render(
            request,
            "partials/channel_fields.html",
            {
                "channel_name": selected,
                "channel_meta": CHANNEL_DEFINITIONS[selected],
                "channel_values": values,
                "hide_shell": True,
            },
        )

    @app.get("/setup/channel", response_class=HTMLResponse)
    async def setup_channel_page(request: Request):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        config = config_service.load()
        selected = _selected_channel(config)
        return _render(
            request,
            "setup_channel.html",
            {
                "title": "Channel",
                "nav_active": "channel",
                "user": user,
                "channel_definitions": CHANNEL_DEFINITIONS,
                "selected_channel": selected,
                "channel_meta": CHANNEL_DEFINITIONS[selected],
                "channel_values": _channel_values(config, selected),
                "send_progress": config.channels.send_progress,
                "send_tool_hints": config.channels.send_tool_hints,
            },
        )

    @app.post("/setup/channel", response_class=HTMLResponse)
    async def setup_channel_submit(request: Request):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        form = await request.form()
        action = str(form.get("action", "next")).strip() or "next"
        selected = str(form.get("channel", "none")).strip()
        selected = selected if selected in CHANNEL_DEFINITIONS else "none"

        config = config_service.load()
        config.channels.send_progress = bool(form.get("send_progress"))
        config.channels.send_tool_hints = bool(form.get("send_tool_hints"))

        for channel_name in CHANNEL_DEFINITIONS:
            if channel_name == "none":
                continue
            getattr(config.channels, channel_name).enabled = False

        if selected != "none":
            channel_cfg = getattr(config.channels, selected)
            channel_cfg.enabled = True
            for field in CHANNEL_DEFINITIONS[selected]["fields"]:
                raw_value = form.get(field["name"])
                value = _coerce_field_value(raw_value, field["type"])
                setattr(channel_cfg, field["name"], value)

        config_service.save(config)
        config_service.set_setup_complete(False)
        agent_service.invalidate()
        gui_logger.info("channel_saved by=%s channel=%s", user.username, selected)

        if action == "save":
            _set_flash(request, "Channel settings saved.")
            return RedirectResponse("/setup/channel", status_code=303)
        return RedirectResponse("/setup/agent", status_code=303)

    @app.get("/setup/agent", response_class=HTMLResponse)
    async def setup_agent_page(request: Request):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        config = config_service.load()
        agent_doc = config_service.read_markdown_document("agents")
        soul_doc = config_service.read_markdown_document("soul")
        user_doc = config_service.read_markdown_document("user")
        return _render(
            request,
            "setup_agent.html",
            {
                "title": "Agent",
                "nav_active": "agent",
                "user": user,
                "config": config,
                "workspace": str(config.workspace_path),
                "instruction_content": agent_doc["content"],
                "instruction_path": agent_doc["path"],
                "instruction_modified_at": agent_doc["modified_at"],
                "soul_path": soul_doc["path"],
                "user_path": user_doc["path"],
                "response_style": config_service.get_response_style(),
                "mcp_servers": _build_mcp_server_cards(config, config_service),
                "workspace_locked": bool(config_service.workspace_override),
                "safe_mode": config_service.is_safe_mode(),
            },
        )

    @app.post("/setup/agent", response_class=HTMLResponse)
    async def setup_agent_submit(request: Request):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        form = await request.form()
        action = str(form.get("action", "finish")).strip() or "finish"
        config = config_service.load()
        defaults = config.agents.defaults

        try:
            if not config_service.workspace_override:
                defaults.workspace = str(form.get("workspace", str(config_service.default_workspace))).strip()
            defaults.model = str(form.get("model", defaults.model)).strip() or defaults.model
            defaults.provider = str(form.get("provider", defaults.provider)).strip() or defaults.provider
            defaults.max_tokens = _form_int(form.get("max_tokens"), defaults.max_tokens)
            defaults.temperature = _form_float(form.get("temperature"), defaults.temperature)
            defaults.max_tool_iterations = _form_int(form.get("max_tool_iterations"), defaults.max_tool_iterations)
            defaults.memory_window = _form_int(form.get("memory_window"), defaults.memory_window)
        except ValueError as exc:
            agent_doc = config_service.read_markdown_document("agents")
            soul_doc = config_service.read_markdown_document("soul")
            user_doc = config_service.read_markdown_document("user")
            return _render(
                request,
                "setup_agent.html",
                {
                    "title": "Agent",
                    "nav_active": "agent",
                    "user": user,
                    "config": config,
                    "workspace": str(form.get("workspace", str(config.workspace_path))),
                    "error": str(exc),
                    "instruction_content": str(form.get("instruction_content", agent_doc["content"])),
                    "instruction_path": agent_doc["path"],
                    "instruction_modified_at": agent_doc["modified_at"],
                    "soul_path": soul_doc["path"],
                    "user_path": user_doc["path"],
                    "response_style": str(form.get("response_style", config_service.get_response_style())),
                    "mcp_servers": _build_mcp_server_cards(config, config_service),
                    "workspace_locked": bool(config_service.workspace_override),
                    "safe_mode": config_service.is_safe_mode(),
                },
                status_code=400,
            )

        reasoning_effort = str(form.get("reasoning_effort", "")).strip()
        defaults.reasoning_effort = reasoning_effort or None
        config.tools.enabled = bool(form.get("tools_enabled"))
        config.tools.restrict_to_workspace = bool(form.get("restrict_to_workspace"))
        instruction_content = str(form.get("instruction_content", ""))
        response_style = str(form.get("response_style", "adaptive")).strip() or "adaptive"
        selected_mcp = set(form.getlist("enabled_mcp"))

        config_service.save(config)
        config_service.save_markdown_document("agents", instruction_content)
        config_service.set_response_style(response_style)
        for name in config.tools.mcp_servers:
            config_service.set_mcp_enabled(name, name in selected_mcp)
        config_service.set_setup_complete(action == "finish")
        agent_service.invalidate()
        gui_logger.info("agent_saved by=%s finish=%s", user.username, action == "finish")

        if action == "save":
            _set_flash(request, "Agent settings saved.")
            return RedirectResponse("/setup/agent", status_code=303)

        _set_flash(request, "Setup complete. Welcome to the dashboard.")
        return RedirectResponse("/dashboard", status_code=303)

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard_page(request: Request):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        if not config_service.is_setup_complete():
            return RedirectResponse("/setup/provider", status_code=303)

        config = config_service.load()
        agent_health = config_service.get_agent_health()
        last_error = config_service.get_last_error()
        last_chat = config_service.get_last_successful_chat()
        last_mcp_test = config_service.get_last_mcp_test()
        active_memory_key = config_service.get_active_memory_doc()
        enabled_channels = [
            name
            for name in CHANNEL_DEFINITIONS
            if name != "none" and getattr(config.channels, name, None) and getattr(config.channels, name).enabled
        ]
        provider_name = config.get_provider_name() or config.agents.defaults.provider
        gateway_status = await _probe_gateway(settings.gateway_health_url)
        sessions = await agent_service.list_sessions()
        installed_servers = _build_mcp_server_cards(config, config_service)
        enabled_mcp_count = sum(1 for server in installed_servers if server["enabled"])
        runtime_status = _build_runtime_status(
            agent_health=agent_health,
            gateway_status=gateway_status,
            last_restart_at=config_service.get_last_restart_at(),
        )
        validation_results = await _validate_setup(
            config=config,
            config_service=config_service,
            gateway_health=gateway_status,
            agent_health=agent_health,
        )
        community_preferences = config_service.get_community_preferences()
        community_overview = await fetch_community_overview()
        setup_progress = _build_setup_progress(
            config=config,
            agent_health=agent_health,
            installed_servers=installed_servers,
            enabled_channels=enabled_channels,
        )
        next_step = _determine_next_step(setup_progress)
        config_status = [
            {"label": "Provider set", "ok": bool(provider_name and provider_name != "auto"), "value": provider_name or "Not set"},
            {"label": "Model set", "ok": bool(config.agents.defaults.model.strip()), "value": config.agents.defaults.model},
            {"label": "API key present", "ok": _provider_has_credentials(config, provider_name), "value": "Present" if _provider_has_credentials(config, provider_name) else "Missing"},
            {"label": "Agent ready", "ok": bool(agent_health.get("ok")), "value": "Ready" if agent_health.get("ok") else "Not ready"},
        ]
        metrics = [
            {"label": "Runtime", "value": runtime_status["label"], "tone": runtime_status["tone"], "href": "/status"},
            {"label": "Provider", "value": provider_name or "Not set", "tone": "neutral", "href": "/setup/provider"},
            {
                "label": "Agent",
                "value": "Healthy" if agent_health.get("ok") else "Not verified" if not agent_health else "Failed",
                "tone": "good" if agent_health.get("ok") else "muted" if not agent_health else "bad",
                "href": "/status",
            },
            {
                "label": "MCP",
                "value": f"{len(installed_servers)} installed / {enabled_mcp_count} enabled",
                "tone": "neutral",
                "href": "/mcp",
            },
            {"label": "Channels", "value": str(len(enabled_channels)), "tone": "neutral", "href": "/setup/channel"},
            {"label": "Saved Sessions", "value": str(len(sessions)), "tone": "neutral", "href": "/history"},
            {
                "label": "Tokens",
                "value": str((agent_health.get("usage") or {}).get("total_tokens", "n/a")),
                "tone": "neutral",
                "href": "/usage",
                "hint": "last health check",
            },
            {"label": "Gateway", "value": gateway_status["label"], "tone": gateway_status["tone"], "href": "/status"},
        ]

        return _render(
            request,
            "dashboard.html",
            {
                "title": "Dashboard",
                "nav_active": "dashboard",
                "user": user,
                "config": config,
                "metrics": metrics,
                "config_status": config_status,
                "enabled_channels": enabled_channels,
                "gateway_status": gateway_status,
                "installed_servers": installed_servers,
                "agent_health": agent_health,
                "runtime_status": runtime_status,
                "last_successful_chat": last_chat,
                "last_error": last_error,
                "last_mcp_test": last_mcp_test,
                "active_memory_document": config_service.get_markdown_document(active_memory_key),
                "setup_progress": setup_progress,
                "next_step": next_step,
                "validation_results": validation_results,
                "activity_feed": _build_activity_feed(config_service, settings),
                "community_overview": community_overview,
                "community_preferences": community_preferences,
                "quick_actions": [
                    {"label": "Open Chat", "href": "/chat"},
                    {"label": "Add MCP", "href": "/mcp"},
                    {"label": "Discover MCP", "href": "/community/discover"},
                    {"label": "Save Agent", "href": "/setup/agent"},
                    {"label": "Open Validator", "href": "/settings"},
                    {"label": "Open Logs", "href": "/logs"},
                ],
            },
        )

    @app.get("/mcp", response_class=HTMLResponse)
    async def mcp_page(request: Request, q: str = Query("")):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        if not config_service.is_setup_complete():
            return RedirectResponse("/setup/provider", status_code=303)

        config = config_service.load()
        return render_mcp_page(request, user, config=config, query=q.strip())

    @app.get("/community/discover", response_class=HTMLResponse)
    async def community_discover_page(
        request: Request,
        q: str = Query(""),
        category: str = Query(""),
        sort: str = Query("trending"),
    ):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        payload: dict[str, Any] = {}
        error = ""
        if community_service.enabled:
            try:
                payload = await community_service.marketplace(query=q.strip(), category=category.strip(), sort=sort.strip())
            except Exception as exc:
                gui_logger.exception("community_marketplace_failed")
                error = str(exc)
        else:
            error = "Community hub is not configured for this GUI instance."

        overview = await fetch_community_overview()
        return _render(
            request,
            "community_discover.html",
            {
                "title": "Discover MCP",
                "nav_active": "community-discover",
                "user": user,
                "query": q.strip(),
                "category": category.strip(),
                "sort": sort.strip() or "trending",
                "community_items": payload.get("items", []),
                "community_overview": overview,
                "community_error": error,
                "community_preferences": config_service.get_community_preferences(),
                "submission_form": {
                    "repo_url": q.strip() if q.strip().startswith("http") else "",
                    "name": "",
                    "description": "",
                    "category": "",
                    "tags": "",
                },
            },
        )

    @app.post("/community/submit/mcp")
    async def community_submit_mcp(request: Request):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        prefs = config_service.get_community_preferences()
        if not prefs.get("allow_public_mcp_submissions"):
            _set_flash(request, "Enable MCP publishing in Settings before submitting to the community hub.", level="error")
            return RedirectResponse("/settings", status_code=303)
        if not community_service.enabled:
            _set_flash(request, "Community hub is not configured for this GUI instance.", level="error")
            return RedirectResponse("/community/discover", status_code=303)

        form = await request.form()
        payload = {
            "repo_url": str(form.get("repo_url", "")).strip(),
            "name": str(form.get("name", "")).strip(),
            "description": str(form.get("description", "")).strip(),
            "category": str(form.get("category", "")).strip(),
            "tags": _split_list(str(form.get("tags", "")).strip()),
            "submitted_by": user.username,
            "source_instance": settings.instance_name,
            "source_public_url": settings.public_url or "",
        }
        try:
            result = await community_service.submit_mcp(payload)
        except Exception as exc:
            gui_logger.exception("community_submit_failed repo=%s", payload["repo_url"])
            _set_flash(request, f"Community submission failed: {exc}", level="error")
            return RedirectResponse("/community/discover", status_code=303)

        item = result.get("item") if isinstance(result.get("item"), dict) else {}
        if item.get("slug"):
            if result.get("created"):
                _set_flash(request, f"Published '{item.get('name', item['slug'])}' to the Community Hub.")
            else:
                _set_flash(request, f"That repository is already tracked. Opened '{item.get('name', item['slug'])}'.")
            return RedirectResponse(f"/community/mcp/{item['slug']}", status_code=303)

        _set_flash(request, "Community submission finished, but no marketplace entry was returned.", level="error")
        return RedirectResponse("/community/discover", status_code=303)

    @app.get("/community/mcp/{slug}", response_class=HTMLResponse)
    async def community_mcp_detail_page(request: Request, slug: str):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        if not community_service.enabled:
            return _render(
                request,
                "community_mcp_detail.html",
                {
                    "title": "Community MCP",
                    "nav_active": "community-discover",
                    "user": user,
                    "community_error": "Community hub is not configured for this GUI instance.",
                    "community_item": {},
                },
                status_code=503,
            )

        try:
            item = await community_service.marketplace_detail(slug)
        except Exception as exc:
            gui_logger.exception("community_marketplace_detail_failed slug=%s", slug)
            return _render(
                request,
                "community_mcp_detail.html",
                {
                    "title": "Community MCP",
                    "nav_active": "community-discover",
                    "user": user,
                    "community_error": str(exc),
                    "community_item": {},
                },
                status_code=502,
            )

        return _render(
            request,
            "community_mcp_detail.html",
            {
                "title": item.get("name", slug),
                "nav_active": "community-discover",
                "user": user,
                "community_error": "",
                "community_item": item,
            },
        )

    @app.get("/community/stacks", response_class=HTMLResponse)
    async def community_stacks_page(request: Request, q: str = Query("")):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        payload: dict[str, Any] = {}
        error = ""
        if community_service.enabled:
            try:
                payload = await community_service.stacks(query=q.strip())
            except Exception as exc:
                gui_logger.exception("community_stacks_failed")
                error = str(exc)
        else:
            error = "Community hub is not configured for this GUI instance."

        return _render(
            request,
            "community_stacks.html",
            {
                "title": "MCP Stacks",
                "nav_active": "community-stacks",
                "user": user,
                "query": q.strip(),
                "community_items": payload.get("items", []),
                "community_error": error,
            },
        )

    @app.get("/community/stacks/{slug}", response_class=HTMLResponse)
    async def community_stack_detail_page(request: Request, slug: str):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        if not community_service.enabled:
            return _render(
                request,
                "community_stack_detail.html",
                {
                    "title": "Stack",
                    "nav_active": "community-stacks",
                    "user": user,
                    "community_error": "Community hub is not configured for this GUI instance.",
                    "community_item": {},
                },
                status_code=503,
            )

        try:
            item = await community_service.stack_detail(slug)
        except Exception as exc:
            gui_logger.exception("community_stack_detail_failed slug=%s", slug)
            return _render(
                request,
                "community_stack_detail.html",
                {
                    "title": "Stack",
                    "nav_active": "community-stacks",
                    "user": user,
                    "community_error": str(exc),
                    "community_item": {},
                },
                status_code=502,
            )

        return _render(
            request,
            "community_stack_detail.html",
            {
                "title": item.get("title", slug),
                "nav_active": "community-stacks",
                "user": user,
                "community_error": "",
                "community_item": item,
            },
        )

    @app.get("/community/showcase", response_class=HTMLResponse)
    async def community_showcase_page(
        request: Request,
        q: str = Query(""),
        category: str = Query(""),
    ):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        payload: dict[str, Any] = {}
        error = ""
        if community_service.enabled:
            try:
                payload = await community_service.showcase(query=q.strip(), category=category.strip())
            except Exception as exc:
                gui_logger.exception("community_showcase_failed")
                error = str(exc)
        else:
            error = "Community hub is not configured for this GUI instance."

        return _render(
            request,
            "community_showcase.html",
            {
                "title": "Showcase",
                "nav_active": "community-showcase",
                "user": user,
                "query": q.strip(),
                "category": category.strip(),
                "community_items": payload.get("items", []),
                "community_error": error,
            },
        )

    @app.get("/community/stats", response_class=HTMLResponse)
    async def community_stats_page(request: Request):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        overview = await fetch_community_overview()
        error = ""
        if not overview and community_service.enabled:
            error = "Community stats are temporarily unavailable."
        elif not community_service.enabled:
            error = "Community hub is not configured for this GUI instance."

        return _render(
            request,
            "community_stats.html",
            {
                "title": "Community Stats",
                "nav_active": "community-stats",
                "user": user,
                "community_overview": overview,
                "community_error": error,
            },
        )

    @app.post("/mcp/analyze", response_class=HTMLResponse)
    async def mcp_analyze(request: Request):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        if not config_service.is_setup_complete():
            return RedirectResponse("/setup/provider", status_code=303)

        form = await request.form()
        query = str(form.get("source", "")).strip()
        config = config_service.load()
        agent_health = await run_agent_health_check()
        if not agent_health["ok"]:
            store_error(str(agent_health.get("error", "Unknown error")), context="provider")
            return render_mcp_page(
                request,
                user,
                config=config,
                query=query,
                error="Analyze is blocked until the configured agent runtime responds successfully. Fix the provider settings and run the health check again.",
                status_code=400,
            )

        try:
            preview = await mcp_service.analyze_repository(query, allow_ai_fallback=True)
            preview["community_match"] = await resolve_community_match(str(preview.get("repo_url", query)))
        except ValueError as exc:
            store_error(str(exc), context="mcp")
            return render_mcp_page(
                request,
                user,
                config=config,
                query=query,
                error=str(exc),
                status_code=400,
            )
        except Exception:
            gui_logger.exception("mcp_analyze_failed by=%s source=%s", user.username, query)
            store_error("MCP analysis failed while inspecting the repository.", context="mcp")
            return render_mcp_page(
                request,
                user,
                config=config,
                query=query,
                error="MCP analysis failed. Check the repository URL and GUI logs.",
                status_code=500,
            )

        gui_logger.info("mcp_analyzed by=%s source=%s", user.username, query)
        clear_error()
        return render_mcp_page(request, user, config=config, query=query, preview=preview)

    @app.post("/mcp/install")
    async def mcp_install(request: Request):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        if not config_service.is_setup_complete():
            return RedirectResponse("/setup/provider", status_code=303)

        form = await request.form()
        query = str(form.get("source", "")).strip()
        agent_health = await run_agent_health_check()
        if not agent_health["ok"]:
            config = config_service.load()
            store_error(str(agent_health.get("error", "Unknown error")), context="provider")
            return render_mcp_page(
                request,
                user,
                config=config,
                query=query,
                error="Install is blocked until the configured agent runtime responds successfully. Fix the provider settings and run the health check again.",
                status_code=400,
            )

        try:
            record = await mcp_service.install_repository(query, allow_ai_fallback=True)
            community_match = await resolve_community_match(str(record.get("repo_url", query)))
            if community_match:
                record["community_slug"] = community_match.get("slug", "")
                record["community_match"] = community_match
                config_service.set_mcp_record(record["server_name"], record)
        except ValueError as exc:
            config = config_service.load()
            store_error(str(exc), context="mcp")
            return render_mcp_page(
                request,
                user,
                config=config,
                query=query,
                error=str(exc),
                status_code=400,
            )
        except Exception:
            gui_logger.exception("mcp_install_failed by=%s source=%s", user.username, query)
            config = config_service.load()
            store_error("MCP installation failed while building the repository.", context="mcp")
            return render_mcp_page(
                request,
                user,
                config=config,
                query=query,
                error="MCP installation failed. Check the build output in the logs page.",
                status_code=500,
            )

        agent_service.invalidate()
        config_service.set_last_mcp_test(
            {
                "server_name": record["server_name"],
                "status": record["status"],
                "status_label": record["status_label"],
                "checked_at": record.get("last_checked_at", ""),
            }
        )
        if record["status"] == "active":
            await send_community_telemetry(record)
            clear_error()
            _set_flash(
                request,
                f"MCP server '{record['server_name']}' installed and verified. Enable it for chat when you are ready.",
            )
            return RedirectResponse("/mcp", status_code=303)
        elif record["status"] == "needs_configuration":
            friendly = store_error(record["last_error"], context="mcp", server_name=record["server_name"])
            record["friendly_error"] = friendly
            config_service.set_mcp_record(record["server_name"], record)
            _set_flash(
                request,
                f"MCP server '{record['server_name']}' installed, but it still needs configuration: {record['last_error']}",
                level="error",
            )
            return RedirectResponse(f"/mcp/{record['server_name']}", status_code=303)
        else:
            friendly = store_error(record["last_error"], context="mcp", server_name=record["server_name"])
            record["friendly_error"] = friendly
            config_service.set_mcp_record(record["server_name"], record)
            _set_flash(
                request,
                f"MCP server '{record['server_name']}' installed, but the runtime probe failed: {record['last_error']}",
                level="error",
            )
            return RedirectResponse(f"/mcp/{record['server_name']}", status_code=303)

    @app.post("/mcp/add", response_class=HTMLResponse)
    async def mcp_add(request: Request):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        form = await request.form()
        name = str(form.get("name", "")).strip()
        transport = str(form.get("transport", "")).strip() or None
        command = str(form.get("command", "")).strip()
        args = _split_list(str(form.get("args", "")).strip())
        env_raw = str(form.get("env_json", "")).strip() or "{}"
        url = str(form.get("url", "")).strip()
        headers_raw = str(form.get("headers_json", "")).strip() or "{}"

        try:
            tool_timeout = _form_int(form.get("tool_timeout"), 30)
            env = _parse_json_object(env_raw, field_name="Env JSON")
            headers = _parse_json_object(headers_raw, field_name="Headers JSON")
        except ValueError as exc:
            config = config_service.load()
            return render_mcp_page(
                request,
                user,
                config=config,
                error=str(exc),
                manual_form={
                    "name": name,
                    "transport": transport or "",
                    "command": command,
                    "args": ", ".join(args),
                    "env_json": env_raw,
                    "url": url,
                    "headers_json": headers_raw,
                    "tool_timeout": str(form.get("tool_timeout", 30)),
                },
                status_code=400,
            )

        if not name:
            _set_flash(request, "Server name is required.", level="error")
            return RedirectResponse("/mcp", status_code=303)

        config = config_service.load()
        config.tools.mcp_servers[name] = MCPServerConfig(
            type=transport,
            command=command,
            args=args,
            env=env,
            url=url,
            headers=headers,
            tool_timeout=tool_timeout,
        )
        config_service.save(config)
        existing_record = config_service.get_mcp_record(name)
        config_service.set_mcp_record(
            name,
            {
                **existing_record,
                "server_name": name,
                "enabled": bool(existing_record.get("enabled", False)),
                "status": existing_record.get("status", "registered"),
                "status_label": existing_record.get("status_label", "Registered"),
            },
        )
        agent_service.invalidate()
        gui_logger.info("mcp_registered by=%s server=%s", user.username, name)
        _set_flash(request, "MCP server registered.")
        return RedirectResponse("/mcp", status_code=303)

    @app.post("/mcp/test/{server_name}")
    async def mcp_test(request: Request, server_name: str):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        form = await request.form()
        next_url = str(form.get("next", "/mcp")).strip() or "/mcp"

        try:
            record = await mcp_service.test_server(server_name)
        except ValueError as exc:
            store_error(str(exc), context="mcp", server_name=server_name)
            _set_flash(request, str(exc), level="error")
            return RedirectResponse(next_url, status_code=303)

        if not record.get("community_slug"):
            community_match = await resolve_community_match(str(record.get("repo_url", "")))
            if community_match:
                record["community_slug"] = community_match.get("slug", "")
                record["community_match"] = community_match
                config_service.set_mcp_record(server_name, record)

        config_service.set_last_mcp_test(
            {
                "server_name": server_name,
                "status": record["status"],
                "status_label": record["status_label"],
                "checked_at": record.get("last_checked_at", ""),
            }
        )
        if record["status"] == "active":
            await send_community_telemetry(record)
            clear_error()
            _set_flash(
                request,
                f"MCP server '{server_name}' is active with {len(record.get('tool_names', []))} tool(s).",
            )
        else:
            friendly = store_error(record.get("last_error", "Unknown MCP error"), context="mcp", server_name=server_name)
            record["friendly_error"] = friendly
            config_service.set_mcp_record(server_name, record)
            _set_flash(
                request,
                f"MCP server '{server_name}' is not active: {record.get('last_error', 'Unknown error')}",
                level="error",
            )
        return RedirectResponse(next_url, status_code=303)

    @app.post("/mcp/remove/{server_name}")
    async def mcp_remove(request: Request, server_name: str):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        result = mcp_service.remove_server(server_name)
        agent_service.invalidate()
        gui_logger.info("mcp_removed by=%s server=%s", user.username, server_name)
        message = f"MCP server '{server_name}' removed."
        if result["checkout_removed"]:
            message += " Managed checkout deleted."
        _set_flash(request, message)
        return RedirectResponse("/mcp", status_code=303)

    @app.post("/mcp/toggle/{server_name}")
    async def mcp_toggle(request: Request, server_name: str):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        form = await request.form()
        next_url = str(form.get("next", "/mcp")).strip() or "/mcp"

        record = config_service.get_mcp_record(server_name)
        if not record:
            _set_flash(request, f"MCP server '{server_name}' was not found.", level="error")
            return RedirectResponse(next_url, status_code=303)

        if record.get("enabled"):
            config_service.set_mcp_enabled(server_name, False)
            agent_service.invalidate()
            _set_flash(request, f"MCP server '{server_name}' disabled.")
            return RedirectResponse(next_url, status_code=303)

        if record.get("status") != "active":
            friendly = store_error(
                record.get("last_error") or "Run a successful MCP test before enabling this server.",
                context="mcp",
                server_name=server_name,
            )
            updated = {**record, "friendly_error": friendly}
            config_service.set_mcp_record(server_name, updated)
            _set_flash(
                request,
                f"MCP server '{server_name}' must pass a test before it can be enabled.",
                level="error",
            )
            return RedirectResponse(next_url, status_code=303)

        config_service.set_mcp_enabled(server_name, True)
        agent_service.invalidate()
        clear_error()
        _set_flash(request, f"MCP server '{server_name}' enabled for the main chat runtime.")
        return RedirectResponse(next_url, status_code=303)

    @app.get("/mcp/{server_name}", response_class=HTMLResponse)
    async def mcp_detail_page(request: Request, server_name: str):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        config = config_service.load()
        history = await agent_service.get_mcp_test_history(user, server_name)
        record = config_service.get_mcp_record(server_name)
        community_item: dict[str, Any] = {}
        community_slug = str(record.get("community_slug", "")).strip()
        if community_slug and community_service.enabled:
            try:
                community_item = await community_service.marketplace_detail(community_slug)
            except Exception:
                gui_logger.exception("community_marketplace_detail_failed slug=%s", community_slug)
        return render_mcp_detail_page(
            request,
            user,
            config=config,
            server_name=server_name,
            community_item=community_item,
            mcp_test_history=history,
        )

    @app.post("/mcp/{server_name}", response_class=HTMLResponse)
    async def mcp_detail_submit(request: Request, server_name: str):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        config = config_service.load()
        server = config.tools.mcp_servers.get(server_name)
        if server is None:
            _set_flash(request, f"MCP server '{server_name}' was not found.", level="error")
            return RedirectResponse("/mcp", status_code=303)

        form = await request.form()
        transport = str(form.get("transport", "")).strip() or None
        command = str(form.get("command", "")).strip()
        args = _split_list(str(form.get("args", "")).strip())
        url = str(form.get("url", "")).strip()
        env_raw = str(form.get("env_json", "")).strip() or "{}"
        headers_raw = str(form.get("headers_json", "")).strip() or "{}"

        try:
            tool_timeout = _form_int(form.get("tool_timeout"), server.tool_timeout)
            env = _parse_json_object(env_raw, field_name="Env JSON")
            headers = _parse_json_object(headers_raw, field_name="Headers JSON")
        except ValueError as exc:
            history = await agent_service.get_mcp_test_history(user, server_name)
            record = config_service.get_mcp_record(server_name)
            community_item: dict[str, Any] = {}
            community_slug = str(record.get("community_slug", "")).strip()
            if community_slug and community_service.enabled:
                try:
                    community_item = await community_service.marketplace_detail(community_slug)
                except Exception:
                    gui_logger.exception("community_marketplace_detail_failed slug=%s", community_slug)
            return render_mcp_detail_page(
                request,
                user,
                config=config,
                server_name=server_name,
                community_item=community_item,
                mcp_test_history=history,
                error=str(exc),
                status_code=400,
            )

        existing_record = config_service.get_mcp_record(server_name)
        known_env_fields = [
            *[str(item) for item in existing_record.get("required_env", [])],
            *[str(item) for item in existing_record.get("optional_env", []) if str(item) not in existing_record.get("required_env", [])],
        ]
        for env_name in known_env_fields:
            field_value = str(form.get(f"env__{env_name}", "")).strip()
            if field_value:
                env[env_name] = field_value
            elif env_name in env:
                env.pop(env_name, None)

        config.tools.mcp_servers[server_name] = MCPServerConfig(
            type=transport,
            command=command,
            args=args,
            env=env,
            url=url,
            headers=headers,
            tool_timeout=tool_timeout,
        )
        config_service.save(config)
        record = config_service.get_mcp_record(server_name)
        config_service.set_mcp_record(
            server_name,
            {
                **record,
                "enabled": False,
                "status": "registered",
                "status_label": "Saved, not tested",
                "friendly_error": {},
                "last_error": "",
            },
        )
        agent_service.invalidate()
        _set_flash(request, f"MCP server '{server_name}' saved. Run the test before enabling it.")
        return RedirectResponse(f"/mcp/{server_name}", status_code=303)

    @app.post("/mcp/publish/{server_name}")
    async def mcp_publish(request: Request, server_name: str):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        prefs = config_service.get_community_preferences()
        if not prefs.get("allow_public_mcp_submissions"):
            _set_flash(request, "Enable MCP publishing in Settings before publishing to the Community Hub.", level="error")
            return RedirectResponse("/settings", status_code=303)
        if not community_service.enabled:
            _set_flash(request, "Community hub is not configured for this GUI instance.", level="error")
            return RedirectResponse(f"/mcp/{server_name}", status_code=303)

        config = config_service.load()
        server = config.tools.mcp_servers.get(server_name)
        if server is None:
            _set_flash(request, f"MCP server '{server_name}' was not found.", level="error")
            return RedirectResponse("/mcp", status_code=303)

        card = _build_mcp_server_card(server_name, server, config_service)
        form = await request.form()
        payload = _build_mcp_submission_payload(
            server_name=server_name,
            card=card,
            form=form,
            submitted_by=user.username,
            source_instance=settings.instance_name,
            source_public_url=settings.public_url or "",
        )
        if not payload.get("repo_url"):
            _set_flash(request, "This MCP has no repository URL recorded yet, so it cannot be published to the Community Hub.", level="error")
            return RedirectResponse(f"/mcp/{server_name}", status_code=303)
        try:
            result = await community_service.submit_mcp(payload)
        except Exception as exc:
            gui_logger.exception("mcp_publish_failed server=%s", server_name)
            _set_flash(request, f"Community publish failed: {exc}", level="error")
            return RedirectResponse(f"/mcp/{server_name}", status_code=303)

        item = result.get("item") if isinstance(result.get("item"), dict) else {}
        if item.get("slug"):
            record = config_service.get_mcp_record(server_name)
            config_service.set_mcp_record(
                server_name,
                {
                    **record,
                    "community_slug": item["slug"],
                    "community_submission_status": "published" if result.get("created") else "duplicate",
                    "community_submission_at": _utc_now(),
                },
            )
            if result.get("created"):
                _set_flash(request, f"Published '{item.get('name', item['slug'])}' to the Community Hub.")
            else:
                _set_flash(request, f"That MCP is already tracked as '{item.get('name', item['slug'])}'.")
        else:
            _set_flash(request, "Community publish finished, but the hub did not return a marketplace entry.", level="error")
        return RedirectResponse(f"/mcp/{server_name}", status_code=303)

    @app.post("/mcp/repair/{server_name}")
    async def mcp_repair(request: Request, server_name: str, background_tasks: BackgroundTasks):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        form = await request.form()
        next_url = str(form.get("next", f"/mcp/{server_name}")).strip() or f"/mcp/{server_name}"
        allow_unrestricted = config_service.is_unrestricted_agent_shell_enabled()
        repair_plan = await mcp_service.build_repair_plan(server_name, allow_unrestricted=allow_unrestricted)
        repair_action = _get_repair_action(settings, repair_plan)
        if not repair_plan.get("supported"):
            _set_flash(request, repair_plan.get("next_step") or "No supported repair recipe is available for this MCP right now.", level="error")
            return RedirectResponse(next_url, status_code=303)
        if not repair_action["enabled"]:
            _set_flash(request, repair_action["description"], level="error")
            return RedirectResponse(next_url, status_code=303)

        record = config_service.get_mcp_record(server_name)
        config_service.set_mcp_record(
            server_name,
            {
                **record,
                "repair_status": "queued",
                "repair_status_label": "Repair queued",
                "repair_recipe": repair_plan.get("recommended_recipe", ""),
                "repair_requested_at": _utc_now(),
                "repair_available_recipes": repair_plan.get("available_recipes", []),
                "repair_log_tail": _append_log(
                    str(record.get("repair_log_tail", "")).strip(),
                    f"Queued repair recipe: {repair_plan.get('recommended_recipe', '') or 'none'}",
                ),
            },
        )
        gui_logger.warning(
            "mcp_repair_requested by=%s server=%s recipe=%s unrestricted=%s",
            user.username,
            server_name,
            repair_plan.get("recommended_recipe", ""),
            allow_unrestricted,
        )
        background_tasks.add_task(
            _run_mcp_repair_command,
            str(repair_action["command"]),
            gui_logger,
            config_service,
            mcp_service,
            server_name,
            repair_plan,
        )
        _set_flash(
            request,
            f"Repair worker started for '{server_name}'. Run the MCP test again after it finishes.",
        )
        return RedirectResponse(next_url, status_code=303)

    @app.post("/mcp/test/{server_name}/chat")
    async def mcp_test_chat_send(request: Request, server_name: str):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        form = await request.form()
        message = str(form.get("message", "")).strip()
        if not message:
            _set_flash(request, "Write a test prompt before sending it.", level="error")
            return RedirectResponse(f"/mcp/{server_name}", status_code=303)

        try:
            record = await mcp_service.test_server(server_name)
            config_service.set_last_mcp_test(
                {
                    "server_name": server_name,
                    "status": record["status"],
                    "status_label": record["status_label"],
                    "checked_at": record.get("last_checked_at", ""),
                }
            )
            if record["status"] != "active":
                friendly = store_error(record.get("last_error", "Unknown MCP error"), context="mcp", server_name=server_name)
                record["friendly_error"] = friendly
                config_service.set_mcp_record(server_name, record)
                _set_flash(request, friendly["title"] + ": " + friendly["next_action"], level="error")
                return RedirectResponse(f"/mcp/{server_name}", status_code=303)
            result = await agent_service.send_mcp_test_message(user, server_name, message)
            record_usage(
                source="mcp_test",
                provider=str(result.get("provider", "")),
                model=str(result.get("model", "")),
                usage=result.get("usage"),
                note=f"MCP test chat for {server_name}",
            )
            clear_error()
        except ValueError as exc:
            friendly = store_error(str(exc), context="mcp", server_name=server_name)
            record = config_service.get_mcp_record(server_name)
            config_service.set_mcp_record(server_name, {**record, "friendly_error": friendly})
            _set_flash(request, friendly["title"] + ": " + friendly["next_action"], level="error")
            return RedirectResponse(f"/mcp/{server_name}", status_code=303)
        except Exception as exc:
            gui_logger.exception("mcp_test_chat_failed user=%s server=%s", user.username, server_name)
            friendly = store_error(str(exc), context="mcp", server_name=server_name)
            record = config_service.get_mcp_record(server_name)
            config_service.set_mcp_record(server_name, {**record, "friendly_error": friendly})
            _set_flash(request, friendly["title"] + ": " + friendly["next_action"], level="error")
            return RedirectResponse(f"/mcp/{server_name}", status_code=303)

        _set_flash(request, f"MCP test chat updated for '{server_name}'.")
        return RedirectResponse(f"/mcp/{server_name}", status_code=303)

    @app.post("/mcp/test/{server_name}/clear")
    async def mcp_test_chat_clear(request: Request, server_name: str):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        await agent_service.clear_mcp_test(user, server_name)
        _set_flash(request, f"MCP test chat cleared for '{server_name}'.")
        return RedirectResponse(f"/mcp/{server_name}", status_code=303)

    @app.get("/chat", response_class=HTMLResponse)
    async def chat_page(request: Request):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        if not config_service.is_setup_complete():
            return RedirectResponse("/setup/provider", status_code=303)

        config = config_service.load()
        history = await agent_service.get_chat_history(user)
        recent_tool_activity = await agent_service.get_recent_tool_activity(user)
        agent_health = config_service.get_agent_health()
        installed_servers = _build_mcp_server_cards(config, config_service)
        active_servers = [server for server in installed_servers if server["enabled"]]
        active_tool_names = sorted({tool for server in active_servers for tool in server.get("tool_names", [])})
        usage_summary = config_service.get_usage_summary()
        return _render(
            request,
            "chat.html",
            {
                "title": "Chat",
                "nav_active": "chat",
                "user": user,
                "chat_history": history,
                "agent_health": agent_health,
                "runtime_status": _build_runtime_status(
                    agent_health=agent_health,
                    gateway_status=await _probe_gateway(settings.gateway_health_url),
                    last_restart_at=config_service.get_last_restart_at(),
                ),
                "model_name": config.agents.defaults.model,
                "provider_name": config.get_provider_name() or config.agents.defaults.provider,
                "active_mcp_servers": active_servers,
                "active_tool_names": active_tool_names,
                "recent_tool_activity": recent_tool_activity,
                "chat_error": config_service.get_last_error(),
                "chat_quick_prompts": [
                    "Check the current setup and tell me if anything is missing.",
                    "List the active MCP servers and the tools they provide.",
                    "Explain the current workspace structure in simple language.",
                ],
                "chat_templates": CHAT_TEMPLATE_DEFINITIONS,
                "recent_uploads": config_service.recent_uploads(),
                "usage_snapshot": usage_summary,
            },
        )

    @app.post("/chat/send", response_class=HTMLResponse)
    async def chat_send(request: Request):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        form = await request.form()
        message = str(form.get("message", "")).strip()
        if not message:
            _set_flash(request, "Write a message before sending.", level="error")
            return RedirectResponse("/chat", status_code=303)

        return await dispatch_chat_message(
            request,
            user,
            message,
            source="chat",
            success_flash="Response received.",
        )

    @app.post("/chat/template")
    async def chat_template_send(request: Request):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        form = await request.form()
        template_key = str(form.get("template", "")).strip()
        value = str(form.get("value", "")).strip()
        if not template_key:
            _set_flash(request, "Select a prompt template first.", level="error")
            return RedirectResponse("/chat", status_code=303)
        if not value:
            _set_flash(request, "Fill in the template field before sending.", level="error")
            return RedirectResponse("/chat", status_code=303)

        try:
            message = _build_chat_template_message(template_key, value)
        except ValueError as exc:
            _set_flash(request, str(exc), level="error")
            return RedirectResponse("/chat", status_code=303)

        return await dispatch_chat_message(
            request,
            user,
            message,
            source=f"template:{template_key}",
            success_flash="Template prompt sent.",
            note=f"Template {template_key}",
        )

    @app.post("/chat/upload")
    async def chat_upload(request: Request):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        form = await request.form()
        upload = form.get("attachment")
        if (
            upload is None
            or not hasattr(upload, "filename")
            or not hasattr(upload, "file")
            or not str(getattr(upload, "filename", "")).strip()
        ):
            _set_flash(request, "Choose a file before uploading.", level="error")
            return RedirectResponse("/chat", status_code=303)

        instruction = str(form.get("message", "")).strip()
        try:
            uploaded = _store_chat_upload(upload, config_service.uploads_dir, config_service.default_workspace)
        except ValueError as exc:
            _set_flash(request, str(exc), level="error")
            return RedirectResponse("/chat", status_code=303)

        base_message = instruction or f"Inspect the uploaded file at `{uploaded['relative_path']}` and summarize what matters."
        message = (
            f"{base_message}\n\n"
            f"The user uploaded a file into the workspace at `{uploaded['relative_path']}`. "
            "Use your file tools to inspect that path directly. If the file is text or code, summarize its contents and the next useful actions."
        )
        return await dispatch_chat_message(
            request,
            user,
            message,
            source="chat_upload",
            success_flash=f"Uploaded {uploaded['name']} and sent it to chat.",
            note=f"Upload {uploaded['relative_path']}",
        )

    @app.post("/chat/clear")
    async def chat_clear(request: Request):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        await agent_service.clear_chat(user)
        _set_flash(request, "Chat history cleared.")
        return RedirectResponse("/chat", status_code=303)

    @app.get("/memory", response_class=HTMLResponse)
    async def memory_page(request: Request, doc: str = Query("memory")):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        config_service.set_active_memory_doc(doc)
        active_document = config_service.read_markdown_document(doc)
        return _render(
            request,
            "memory.html",
            {
                "title": "Memory",
                "nav_active": "memory",
                "user": user,
                "documents": config_service.markdown_documents(),
                "document_groups": _group_documents(config_service.markdown_documents()),
                "active_document": active_document,
                "markdown_preview": _render_markdown_preview(active_document["content"]),
            },
        )

    @app.post("/memory", response_class=HTMLResponse)
    async def memory_submit(request: Request):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        form = await request.form()
        doc_key = str(form.get("doc", "memory")).strip() or "memory"
        content = str(form.get("content", ""))
        saved = config_service.save_markdown_document(doc_key, content)
        config_service.set_active_memory_doc(doc_key)
        gui_logger.info("memory_saved by=%s doc=%s", user.username, doc_key)
        _set_flash(request, f"{saved['label']} saved.")
        return RedirectResponse(f"/memory?doc={doc_key}", status_code=303)

    @app.post("/memory/reset")
    async def memory_reset(request: Request):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        form = await request.form()
        doc_key = str(form.get("doc", "memory")).strip() or "memory"
        reset = config_service.reset_markdown_document(doc_key)
        config_service.set_active_memory_doc(doc_key)
        gui_logger.info("memory_reset by=%s doc=%s", user.username, doc_key)
        _set_flash(request, f"{reset['label']} reset to the bundled template.")
        return RedirectResponse(f"/memory?doc={doc_key}", status_code=303)

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        config = config_service.load()
        validation = await _validate_setup(
            config=config,
            config_service=config_service,
            gateway_health=await _probe_gateway(settings.gateway_health_url),
            agent_health=config_service.get_agent_health(),
        )
        return _render(
            request,
            "settings.html",
            {
                "title": "Settings",
                "nav_active": "settings",
                "user": user,
                "settings_form": {
                    "workspace": str(config.workspace_path),
                    "tools_enabled": config.tools.enabled,
                    "restrict_to_workspace": config.tools.restrict_to_workspace,
                    "send_progress": config.channels.send_progress,
                    "send_tool_hints": config.channels.send_tool_hints,
                    "exec_timeout": config.tools.exec.timeout,
                    "path_append": config.tools.exec.path_append,
                    "dangerous_repair_mode": config_service.is_unrestricted_agent_shell_enabled(),
                    **config_service.get_community_preferences(),
                },
                "settings_meta": {
                    "config_path": str(settings.config_path),
                    "provider": config.agents.defaults.provider,
                    "model": config.agents.defaults.model,
                    "installed_mcp_servers": len(config.tools.mcp_servers),
                    "setup_complete": config_service.is_setup_complete(),
                    "repair_mode": settings.repair_mode,
                    "repair_command_configured": bool(str(settings.repair_command or "").strip()),
                    "community_api_url": settings.community_api_url or "",
                    "community_public_url": settings.community_public_url or "",
                    "community_enabled": bool(community_service.enabled),
                },
                "validation_results": validation,
                "next_validation_issue": _next_validation_issue(validation),
                "workspace_locked": bool(config_service.workspace_override),
            },
        )

    @app.post("/settings")
    async def settings_submit(request: Request):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        form = await request.form()
        config = config_service.load()
        try:
            if not config_service.workspace_override:
                config.agents.defaults.workspace = str(form.get("workspace", str(config.workspace_path))).strip()
            config.tools.enabled = bool(form.get("tools_enabled"))
            config.tools.restrict_to_workspace = bool(form.get("restrict_to_workspace"))
            config.channels.send_progress = bool(form.get("send_progress"))
            config.channels.send_tool_hints = bool(form.get("send_tool_hints"))
            config.tools.exec.timeout = _form_int(form.get("exec_timeout"), config.tools.exec.timeout)
            config.tools.exec.path_append = str(form.get("path_append", config.tools.exec.path_append)).strip()
            dangerous_repair_mode = bool(form.get("dangerous_repair_mode"))
            share_anonymous_metrics = bool(form.get("share_anonymous_metrics"))
            receive_recommendations = bool(form.get("receive_recommendations"))
            show_marketplace_stats = bool(form.get("show_marketplace_stats"))
            allow_public_mcp_submissions = bool(form.get("allow_public_mcp_submissions"))
        except ValueError as exc:
            validation = await _validate_setup(
                config=config,
                config_service=config_service,
                gateway_health=await _probe_gateway(settings.gateway_health_url),
                agent_health=config_service.get_agent_health(),
            )
            return _render(
                request,
                "settings.html",
                {
                    "title": "Settings",
                    "nav_active": "settings",
                    "user": user,
                    "error": str(exc),
                    "settings_form": {
                        "workspace": str(form.get("workspace", str(config.workspace_path))),
                        "tools_enabled": bool(form.get("tools_enabled")),
                        "restrict_to_workspace": bool(form.get("restrict_to_workspace")),
                        "send_progress": bool(form.get("send_progress")),
                        "send_tool_hints": bool(form.get("send_tool_hints")),
                        "exec_timeout": str(form.get("exec_timeout", config.tools.exec.timeout)),
                        "path_append": str(form.get("path_append", config.tools.exec.path_append)),
                        "dangerous_repair_mode": bool(form.get("dangerous_repair_mode")),
                        "share_anonymous_metrics": bool(form.get("share_anonymous_metrics")),
                        "receive_recommendations": bool(form.get("receive_recommendations")),
                        "show_marketplace_stats": bool(form.get("show_marketplace_stats")),
                        "allow_public_mcp_submissions": bool(form.get("allow_public_mcp_submissions")),
                    },
                    "settings_meta": {
                        "config_path": str(settings.config_path),
                        "provider": config.agents.defaults.provider,
                        "model": config.agents.defaults.model,
                        "installed_mcp_servers": len(config.tools.mcp_servers),
                        "setup_complete": config_service.is_setup_complete(),
                        "repair_mode": settings.repair_mode,
                        "repair_command_configured": bool(str(settings.repair_command or "").strip()),
                        "community_api_url": settings.community_api_url or "",
                        "community_public_url": settings.community_public_url or "",
                        "community_enabled": bool(community_service.enabled),
                    },
                    "validation_results": validation,
                    "next_validation_issue": _next_validation_issue(validation),
                    "workspace_locked": bool(config_service.workspace_override),
                },
                status_code=400,
            )

        config_service.save(config)
        config_service.set_unrestricted_agent_shell_enabled(dangerous_repair_mode)
        config_service.set_community_preferences(
            share_anonymous_metrics=share_anonymous_metrics,
            receive_recommendations=receive_recommendations,
            show_marketplace_stats=show_marketplace_stats,
            allow_public_mcp_submissions=allow_public_mcp_submissions,
        )
        agent_service.invalidate()
        clear_error()
        _set_flash(request, "Settings saved.")
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/validate")
    async def settings_validate(request: Request):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        agent_health = await run_agent_health_check()
        if agent_health.get("ok"):
            clear_error()
        else:
            store_error(str(agent_health.get("error", "Unknown error")), context="provider")

        config = config_service.load()
        validation = await _validate_setup(
            config=config,
            config_service=config_service,
            gateway_health=await _probe_gateway(settings.gateway_health_url),
            agent_health=agent_health,
        )
        return _render(
            request,
            "settings.html",
            {
                "title": "Settings",
                "nav_active": "settings",
                "user": user,
                "settings_form": {
                    "workspace": str(config.workspace_path),
                    "tools_enabled": config.tools.enabled,
                    "restrict_to_workspace": config.tools.restrict_to_workspace,
                    "send_progress": config.channels.send_progress,
                    "send_tool_hints": config.channels.send_tool_hints,
                    "exec_timeout": config.tools.exec.timeout,
                    "path_append": config.tools.exec.path_append,
                },
                "settings_meta": {
                    "config_path": str(settings.config_path),
                    "provider": config.agents.defaults.provider,
                    "model": config.agents.defaults.model,
                    "installed_mcp_servers": len(config.tools.mcp_servers),
                    "setup_complete": config_service.is_setup_complete(),
                },
                "validation_results": validation,
                "next_validation_issue": _next_validation_issue(validation),
                "workspace_locked": bool(config_service.workspace_override),
            },
        )

    @app.get("/history", response_class=HTMLResponse)
    async def history_page(request: Request):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        sessions = await agent_service.list_sessions()
        return _render(
            request,
            "history.html",
            {
                "title": "History",
                "nav_active": "chat",
                "user": user,
                "sessions": sessions,
            },
        )

    @app.get("/status", response_class=HTMLResponse)
    async def status_page(request: Request):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        config = config_service.load()
        gateway_health = await _probe_gateway(settings.gateway_health_url)
        agent_health = config_service.get_agent_health()
        return _render(
            request,
            "status.html",
            {
                "title": "Status",
                "nav_active": "dashboard",
                "user": user,
                "runtime_status": _build_runtime_status(
                    agent_health=agent_health,
                    gateway_status=gateway_health,
                    last_restart_at=config_service.get_last_restart_at(),
                ),
                "gateway_status": gateway_health,
                "agent_health": agent_health,
                "config_path_value": str(settings.config_path),
                "workspace_value": str(config.workspace_path),
                "last_restart_at": config_service.get_last_restart_at(),
            },
        )

    @app.get("/usage", response_class=HTMLResponse)
    async def usage_page(request: Request):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        usage_summary = config_service.get_usage_summary()
        return _render(
            request,
            "usage.html",
            {
                "title": "Usage",
                "nav_active": "dashboard",
                "user": user,
                "agent_health": config_service.get_agent_health(),
                "usage_summary": usage_summary,
                "usage_events": usage_summary["recent_events"],
            },
        )

    @app.get("/logs", response_class=HTMLResponse)
    async def logs_page(request: Request):
        user = _require_admin(request, auth_service)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        level = str(request.query_params.get("level", "all")).strip().lower() or "all"
        log_file = config_service.runtime_dir / "logs" / "gui.log"
        if log_file.exists():
            lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
            entries = [_classify_log_line(line) for line in lines[-400:]]
            if level != "all":
                entries = [entry for entry in entries if entry["level"] == level]
        else:
            entries = [{"level": "info", "label": "INFO", "line": "No GUI logs have been written yet."}]

        return _render(
            request,
            "logs.html",
            {
                "title": "Logs",
                "nav_active": "logs",
                "user": user,
                "log_file": str(log_file),
                "log_entries": entries,
                "log_level": level,
            },
        )

    return app


def _render(
    request: Request,
    template_name: str,
    context: dict[str, Any],
    *,
    status_code: int = 200,
) -> HTMLResponse:
    """Render a Jinja template with the shared shell context."""
    settings: GUISettings = request.app.state.settings
    config_service: GUIConfigService = request.app.state.config_service
    gui_logger: logging.Logger = request.app.state.gui_logger
    config = config_service.load()
    agent_health = config_service.get_agent_health()
    installed_servers = _build_mcp_server_cards(config, config_service)
    enabled_channels = [
        name
        for name in CHANNEL_DEFINITIONS
        if name != "none" and getattr(config.channels, name, None) and getattr(config.channels, name).enabled
    ]
    setup_progress = _build_setup_progress(
        config=config,
        agent_health=agent_health,
        installed_servers=installed_servers,
        enabled_channels=enabled_channels,
    )
    provider_name = config.get_provider_name() or config.agents.defaults.provider
    update_status = _ensure_update_status(settings, config_service, gui_logger, user_present=bool(context.get("user")))
    shell_context = {
        "instance_name": settings.instance_name,
        "current_version": __version__,
        "gui_host": settings.host,
        "gui_port": settings.port,
        "config_path": str(config_service.config_path),
        "workspace_path": str(config_service.default_workspace),
        "request_path": request.url.path,
        "setup_complete": config_service.is_setup_complete(),
        "safe_mode": config_service.is_safe_mode(),
        "branding_banner_url": "/media/branding/nanobot-webgui-banner.png" if config_service.branding_banner_path.exists() else "",
        "project_repo_url": "https://github.com/lucmuss/nanobot-webgui",
        "upstream_repo_url": "https://github.com/HKUDS/nanobot",
        "public_url": settings.public_url or "",
        "flash": context.get("flash") or _pop_flash(request),
        "restart_action": _get_restart_action(settings),
        "update_status": update_status,
        "update_action": _get_update_action(settings, update_status),
        "shell_status": {
            "runtime_label": "Online" if agent_health.get("ok") else "Needs setup" if not config_service.is_setup_complete() else "Attention",
            "setup_done": len([item for item in setup_progress if item.get("done")]),
            "setup_total": len(setup_progress),
            "enabled_mcp": len(config_service.enabled_mcp_servers(config.tools.mcp_servers)),
            "provider_label": provider_name or "Unset",
        },
    }
    return _TEMPLATES.TemplateResponse(
        request=request,
        name=template_name,
        context={**shell_context, **context},
        status_code=status_code,
    )


def _current_admin(request: Request, auth_service: AuthService) -> AdminUser | None:
    """Read the current session user."""
    session_admin_id = request.session.get("admin_id")
    try:
        admin_id = int(session_admin_id) if session_admin_id is not None else None
    except (TypeError, ValueError):
        return None
    return auth_service.get_admin(admin_id)


def _require_admin(request: Request, auth_service: AuthService) -> AdminUser | None:
    """Return the authenticated user for protected routes."""
    if not auth_service.has_admin():
        return None
    return _current_admin(request, auth_service)


def _selected_channel(config) -> str:
    """Return the first enabled channel or 'none'."""
    for channel_name in CHANNEL_DEFINITIONS:
        if channel_name == "none":
            continue
        if getattr(config.channels, channel_name).enabled:
            return channel_name
    return "none"


def _channel_values(config, channel_name: str) -> dict[str, Any]:
    """Return the current values for the selected channel form."""
    if channel_name == "none":
        return {}

    channel_cfg = getattr(config.channels, channel_name)
    values: dict[str, Any] = {}
    for field in CHANNEL_DEFINITIONS[channel_name]["fields"]:
        raw_value = getattr(channel_cfg, field["name"], "")
        if field["type"] == "list":
            values[field["name"]] = ", ".join(raw_value)
        else:
            values[field["name"]] = raw_value
    return values


def _coerce_field_value(raw_value: Any, field_type: str) -> Any:
    """Convert HTML form values into config-friendly Python types."""
    if field_type == "bool":
        return bool(raw_value)
    if field_type == "list":
        return _split_list(str(raw_value or ""))
    return str(raw_value or "").strip()


def _split_list(value: str) -> list[str]:
    """Split comma/newline separated values into a clean list."""
    parts = []
    for chunk in value.replace("\n", ",").split(","):
        item = chunk.strip()
        if item:
            parts.append(item)
    return parts


def _parse_json_object(raw: str, *, field_name: str) -> dict[str, str]:
    """Parse a JSON object textarea used in the GUI forms."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} must be valid JSON.") from exc

    if not isinstance(parsed, dict):
        raise ValueError(f"{field_name} must be a JSON object.")

    return {str(key): str(value) for key, value in parsed.items()}


def _form_int(value: Any, default: int) -> int:
    """Parse an integer form field."""
    raw = str(value or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError("Enter a valid whole number.") from exc


def _form_float(value: Any, default: float) -> float:
    """Parse a float form field."""
    raw = str(value or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError("Enter a valid number.") from exc


def _set_flash(request: Request, message: str, *, level: str = "success") -> None:
    """Store a one-shot flash message in the session."""
    request.session["flash"] = {"message": message, "level": level}


def _pop_flash(request: Request) -> dict[str, str] | None:
    """Return and clear the session flash message."""
    flash = request.session.pop("flash", None)
    if isinstance(flash, dict):
        return {
            "message": str(flash.get("message", "")),
            "level": str(flash.get("level", "success")),
        }
    return None


def _store_avatar(upload: UploadFile, avatars_dir: Path) -> str:
    """Store an uploaded avatar and return the relative media path."""
    suffix = Path(upload.filename or "").suffix.lower()
    content_type = (upload.content_type or "").strip().lower()
    allowed_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    if suffix not in allowed_suffixes and content_type not in {
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/gif",
    }:
        raise ValueError("Avatar must be a PNG, JPEG, WEBP, or GIF image.")
    if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        suffix = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/webp": ".webp",
            "image/gif": ".gif",
        }.get(content_type, ".png")

    avatars_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}{suffix}"
    target = avatars_dir / filename
    upload.file.seek(0)
    with target.open("wb") as output:
        shutil.copyfileobj(upload.file, output)
    return f"avatars/{filename}"


def _build_mcp_server_card(server_name: str, server: MCPServerConfig, config_service: GUIConfigService) -> dict[str, Any]:
    """Merge one MCP config entry with GUI-managed runtime metadata."""
    record = config_service.get_mcp_record(server_name)
    enabled = bool(record.get("enabled", False))
    test_status = str(record.get("status", "registered")).strip() or "registered"
    test_label = str(record.get("status_label", "Registered")).strip() or "Registered"
    if enabled:
        effective_status = test_label if test_status != "registered" else "Enabled"
        status_tone = "good" if test_status == "active" else "bad" if test_status in {"error", "needs_configuration"} else "muted"
    else:
        effective_status = "Disabled"
        status_tone = "muted"

    return {
        "name": server_name,
        "summary": _display_summary_text(record.get("summary", "")),
        "repo_url": str(record.get("repo_url", "")).strip(),
        "transport": str(record.get("transport", "")).strip() or server.type or "auto",
        "status": test_status,
        "status_label": test_label,
        "effective_status": effective_status,
        "status_tone": status_tone,
        "enabled": enabled,
        "friendly_error": record.get("friendly_error") if isinstance(record.get("friendly_error"), dict) else {},
        "last_error": str(record.get("last_error", "")).strip(),
        "last_checked_at": str(record.get("last_checked_at", "")).strip(),
        "last_installed_at": str(record.get("last_installed_at", "")).strip(),
        "tool_names": [str(item) for item in record.get("tool_names", [])],
        "required_env": [str(item) for item in record.get("required_env", [])],
        "optional_env": [str(item) for item in record.get("optional_env", [])],
        "missing_env": [str(item) for item in record.get("missing_env", [])],
        "healthcheck": str(record.get("healthcheck", "")).strip(),
        "install_steps": [str(item) for item in record.get("install_steps", [])],
        "install_dir": str(record.get("install_dir", "")).strip(),
        "log_tail": str(record.get("log_tail", "")).strip(),
        "repo_type": str(record.get("repo_type", "")).strip(),
        "analysis_mode": str(record.get("analysis_mode", "deterministic")).strip() or "deterministic",
        "analysis_confidence": float(record.get("analysis_confidence", 0.0) or 0.0),
        "required_runtimes": [str(item) for item in record.get("required_runtimes", [])],
        "runtime_status": list(record.get("runtime_status", [])) if isinstance(record.get("runtime_status"), list) else [],
        "missing_runtimes": [str(item) for item in record.get("missing_runtimes", [])],
        "next_action": str(record.get("next_action", "")).strip(),
        "repair_status": str(record.get("repair_status", "")).strip(),
        "repair_status_label": str(record.get("repair_status_label", "")).strip(),
        "repair_recipe": str(record.get("repair_recipe", "")).strip(),
        "repair_requested_at": str(record.get("repair_requested_at", "")).strip(),
        "repair_finished_at": str(record.get("repair_finished_at", "")).strip(),
        "repair_log_tail": str(record.get("repair_log_tail", "")).strip(),
        "repair_available_recipes": [str(item) for item in record.get("repair_available_recipes", [])]
        or supported_repair_recipes([str(item) for item in record.get("missing_runtimes", [])]),
        "dangerous_repair_enabled": config_service.is_unrestricted_agent_shell_enabled(),
        "start_command": _join_command(server.command, list(server.args)),
        "type": server.type or "auto",
        "command": server.command,
        "args": list(server.args),
        "url": server.url,
    }


def _build_mcp_server_cards(config, config_service: GUIConfigService) -> list[dict[str, Any]]:
    """Merge MCP config entries with GUI-managed runtime metadata."""
    cards: list[dict[str, Any]] = []
    for name, server in config.tools.mcp_servers.items():
        cards.append(_build_mcp_server_card(name, server, config_service))
    cards.sort(key=lambda item: item["name"])
    return cards


def _display_summary_text(value: Any) -> str:
    """Strip raw HTML and markdown image syntax from stored MCP summaries."""
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"!\[[^\]]*]\([^)]*\)", " ", text)
    text = re.sub(r"<img\b[^>]*>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"</?[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _default_mcp_publish_form(card: dict[str, Any], user: AdminUser) -> dict[str, str]:
    """Build sensible defaults for publishing one local MCP entry to the hub."""
    return {
        "name": str(card.get("name", "")).strip(),
        "description": str(card.get("summary", "")).strip() or "Community-submitted MCP server from a Nanobot instance.",
        "category": _guess_community_category(card),
        "tags": ", ".join(_guess_community_tags(card)),
        "submitted_by": user.username,
    }


def _build_mcp_submission_payload(
    *,
    server_name: str,
    card: dict[str, Any],
    form,
    submitted_by: str,
    source_instance: str,
    source_public_url: str,
) -> dict[str, Any]:
    """Build one bounded MCP submission payload for the community hub."""
    name = str(form.get("name", "")).strip() or server_name
    description = str(form.get("description", "")).strip() or str(card.get("summary", "")).strip()
    category = str(form.get("category", "")).strip() or _guess_community_category(card)
    tags = _split_list(str(form.get("tags", "")).strip()) or _guess_community_tags(card)
    known_issues = []
    if str(card.get("last_error", "")).strip():
        known_issues.append(str(card.get("last_error", "")).strip())
    return {
        "repo_url": str(card.get("repo_url", "")).strip(),
        "name": name,
        "description": description or "Community-submitted MCP server from a Nanobot instance.",
        "category": category,
        "install_method": _guess_community_install_method(card),
        "language": _guess_community_language(card),
        "tags": tags,
        "tools": [str(item) for item in card.get("tool_names", [])],
        "known_issues": known_issues,
        "submitted_by": submitted_by,
        "source_instance": source_instance,
        "source_public_url": source_public_url,
        "repo_type": str(card.get("repo_type", "")).strip(),
    }


def _guess_community_category(card: dict[str, Any]) -> str:
    """Infer a coarse community category from local MCP metadata."""
    haystack = " ".join(
        [
            str(card.get("name", "")),
            str(card.get("summary", "")),
            *[str(item) for item in card.get("tool_names", [])],
        ]
    ).lower()
    if any(token in haystack for token in ("github", "repo", "pull", "issue", "code", "devtools")):
        return "Coding"
    if any(token in haystack for token in ("search", "crawl", "extract", "browser", "doc", "context")):
        return "Research"
    return "Automation"


def _guess_community_install_method(card: dict[str, Any]) -> str:
    """Infer the install method used for one MCP record."""
    repo_type = str(card.get("repo_type", "")).strip()
    if repo_type:
        return repo_type
    transport = str(card.get("transport", "")).strip().lower()
    if transport in {"sse", "streamablehttp"}:
        return "remote"
    return "unknown"


def _guess_community_language(card: dict[str, Any]) -> str:
    """Infer a coarse runtime/language label for the community hub."""
    method = _guess_community_install_method(card).lower()
    if method in {"npm", "workspace_package", "monorepo"}:
        return "Node.js"
    if method in {"python", "pip", "uv"}:
        return "Python"
    if method in {"remote", "http", "sse", "streamablehttp"}:
        return "Remote"
    if method == "docker":
        return "Docker"
    return "Unknown"


def _guess_community_tags(card: dict[str, Any]) -> list[str]:
    """Suggest a small tag list for a community submission."""
    tags: list[str] = []
    repo_type = str(card.get("repo_type", "")).strip()
    if repo_type:
        tags.append(repo_type)
    for tool in [str(item).strip() for item in card.get("tool_names", [])]:
        if tool and tool.lower() not in {tag.lower() for tag in tags}:
            tags.append(tool)
        if len(tags) >= 6:
            break
    return tags


def _provider_has_credentials(config, provider_name: str | None) -> bool:
    """Return whether the selected provider has the credentials needed for runtime use."""
    if not provider_name:
        return False
    spec = next((item for item in PROVIDERS if item.name == provider_name), None)
    if spec and spec.is_oauth:
        return True
    provider_cfg = config.get_provider() if config.get_provider_name() else getattr(config.providers, provider_name, None)
    return bool(provider_cfg and getattr(provider_cfg, "api_key", ""))


def _build_runtime_status(
    *,
    agent_health: dict[str, Any],
    gateway_status: dict[str, str],
    last_restart_at: str,
) -> dict[str, str]:
    """Return the current high-level runtime state for the dashboard and chat."""
    if last_restart_at:
        try:
            restarted_at = datetime.fromisoformat(last_restart_at)
        except ValueError:
            restarted_at = None
        if restarted_at is not None and datetime.now(timezone.utc) - restarted_at <= timedelta(minutes=2):
            return {"label": "Restarted recently", "tone": "muted"}

    if agent_health.get("ok"):
        return {"label": "Online", "tone": "good"}
    if agent_health:
        return {"label": "Faulty", "tone": "bad"}
    if gateway_status.get("tone") == "good":
        return {"label": "Gateway only", "tone": "muted"}
    return {"label": "Offline", "tone": "bad"}


def _utc_now() -> str:
    """Return a compact UTC timestamp string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _join_command(command: str, args: list[str]) -> str:
    """Format a command plus args for display."""
    parts = [command.strip(), *[str(arg).strip() for arg in args if str(arg).strip()]]
    return " ".join(part for part in parts if part)


def _group_documents(documents: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Group editable markdown documents by their UI category."""
    groups: dict[str, list[dict[str, str]]] = {}
    for document in documents:
        groups.setdefault(document.get("group", "Other"), []).append(document)
    return [
        {"label": group_name, "documents": sorted(items, key=lambda item: item["label"])}
        for group_name, items in sorted(groups.items())
    ]


def _render_markdown_preview(content: str) -> str:
    """Render a lightweight markdown preview without extra dependencies."""
    text = content.strip()
    if not text:
        return "<p class='muted'>No content yet.</p>"

    lines = content.splitlines()
    blocks: list[str] = []
    paragraph: list[str] = []
    list_items: list[str] = []
    code_lines: list[str] = []
    in_code_block = False

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            blocks.append(f"<p>{_render_inline_markdown(' '.join(paragraph))}</p>")
            paragraph = []

    def flush_list() -> None:
        nonlocal list_items
        if list_items:
            blocks.append("<ul>" + "".join(list_items) + "</ul>")
            list_items = []

    def flush_code() -> None:
        nonlocal code_lines
        if code_lines:
            blocks.append(f"<pre><code>{html.escape(chr(10).join(code_lines))}</code></pre>")
            code_lines = []

    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.strip()

        if stripped.startswith("```"):
            flush_paragraph()
            flush_list()
            if in_code_block:
                flush_code()
                in_code_block = False
            else:
                in_code_block = True
            continue

        if in_code_block:
            code_lines.append(line)
            continue

        if not stripped:
            flush_paragraph()
            flush_list()
            continue

        if stripped.startswith(("#", "##", "###")):
            flush_paragraph()
            flush_list()
            level = max(1, min(3, len(stripped) - len(stripped.lstrip("#"))))
            heading = stripped[level:].strip()
            blocks.append(f"<h{level + 1}>{_render_inline_markdown(heading)}</h{level + 1}>")
            continue

        if stripped.startswith(("- ", "* ")):
            flush_paragraph()
            list_items.append(f"<li>{_render_inline_markdown(stripped[2:].strip())}</li>")
            continue

        paragraph.append(stripped)

    flush_paragraph()
    flush_list()
    if in_code_block:
        flush_code()

    return "".join(blocks) or "<p class='muted'>No content yet.</p>"


def _render_inline_markdown(text: str) -> str:
    """Render a small markdown subset for inline preview use."""
    escaped = html.escape(text)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", escaped)
    return escaped


def _build_chat_template_message(template_key: str, value: str) -> str:
    """Convert one small chat template form into a concrete prompt."""
    clean = value.strip()
    if not clean:
        raise ValueError("Fill in the template field before sending.")

    if template_key == "repo_analyze":
        return (
            f"Analyze the repository or project located at `{clean}`. "
            "Explain the purpose, likely architecture, key risks, and the most useful next steps."
        )
    if template_key == "error_explain":
        return (
            "Explain the following error in plain English, identify the most likely cause, "
            "and suggest the next debugging steps:\n\n"
            f"{clean}"
        )
    if template_key == "file_summarize":
        return (
            f"Open the file at `{clean}` inside the workspace. "
            "Summarize what it does, what is important, and any issues or follow-up actions."
        )
    raise ValueError("Unknown prompt template.")


def _store_chat_upload(upload: UploadFile, uploads_dir: Path, workspace: Path) -> dict[str, Any]:
    """Store one chat attachment in the workspace uploads directory."""
    raw_name = Path(upload.filename or "").name
    safe_name = safe_filename(raw_name) or "upload.bin"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    target = uploads_dir / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}-{safe_name}"
    upload.file.seek(0)
    with target.open("wb") as output:
        shutil.copyfileobj(upload.file, output)

    size_bytes = target.stat().st_size
    if size_bytes <= 0:
        target.unlink(missing_ok=True)
        raise ValueError("The uploaded file was empty.")
    if size_bytes > 10 * 1024 * 1024:
        target.unlink(missing_ok=True)
        raise ValueError("Files larger than 10 MB are not accepted in the chat upload form.")

    return {
        "name": safe_name,
        "path": str(target),
        "relative_path": str(target.relative_to(workspace)),
        "size_bytes": size_bytes,
        "size_label": _format_bytes(size_bytes),
        "modified_at": _utc_now(),
    }


def _format_bytes(size_bytes: int) -> str:
    """Format file sizes for the GUI."""
    units = ["B", "KB", "MB", "GB"]
    size = float(size_bytes)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{int(size_bytes)} B"


def _estimate_usage_cost(
    *,
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> float | None:
    """Return a rough USD cost when a local price profile is known."""
    price_profiles: dict[str, dict[str, float]] = {}
    profile = price_profiles.get(f"{provider}/{model}") or price_profiles.get(model)
    if not profile:
        return None
    return (
        (prompt_tokens / 1_000_000) * profile.get("prompt_usd_per_million", 0.0)
        + (completion_tokens / 1_000_000) * profile.get("completion_usd_per_million", 0.0)
    )


def _build_setup_progress(
    *,
    config,
    agent_health: dict[str, Any],
    installed_servers: list[dict[str, Any]],
    enabled_channels: list[str],
) -> list[dict[str, Any]]:
    """Return the setup checklist shown on the dashboard."""
    provider_name = config.get_provider_name() or config.agents.defaults.provider
    needs_configuration_server = next((server for server in installed_servers if server.get("missing_env")), None)
    installed_but_disabled = next(
        (server for server in installed_servers if server.get("status") == "active" and not server.get("enabled")),
        None,
    )
    return [
        {
            "key": "provider",
            "label": "Provider configured",
            "done": bool(provider_name and provider_name != "auto" and _provider_has_credentials(config, provider_name)),
            "detail": provider_name or "No provider selected",
            "href": "/setup/provider",
            "action_label": "Open Provider",
        },
        {
            "key": "model",
            "label": "Model configured",
            "done": bool(config.agents.defaults.model.strip()),
            "detail": config.agents.defaults.model or "No model configured",
            "href": "/setup/agent",
            "action_label": "Open Agent",
        },
        {
            "key": "agent",
            "label": "Agent runtime verified",
            "done": bool(agent_health.get("ok")),
            "detail": "Healthy" if agent_health.get("ok") else "Run the health check",
            "href": "/settings",
            "action_label": "Validate Setup",
        },
        {
            "key": "mcp_installed",
            "label": "MCP installed",
            "done": bool(installed_servers),
            "detail": f"{len(installed_servers)} installed",
            "href": "/mcp",
            "action_label": "Open MCP",
            "meta": {
                "needs_configuration": bool(needs_configuration_server),
                "installed_but_disabled": bool(installed_but_disabled),
                "action_href": (
                    f"/mcp/{needs_configuration_server['name']}" if needs_configuration_server
                    else f"/mcp/{installed_but_disabled['name']}" if installed_but_disabled
                    else "/mcp"
                ),
            },
        },
        {
            "key": "channel",
            "label": "Channel enabled",
            "done": bool(enabled_channels),
            "detail": ", ".join(enabled_channels) if enabled_channels else "No channel enabled yet",
            "href": "/setup/channel",
            "action_label": "Open Channel",
        },
    ]


def _determine_next_step(progress: list[dict[str, Any]]) -> dict[str, str]:
    """Return the most useful next-step callout for the dashboard."""
    mcp_step = next((item for item in progress if item["key"] == "mcp_installed"), None)
    if mcp_step and isinstance(mcp_step.get("meta"), dict):
        meta = mcp_step["meta"]
        if meta.get("needs_configuration"):
            return {
                "label": "MCP needs configuration",
                "description": "One installed MCP server is missing required values. Open it and fill in the missing configuration first.",
                "href": meta.get("action_href", "/mcp"),
                "action_label": "Fix MCP",
            }
        if meta.get("installed_but_disabled"):
            return {
                "label": "MCP installed but not enabled",
                "description": "A tested MCP server is ready, but it is not yet enabled for the main chat runtime.",
                "href": meta.get("action_href", "/mcp"),
                "action_label": "Enable MCP",
            }

    for item in progress:
        if item["done"]:
            continue
        descriptions = {
            "provider": "Add a working provider and API key before anything else can run.",
            "model": "Pick the default model and agent behavior that Nanobot should use.",
            "agent": "Run the health check so the GUI can confirm the runtime is usable.",
            "mcp_installed": "Install an MCP server so the agent has extra capabilities beyond the core toolset.",
            "channel": "Enable a delivery channel if you want Nanobot to respond outside the web GUI.",
        }
        return {
            "label": item["label"],
            "description": descriptions.get(item["key"], "Open the related settings page and finish this step."),
            "href": item["href"],
            "action_label": item["action_label"],
        }
    return {
        "label": "Start chatting",
        "description": "The runtime looks ready. Open the chat page or refine MCP configuration from the registry.",
        "href": "/chat",
        "action_label": "Open Chat",
    }


def _build_activity_feed(config_service: GUIConfigService, settings: GUISettings) -> list[dict[str, str]]:
    """Build a small dashboard activity feed from stored GUI state."""
    items: list[dict[str, str]] = []

    last_chat = config_service.get_last_successful_chat()
    if last_chat:
        items.append(
            {
                "title": "Chat executed",
                "at": str(last_chat.get("at", "")),
                "detail": str(last_chat.get("user_message", ""))[:180],
            }
        )

    last_mcp_test = config_service.get_last_mcp_test()
    if last_mcp_test:
        items.append(
            {
                "title": f"MCP test: {last_mcp_test.get('server_name', 'unknown')}",
                "at": str(last_mcp_test.get("checked_at", "")),
                "detail": str(last_mcp_test.get("status_label", "")),
            }
        )

    last_restart_at = config_service.get_last_restart_at()
    if last_restart_at:
        restart_action = _get_restart_action(settings)
        title = "Runtime restart requested" if restart_action["mode"] == "command" else "GUI restart requested"
        detail = (
            "The GUI ran the configured restart action for this deployment."
            if restart_action["mode"] == "command"
            else "The GUI process was asked to restart through its supervisor policy."
        )
        items.append(
            {
                "title": title,
                "at": last_restart_at,
                "detail": detail,
            }
        )

    active_doc = config_service.get_markdown_document(config_service.get_active_memory_doc())
    if active_doc:
        items.append(
            {
                "title": "Memory document selected",
                "at": "",
                "detail": active_doc["label"],
            }
        )

    for event in config_service.get_usage_events(limit=4):
        items.append(
            {
                "title": f"Usage recorded: {event.get('source', 'event')}",
                "at": str(event.get("timestamp", "")),
                "detail": " / ".join(
                    part for part in [str(event.get("provider", "")), str(event.get("model", ""))] if part
                ),
            }
        )

    items.sort(key=lambda item: item.get("at", ""), reverse=True)
    return items[:6]


def _classify_log_line(line: str) -> dict[str, str]:
    """Classify one GUI log line for filtering and highlighting."""
    upper = line.upper()
    if " ERROR " in upper or upper.startswith("ERROR"):
        return {"level": "error", "label": "ERROR", "line": line}
    if " WARNING " in upper or upper.startswith("WARNING"):
        return {"level": "warning", "label": "WARNING", "line": line}
    return {"level": "info", "label": "INFO", "line": line}


async def _validate_setup(
    *,
    config,
    config_service: GUIConfigService,
    gateway_health: dict[str, str],
    agent_health: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return a compact setup validation checklist for the settings page."""
    provider_name = config.get_provider_name() or config.agents.defaults.provider
    workspace_path = config.workspace_path
    config_path = config_service.config_path
    enabled_mcp = config_service.enabled_mcp_servers(config.tools.mcp_servers)
    workspace_parent = workspace_path if workspace_path.exists() else workspace_path.parent

    checks = [
        {
            "label": "Setup completion",
            "ok": config_service.is_setup_complete(),
            "detail": "The onboarding wizard is complete." if config_service.is_setup_complete() else "Provider, channel, or agent setup is still incomplete.",
            "hint": "Finish the wizard before using MCP automation." if not config_service.is_setup_complete() else "Ready for normal runtime use.",
            "action_label": "Open Provider Setup" if not config_service.is_setup_complete() else "",
            "action_url": "/setup/provider" if not config_service.is_setup_complete() else "",
        },
        {
            "label": "Provider credentials",
            "ok": _provider_has_credentials(config, provider_name),
            "detail": f"{provider_name or 'No provider selected'} is {'configured' if _provider_has_credentials(config, provider_name) else 'missing credentials'}.",
            "hint": "Verify API key, API base, and any custom headers." if not _provider_has_credentials(config, provider_name) else "Provider auth looks present.",
            "action_label": "Open Provider",
            "action_url": "/setup/provider",
        },
        {
            "label": "Agent runtime",
            "ok": bool(agent_health.get('ok')),
            "detail": f"{agent_health.get('provider', provider_name or 'provider')} / {agent_health.get('model', config.agents.defaults.model)}",
            "hint": agent_health.get("error", "Agent responded successfully.") if agent_health else "Run a validation check to confirm the provider works.",
            "action_label": "Open Agent",
            "action_url": "/setup/agent",
        },
        {
            "label": "Workspace path",
            "ok": workspace_path.exists() and workspace_path.is_dir() and os.access(workspace_parent, os.W_OK),
            "detail": str(workspace_path),
            "hint": "Nanobot must be able to read and write the workspace." if not (workspace_path.exists() and workspace_path.is_dir() and os.access(workspace_parent, os.W_OK)) else "Workspace is present and writable.",
            "action_label": "Open Agent",
            "action_url": "/setup/agent",
        },
        {
            "label": "Config file",
            "ok": config_path.exists(),
            "detail": str(config_path),
            "hint": "The runtime config file must stay readable on disk." if not config_path.exists() else "Config file is present.",
            "action_label": "",
            "action_url": "",
        },
        {
            "label": "Gateway health",
            "ok": gateway_health.get("tone") == "good",
            "detail": gateway_health.get("label", "Unknown"),
            "hint": "The gateway is optional, but this shows whether the headless runtime endpoint is reachable." if gateway_health.get("tone") != "good" else "Gateway health endpoint responded.",
            "action_label": "Open Status",
            "action_url": "/status",
        },
        {
            "label": "Tool execution",
            "ok": bool(config.tools.enabled) and config.tools.exec.timeout > 0,
            "detail": f"Tools {'enabled' if config.tools.enabled else 'disabled'}, timeout {config.tools.exec.timeout}s",
            "hint": "Enable tools and keep the timeout above zero for normal Nanobot behavior." if (not config.tools.enabled or config.tools.exec.timeout <= 0) else "Tool runtime is enabled with a valid timeout.",
            "action_label": "Open Settings",
            "action_url": "/settings",
        },
        {
            "label": "MCP runtime",
            "ok": not config.tools.mcp_servers or bool(enabled_mcp),
            "detail": f"{len(config.tools.mcp_servers)} installed, {len(enabled_mcp)} enabled",
            "hint": "Install and test MCP servers before enabling them for chat." if not enabled_mcp else "At least one MCP server is enabled for the main runtime.",
            "action_label": "Open MCP",
            "action_url": "/mcp",
        },
    ]
    return checks


def _next_validation_issue(validation_results: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the first validation item that still needs user action."""
    for item in validation_results:
        if not item.get("ok"):
            return item
    return None


async def _probe_gateway(health_url: str | None) -> dict[str, str]:
    """Probe the optional gateway health endpoint for the dashboard."""
    if not health_url:
        return {"label": "Not configured", "tone": "muted"}

    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            response = await client.get(health_url)
        if response.is_success:
            return {"label": "Connected", "tone": "good"}
        return {"label": f"Error {response.status_code}", "tone": "bad"}
    except Exception:
        return {"label": "Offline", "tone": "bad"}


def _normalize_update_repo(repo: str | None) -> str:
    """Normalize a GitHub repo slug or URL down to owner/repo."""
    value = str(repo or "").strip()
    if not value:
        return ""
    value = re.sub(r"^https?://github\.com/", "", value, flags=re.IGNORECASE).strip("/")
    if value.endswith(".git"):
        value = value[:-4]
    parts = [part for part in value.split("/") if part]
    if len(parts) >= 2:
        return "/".join(parts[:2])
    return value


def _version_sort_key(value: str) -> tuple[Any, ...]:
    """Return a comparable sort key for common Nanobot version tags."""
    normalized = str(value or "").strip().lower().lstrip("v")
    if not normalized:
        return (0,)
    try:
        from packaging.version import Version

        return (1, Version(normalized))
    except Exception:
        pass

    base, _, suffix = normalized.partition(".post")
    numbers = tuple(int(piece) for piece in re.findall(r"\d+", base))
    suffix_numbers = re.findall(r"\d+", suffix)
    post = int(suffix_numbers[0]) if suffix_numbers else -1
    return (0, numbers, post, normalized)


def _is_newer_version(candidate: str, current: str) -> bool:
    """Return True when the fetched version is newer than the running GUI version."""
    return _version_sort_key(candidate) > _version_sort_key(current)


def _parse_iso_timestamp(value: str | None) -> datetime | None:
    """Parse one persisted ISO timestamp safely."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _is_update_check_stale(update_status: dict[str, Any], *, hours: int) -> bool:
    """Return True when the cached update metadata should be refreshed."""
    checked_at = _parse_iso_timestamp(str(update_status.get("checked_at", "")))
    if checked_at is None:
        return True
    return datetime.now(timezone.utc) - checked_at >= timedelta(hours=max(hours, 1))


def _fetch_latest_release_info(repo: str) -> dict[str, str]:
    """Fetch the latest GitHub release, falling back to the newest tag when needed."""
    normalized_repo = _normalize_update_repo(repo)
    if not normalized_repo or "/" not in normalized_repo:
        raise ValueError("A valid GitHub owner/repo is required for update checks.")

    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or ""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "nanobot-webgui-update-check",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    base_url = f"https://api.github.com/repos/{normalized_repo}"
    with httpx.Client(timeout=4.5, headers=headers, follow_redirects=True) as client:
        release_response = client.get(f"{base_url}/releases/latest")
        if release_response.status_code == 404:
            tags_response = client.get(f"{base_url}/tags", params={"per_page": 1})
            tags_response.raise_for_status()
            tags = tags_response.json()
            if not isinstance(tags, list) or not tags:
                raise ValueError("No GitHub releases or tags were found for this repository.")
            tag_name = str(tags[0].get("name", "")).strip()
            if not tag_name:
                raise ValueError("The newest GitHub tag is missing a name.")
            return {
                "tag_name": tag_name,
                "latest_version": tag_name.lstrip("v"),
                "release_url": f"https://github.com/{normalized_repo}/releases/tag/{tag_name}",
                "release_notes_url": f"https://github.com/{normalized_repo}/releases/tag/{tag_name}",
                "release_name": tag_name,
                "published_at": "",
                "source": "github_tag",
            }

        release_response.raise_for_status()
        payload = release_response.json()
        tag_name = str(payload.get("tag_name", "")).strip()
        if not tag_name:
            raise ValueError("The latest GitHub release is missing a tag name.")
        release_url = str(payload.get("html_url", "")).strip()
        return {
            "tag_name": tag_name,
            "latest_version": tag_name.lstrip("v"),
            "release_url": release_url,
            "release_notes_url": release_url,
            "release_name": str(payload.get("name", "")).strip() or tag_name,
            "published_at": str(payload.get("published_at", "")).strip(),
            "source": "github_release",
        }


def _ensure_update_status(
    settings: GUISettings,
    config_service: GUIConfigService,
    logger: logging.Logger,
    *,
    force: bool = False,
    user_present: bool = False,
) -> dict[str, Any]:
    """Return cached update metadata and refresh it at most once per configured interval."""
    repo = _normalize_update_repo(settings.update_repo)
    cached = config_service.get_update_status()
    base_status = {
        "enabled": bool(settings.update_check_enabled and repo),
        "current_version": __version__,
        "latest_version": str(cached.get("latest_version", "")),
        "tag_name": str(cached.get("tag_name", "")),
        "available": bool(cached.get("available", False)),
        "checked_at": str(cached.get("checked_at", "")),
        "release_url": str(cached.get("release_url", "")),
        "release_notes_url": str(cached.get("release_notes_url", "")),
        "release_name": str(cached.get("release_name", "")),
        "published_at": str(cached.get("published_at", "")),
        "source": str(cached.get("source", "")),
        "repo": repo,
        "error": str(cached.get("error", "")),
        "updating": bool(cached.get("updating", False)),
        "last_update_request_at": str(cached.get("last_update_request_at", "")),
        "last_update_error": str(cached.get("last_update_error", "")),
    }

    if not base_status["enabled"]:
        if cached != base_status:
            return config_service.set_update_status(base_status)
        return base_status

    if base_status["latest_version"]:
        base_status["available"] = _is_newer_version(base_status["latest_version"], __version__)
        if not base_status["available"]:
            base_status["updating"] = False

    should_refresh = force or base_status["repo"] != str(cached.get("repo", ""))
    should_refresh = should_refresh or base_status["current_version"] != str(cached.get("current_version", ""))
    should_refresh = should_refresh or _is_update_check_stale(base_status, hours=settings.update_check_interval_hours)

    if not user_present and not force and not should_refresh:
        if cached != base_status:
            return config_service.set_update_status(base_status)
        return base_status

    if not should_refresh:
        if cached != base_status:
            return config_service.set_update_status(base_status)
        return base_status

    try:
        fetched = _fetch_latest_release_info(repo)
        refreshed = {
            **base_status,
            **fetched,
            "enabled": True,
            "available": _is_newer_version(str(fetched.get("latest_version", "")), __version__),
            "checked_at": _utc_now(),
            "repo": repo,
            "error": "",
            "updating": bool(base_status["updating"])
            and _is_newer_version(str(fetched.get("latest_version", "")), __version__),
            "last_update_error": str(base_status.get("last_update_error", "")),
        }
        return config_service.set_update_status(refreshed)
    except Exception as exc:
        logger.warning("update_check_failed repo=%s error=%s", repo, exc)
        return config_service.set_update_status(
            {
                **base_status,
                "checked_at": _utc_now(),
                "error": str(exc),
            }
        )


def _get_update_action(settings: GUISettings, update_status: dict[str, Any]) -> dict[str, Any]:
    """Describe the update action supported by this deployment."""
    mode = str(settings.update_mode or "").strip().lower()
    command = str(settings.update_command or "").strip()

    if not mode:
        mode = "command" if command else "disabled"

    if not update_status.get("enabled"):
        return {
            "enabled": False,
            "mode": "disabled",
            "label": "Updates disabled",
            "description": "GitHub update checks are disabled for this deployment.",
            "command": "",
        }
    if not update_status.get("available"):
        return {
            "enabled": False,
            "mode": "disabled",
            "label": "Up to date",
            "description": "No newer GUI version is available right now.",
            "command": "",
        }
    if mode == "command" and command:
        return {
            "enabled": True,
            "mode": "command",
            "label": "Update now",
            "description": "Run the configured deployment update command now.",
            "command": command,
        }
    return {
        "enabled": False,
        "mode": "disabled",
        "label": "Update unavailable",
        "description": "A new version is available, but no update command is configured for this deployment.",
        "command": "",
    }


def _get_repair_action(settings: GUISettings, repair_plan: dict[str, Any]) -> dict[str, Any]:
    """Describe the MCP repair action supported by this deployment."""
    mode = str(settings.repair_mode or "").strip().lower()
    command = str(settings.repair_command or "").strip()
    recipe = str(repair_plan.get("recommended_recipe", "")).strip()

    if not mode:
        mode = "command" if command else "disabled"

    if not repair_plan.get("supported"):
        return {
            "enabled": False,
            "mode": "disabled",
            "label": "Repair unavailable",
            "description": "No supported repair recipe is available for this MCP right now.",
            "command": "",
        }
    if mode == "command" and command:
        return {
            "enabled": True,
            "mode": "command",
            "label": "Apply unrestricted repair" if recipe == "unrestricted_agent_shell" else "Apply supported repair",
            "description": "Run the configured MCP repair worker for this deployment.",
            "command": command,
        }
    return {
        "enabled": False,
        "mode": "disabled",
        "label": "Repair unavailable",
        "description": "Repair worker command is not configured for this deployment.",
        "command": "",
    }


def _get_restart_action(settings: GUISettings) -> dict[str, Any]:
    """Describe the restart action supported by this deployment."""
    mode = str(settings.restart_mode or "").strip().lower()
    command = str(settings.restart_command or "").strip()

    if not mode:
        mode = "command" if command else "disabled"

    if mode == "command" and command:
        return {
            "enabled": True,
            "mode": "command",
            "label": "Restart Runtime",
            "description": "Run the configured restart action for this deployment.",
            "command": command,
        }
    if mode == "self":
        return {
            "enabled": True,
            "mode": "self",
            "label": "Restart GUI",
            "description": "Restart this GUI process. Requires Docker restart policy or another supervisor.",
            "command": "",
        }
    return {
        "enabled": False,
        "mode": "disabled",
        "label": "Restart unavailable",
        "description": "Restart is not configured for this deployment.",
        "command": "",
    }


def _restart_process(logger: logging.Logger) -> None:
    """Restart the GUI process by exiting and relying on Docker restart policy."""
    time.sleep(0.8)
    logger.warning("instance_restart_executing")
    os._exit(0)


def _run_restart_command(command: str, logger: logging.Logger) -> None:
    """Run the configured external restart command."""
    time.sleep(0.8)
    logger.warning("instance_restart_command_executing command=%s", command)
    try:
        result = subprocess.run(
            shlex.split(command),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception:
        logger.exception("instance_restart_command_failed command=%s", command)
        return

    if result.stdout.strip():
        logger.info("instance_restart_command_stdout %s", result.stdout.strip())
    if result.stderr.strip():
        logger.warning("instance_restart_command_stderr %s", result.stderr.strip())
    if result.returncode != 0:
        logger.error("instance_restart_command_exit code=%s command=%s", result.returncode, command)


def _run_update_command(
    command: str,
    logger: logging.Logger,
    config_service: GUIConfigService,
    target_version: str,
) -> None:
    """Run the configured external update command."""
    time.sleep(0.8)
    logger.warning("instance_update_command_executing command=%s target=%s", command, target_version)
    try:
        result = subprocess.run(
            shlex.split(command),
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except Exception:
        logger.exception("instance_update_command_failed command=%s", command)
        current = config_service.get_update_status()
        config_service.set_update_status(
            {
                **current,
                "updating": False,
                "last_update_error": "Failed to execute the configured update command.",
            }
        )
        return

    if result.stdout.strip():
        logger.info("instance_update_command_stdout %s", result.stdout.strip())
    if result.stderr.strip():
        logger.warning("instance_update_command_stderr %s", result.stderr.strip())

    current = config_service.get_update_status()
    if result.returncode != 0:
        logger.error("instance_update_command_exit code=%s command=%s", result.returncode, command)
        error_hint = result.stderr.strip() or result.stdout.strip() or f"Update command exited with {result.returncode}."
        config_service.set_update_status(
            {
                **current,
                "updating": False,
                "last_update_error": error_hint[:500],
            }
        )
        return

    config_service.set_update_status(
        {
            **current,
            "updating": False,
            "last_update_error": "",
        }
    )


def _run_mcp_repair_command(
    command: str,
    logger: logging.Logger,
    config_service: GUIConfigService,
    mcp_service: GUIMCPService,
    server_name: str,
    repair_plan: dict[str, Any],
) -> None:
    """Run the configured external MCP repair command and persist the result."""
    recipe = str(repair_plan.get("recommended_recipe", "")).strip()
    time.sleep(0.4)
    logger.warning("mcp_repair_command_executing server=%s recipe=%s command=%s", server_name, recipe, command)

    record = config_service.get_mcp_record(server_name)
    config_service.set_mcp_record(
        server_name,
        {
            **record,
            "repair_status": "running",
            "repair_status_label": "Repair running",
        },
    )

    env = os.environ.copy()
    env.update(
        {
            "NANOBOT_REPAIR_PLAN_JSON": json.dumps(repair_plan),
            "NANOBOT_REPAIR_RECIPE": recipe,
            "NANOBOT_REPAIR_ALLOW_UNRESTRICTED": "1"
            if recipe == "unrestricted_agent_shell"
            else "0",
            "NANOBOT_REPAIR_SHELL_COMMAND": str(repair_plan.get("shell_command", "")),
            "NANOBOT_REPAIR_SERVER": server_name,
            "NANOBOT_CONFIG_PATH": str(config_service.config_path),
            "NANOBOT_WORKSPACE": str(config_service.default_workspace),
        }
    )

    try:
        result = subprocess.run(
            shlex.split(command),
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
            env=env,
        )
    except Exception:
        logger.exception("mcp_repair_command_failed server=%s recipe=%s", server_name, recipe)
        current = config_service.get_mcp_record(server_name)
        config_service.set_mcp_record(
            server_name,
            {
                **current,
                "repair_status": "error",
                "repair_status_label": "Repair failed",
                "repair_finished_at": _utc_now(),
                "repair_recipe": recipe,
                "repair_log_tail": _append_log(
                    str(current.get("repair_log_tail", "")).strip(),
                    "Repair worker failed to execute.",
                ),
            },
        )
        return

    combined_log = "\n\n".join(
        chunk for chunk in [result.stdout.strip(), result.stderr.strip()] if chunk
    ) or "(no repair worker output)"
    refreshed = mcp_service.refresh_runtime_requirements(server_name) if result.returncode == 0 else config_service.get_mcp_record(server_name)
    config_service.set_mcp_record(
        server_name,
        {
            **refreshed,
            "repair_status": "ok" if result.returncode == 0 else "error",
            "repair_status_label": "Repair applied" if result.returncode == 0 else "Repair failed",
            "repair_finished_at": _utc_now(),
            "repair_recipe": recipe,
            "repair_available_recipes": repair_plan.get("available_recipes", []),
            "repair_log_tail": _append_log(
                str(refreshed.get("repair_log_tail", "")).strip(),
                combined_log[:5000],
            ),
        },
    )
    if result.returncode != 0:
        logger.error("mcp_repair_command_exit code=%s server=%s recipe=%s", result.returncode, server_name, recipe)


def _community_error_code(raw_error: str) -> str:
    """Reduce one MCP error down to a small telemetry-safe code."""
    value = str(raw_error or "").strip().lower()
    if not value:
        return ""
    if "timeout" in value:
        return "timeout"
    if "unauthorized" in value or "401" in value or "authentication" in value:
        return "auth"
    if "missing required environment variables" in value or "missing" in value:
        return "missing_env"
    if "enoent" in value or "not found" in value:
        return "missing_runtime"
    return "probe_failed"


def _community_timeout_bucket(raw_timeout: Any) -> str:
    """Bucket the configured tool timeout for anonymous telemetry."""
    try:
        timeout_value = int(raw_timeout or 0)
    except (TypeError, ValueError):
        timeout_value = 0
    if timeout_value <= 0:
        return ""
    if timeout_value <= 30:
        return "0-30"
    if timeout_value <= 60:
        return "31-60"
    if timeout_value <= 120:
        return "61-120"
    return "120+"


def _render_reconnect_page(*, title: str, message: str, redirect_url: str, status_code: int = 202) -> HTMLResponse:
    """Render a small reconnect page for restart and update flows."""
    safe_title = html.escape(title)
    safe_message = html.escape(message)
    safe_redirect = json.dumps(str(redirect_url or "/"))
    return HTMLResponse(
        f"""
        <!DOCTYPE html>
        <html lang="en">
          <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>{safe_title}</title>
            <style>
              body {{ font-family: Segoe UI, sans-serif; padding: 48px; background: #f4efe6; color: #1d1a17; }}
              .card {{ max-width: 560px; margin: 0 auto; padding: 24px; border-radius: 18px; background: #fffdf8; border: 1px solid rgba(57,42,27,0.12); }}
              .muted {{ color: #6f675f; }}
            </style>
          </head>
          <body>
            <div class="card">
              <h1>{safe_title}</h1>
              <p>{safe_message}</p>
              <p class="muted" id="reconnect-status">Waiting for the GUI to respond again...</p>
            </div>
            <script>
              const redirectUrl = {safe_redirect};
              async function reconnectWhenReady() {{
                try {{
                  const response = await fetch('/health', {{ cache: 'no-store' }});
                  if (response.ok) {{
                    window.location.href = redirectUrl;
                    return;
                  }}
                }} catch (_error) {{}}
                window.setTimeout(reconnectWhenReady, 3000);
              }}
              window.setTimeout(reconnectWhenReady, 1500);
            </script>
          </body>
        </html>
        """,
        status_code=status_code,
    )


def _setup_logger(log_file: Path) -> logging.Logger:
    """Configure a rotating file logger for GUI actions."""
    logger = logging.getLogger("nanobot.gui")
    if logger.handlers:
        return logger

    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = RotatingFileHandler(log_file, maxBytes=512_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger
