"""FastAPI-powered web GUI for nanobot."""

from __future__ import annotations

import html
import json
import logging
import os
import re
import shutil
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

from nanobot.config.schema import MCPServerConfig
from nanobot.gui.agent_service import GUIAgentService
from nanobot.gui.auth import AdminUser, AuthService
from nanobot.gui.config_service import GUIConfigService
from nanobot.gui.error_utils import explain_error
from nanobot.gui.mcp_service import GUIMCPService
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
    gateway_health_url: str | None = None
    https_only_cookies: bool = False


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

    app = FastAPI(title="nanobot GUI", version="0.2.0")
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

        config_service.set_last_restart_at(_utc_now())
        gui_logger.warning("instance_restart_requested by=%s", user.username)
        background_tasks.add_task(_restart_process, gui_logger)
        return HTMLResponse(
            """
            <!DOCTYPE html>
            <html lang="en">
              <head>
                <meta charset="utf-8">
                <meta http-equiv="refresh" content="5;url=/">
                <title>Restarting nanobot</title>
                <style>
                  body { font-family: Segoe UI, sans-serif; padding: 48px; background: #f4efe6; color: #1d1a17; }
                  .card { max-width: 560px; margin: 0 auto; padding: 24px; border-radius: 18px; background: #fffdf8; border: 1px solid rgba(57,42,27,0.12); }
                </style>
              </head>
              <body>
                <div class="card">
                  <h1>Restart requested</h1>
                  <p>The nanobot dev instance is restarting now. This page will try to reconnect automatically.</p>
                </div>
              </body>
            </html>
            """,
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
        if isinstance(avatar_value, UploadFile) and avatar_value.filename:
            avatar_path = _store_avatar(avatar_value, config_service.avatars_dir)

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
                "activity_feed": _build_activity_feed(config_service),
                "quick_actions": [
                    {"label": "Open Chat", "href": "/chat"},
                    {"label": "Add MCP", "href": "/mcp"},
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
            preview = await mcp_service.analyze_repository(query)
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
            record = await mcp_service.install_repository(query)
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
            clear_error()
            _set_flash(request, f"MCP server '{record['server_name']}' installed and active.")
        elif record["status"] == "needs_configuration":
            friendly = store_error(record["last_error"], context="mcp", server_name=record["server_name"])
            record["friendly_error"] = friendly
            config_service.set_mcp_record(record["server_name"], record)
            _set_flash(
                request,
                f"MCP server '{record['server_name']}' installed, but it still needs configuration: {record['last_error']}",
                level="error",
            )
        else:
            friendly = store_error(record["last_error"], context="mcp", server_name=record["server_name"])
            record["friendly_error"] = friendly
            config_service.set_mcp_record(record["server_name"], record)
            _set_flash(
                request,
                f"MCP server '{record['server_name']}' installed, but the runtime probe failed: {record['last_error']}",
                level="error",
            )
        return RedirectResponse("/mcp", status_code=303)

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

        config_service.set_last_mcp_test(
            {
                "server_name": server_name,
                "status": record["status"],
                "status_label": record["status_label"],
                "checked_at": record.get("last_checked_at", ""),
            }
        )
        if record["status"] == "active":
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
        return render_mcp_detail_page(
            request,
            user,
            config=config,
            server_name=server_name,
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
            return render_mcp_detail_page(
                request,
                user,
                config=config,
                server_name=server_name,
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
                status_code=400,
            )

        config_service.save(config)
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
    shell_context = {
        "instance_name": settings.instance_name,
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
        "flash": context.get("flash") or _pop_flash(request),
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
    if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        content_type = upload.content_type or ""
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
        "summary": str(record.get("summary", "")).strip(),
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


def _build_activity_feed(config_service: GUIConfigService) -> list[dict[str, str]]:
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
        items.append(
            {
                "title": "Runtime restart requested",
                "at": last_restart_at,
                "detail": "The GUI asked Docker to restart the runtime container.",
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


def _restart_process(logger: logging.Logger) -> None:
    """Restart the GUI process by exiting and relying on Docker restart policy."""
    time.sleep(0.8)
    logger.warning("instance_restart_executing")
    os._exit(0)


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
