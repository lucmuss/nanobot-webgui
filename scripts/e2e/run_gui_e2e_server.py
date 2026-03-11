"""Start an isolated nanobot GUI instance for Playwright E2E tests."""

from __future__ import annotations

import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import uvicorn
from fastapi.responses import JSONResponse

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.helpers.mcp_fixtures import build_mcp_fixture_analysis, load_mcp_fixture


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fake_usage(content: str) -> dict[str, int]:
    prompt_tokens = max(8, len(content.split()) * 4)
    completion_tokens = max(12, min(96, len(content) // 4 + 12))
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def _ensure_clean_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def _fixture_name_from_repo_key(repo_key: str) -> str:
    """Resolve one repo key to a local MCP fixture directory."""
    fixture_root = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "mcp"
    candidate = fixture_root / repo_key / "fixture.json"
    if candidate.exists():
        return repo_key
    return "echo-mcp"


def _install_e2e_harness(
    app,
    runtime_dir: Path,
    workspace_dir: Path,
    *,
    gui_app_module,
    MCPServerConfig,
    SessionManager,
    parse_repository_source,
) -> None:
    config_service = app.state.config_service
    auth_service = app.state.auth_service
    agent_service = app.state.agent_service
    mcp_service = app.state.mcp_service
    gui_logger = app.state.gui_logger

    allowed_providers = {spec.name for spec in gui_app_module.PROVIDERS}

    def reset_instance() -> None:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        logs_dir = runtime_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        (logs_dir / "gui.log").write_text("", encoding="utf-8")
        for child in runtime_dir.iterdir():
            if child.name == "logs":
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)
        _ensure_clean_dir(workspace_dir)
        config_service.ensure_instance()
        auth_service.init_db()
        auth_service.ensure_session_secret()
        config_service.set_setup_complete(False)
        config_service.set_agent_health({})
        config_service.clear_last_error()
        agent_service.invalidate()

    def current_health() -> dict[str, Any]:
        config = config_service.load()
        provider_name = config.get_provider_name() or config.agents.defaults.provider
        model = str(config.agents.defaults.model or "").strip()
        provider_cfg = getattr(config.providers, provider_name, None) if provider_name else None
        api_key = str(getattr(provider_cfg, "api_key", "") or "").strip() if provider_cfg else ""

        if not provider_name or provider_name == "auto" or provider_name not in allowed_providers:
            return {
                "ok": False,
                "provider": provider_name or "none",
                "model": model,
                "latency_ms": 11,
                "error": "Invalid provider configured.",
                "checked_at": _utc_now(),
            }
        if not model:
            return {
                "ok": False,
                "provider": provider_name,
                "model": model,
                "latency_ms": 12,
                "error": "No model configured.",
                "checked_at": _utc_now(),
            }
        if not api_key:
            return {
                "ok": False,
                "provider": provider_name,
                "model": model,
                "latency_ms": 13,
                "error": "Missing Authentication header",
                "checked_at": _utc_now(),
            }
        return {
            "ok": True,
            "provider": provider_name,
            "model": model,
            "latency_ms": 24,
            "checked_at": _utc_now(),
            "usage": {"prompt_tokens": 9, "completion_tokens": 6, "total_tokens": 15},
            "preview": "OK",
        }

    def build_fake_mcp_analysis(source: str) -> dict[str, Any]:
        repo = parse_repository_source(source)
        repo_key = repo["repo"].lower()
        if repo_key in {"missing", "not-found", "404-repo"}:
            raise ValueError("Repository not found on GitHub.")
        fixture_name = _fixture_name_from_repo_key(repo_key)
        return build_mcp_fixture_analysis(fixture_name=fixture_name, repo=repo)

    async def fake_check_runtime() -> dict[str, Any]:
        return current_health()

    async def fake_analyze_repository(source: str, **_: object) -> dict[str, Any]:
        return build_fake_mcp_analysis(source)

    async def fake_install_repository(source: str, **_: object) -> dict[str, Any]:
        analysis = build_fake_mcp_analysis(source)
        fixture_name = _fixture_name_from_analysis(analysis)
        fixture = load_mcp_fixture(fixture_name)
        install_dir = config_service.mcp_installs_dir / analysis["install_slug"]
        install_dir.mkdir(parents=True, exist_ok=True)
        (install_dir / "README.md").write_text(
            f"# {analysis['server_name']}\n\nInstalled by the isolated E2E harness.\n",
            encoding="utf-8",
        )

        config = config_service.load()
        existing = config.tools.mcp_servers.get(analysis["server_name"])
        env = dict(existing.env) if existing else {}
        config.tools.mcp_servers[analysis["server_name"]] = MCPServerConfig(
            type="stdio",
            command=analysis["run_command"],
            args=list(analysis["run_args"]),
            env=env,
            url="",
            headers={},
            tool_timeout=30,
        )
        config_service.save(config)

        existing_record = config_service.get_mcp_record(analysis["server_name"])
        provisional = {
            "server_name": analysis["server_name"],
            "title": analysis["title"],
            "summary": analysis["summary"],
            "repo_url": analysis["repo_url"],
            "clone_url": analysis["clone_url"],
            "install_dir": str(install_dir),
            "install_steps": [step["display"] for step in analysis["install_steps"]],
            "required_env": list(analysis["required_env"]),
            "optional_env": list(analysis["optional_env"]),
            "healthcheck": analysis["healthcheck"],
            "evidence": list(analysis["evidence"]),
            "repo_type": analysis.get("repo_type", "fixture"),
            "analysis_mode": analysis.get("analysis_mode", "deterministic"),
            "analysis_confidence": analysis.get("analysis_confidence", 0.95),
            "required_runtimes": list(analysis.get("required_runtimes", [])),
            "runtime_status": list(analysis.get("runtime_status", [])),
            "missing_runtimes": list(analysis.get("missing_runtimes", [])),
            "next_action": analysis.get("next_action", ""),
            "last_installed_at": _utc_now(),
            "enabled": bool(existing_record.get("enabled", False)),
            "tool_names": list(analysis["tool_names"]),
            "probe_error": str(fixture.get("probe_error", "")).strip(),
            "log_tail": "Installed by the E2E MCP harness.",
        }
        config_service.set_mcp_record(analysis["server_name"], provisional)
        record = await fake_test_server(analysis["server_name"])
        record.update(
            {
                "server_name": analysis["server_name"],
                "title": analysis["title"],
                "summary": analysis["summary"],
                "repo_url": analysis["repo_url"],
                "clone_url": analysis["clone_url"],
                "install_dir": str(install_dir),
                "install_steps": [step["display"] for step in analysis["install_steps"]],
                "required_env": list(analysis["required_env"]),
                "optional_env": list(analysis["optional_env"]),
                "healthcheck": analysis["healthcheck"],
                "evidence": list(analysis["evidence"]),
                "repo_type": analysis.get("repo_type", "fixture"),
                "analysis_mode": analysis.get("analysis_mode", "deterministic"),
                "analysis_confidence": analysis.get("analysis_confidence", 0.95),
                "required_runtimes": list(analysis.get("required_runtimes", [])),
                "runtime_status": list(analysis.get("runtime_status", [])),
                "missing_runtimes": list(analysis.get("missing_runtimes", [])),
                "next_action": analysis.get("next_action", ""),
                "last_installed_at": _utc_now(),
                "enabled": bool(config_service.get_mcp_record(analysis["server_name"]).get("enabled", False)),
            }
        )
        config_service.set_mcp_record(analysis["server_name"], record)
        return record

    async def fake_test_server(server_name: str) -> dict[str, Any]:
        config = config_service.load()
        cfg = config.tools.mcp_servers.get(server_name)
        if cfg is None:
            raise ValueError(f"MCP server '{server_name}' is not registered.")

        existing = config_service.get_mcp_record(server_name)
        required_env = [str(item) for item in existing.get("required_env", [])]
        missing_env = [name for name in required_env if not str(cfg.env.get(name, "")).strip()]
        expected_tools = [str(item) for item in existing.get("tool_names", [])]
        probe_error = str(existing.get("probe_error", "")).strip()
        log_tail = str(existing.get("log_tail", "")).strip()
        base_record = {
            **existing,
            "server_name": server_name,
            "transport": "stdio",
            "command": cfg.command,
            "args": list(cfg.args),
            "url": cfg.url,
            "tool_timeout": cfg.tool_timeout,
            "missing_env": missing_env,
            "tool_names": expected_tools,
            "last_checked_at": _utc_now(),
            "enabled": bool(existing.get("enabled", False)),
            "friendly_error": {},
            "log_tail": log_tail,
        }
        if missing_env:
            result = {
                **base_record,
                "status": "needs_configuration",
                "status_label": "Needs configuration",
                "last_test_status": "needs_configuration",
                "last_test_label": "Needs configuration",
                "last_error": "Missing required environment variables: " + ", ".join(missing_env),
            }
            config_service.set_mcp_record(server_name, result)
            return result

        if probe_error or cfg.env.get("FORCE_FAIL") == "1":
            result = {
                **base_record,
                "status": "error",
                "status_label": "Probe failed",
                "last_test_status": "error",
                "last_test_label": "Probe failed",
                "last_error": probe_error or "Connection failed: fake MCP probe failure.",
            }
            config_service.set_mcp_record(server_name, result)
            return result

        result = {
            **base_record,
            "status": "active",
            "status_label": "Active",
            "last_test_status": "active",
            "last_test_label": "Active",
            "last_error": "",
            "tool_names": expected_tools or ["echo_message"],
        }
        config_service.set_mcp_record(server_name, result)
        return result

    def _fixture_name_from_analysis(analysis: dict[str, Any]) -> str:
        for item in analysis.get("evidence", []):
            if isinstance(item, str) and item.startswith("fixture:"):
                return item.split(":", 1)[1]
        return "echo-mcp"

    async def fake_send_message(admin, content: str) -> dict[str, Any]:
        health = current_health()
        if not health["ok"]:
            raise ValueError(str(health["error"]))

        config = config_service.load()
        workspace = config.workspace_path
        workspace.mkdir(parents=True, exist_ok=True)
        session_manager = SessionManager(workspace)
        session = session_manager.get_or_create(agent_service._session_key(admin))
        session.add_message("user", content)

        enabled_servers = config_service.enabled_mcp_servers(config.tools.mcp_servers)
        active_records = [config_service.get_mcp_record(name) for name in enabled_servers]
        tool_calls: list[dict[str, Any]] = []
        assistant_text = "E2E assistant response."

        file_match = re.search(r"`([^`]+)`", content)
        relative_path = file_match.group(1) if file_match else ""
        candidate = workspace / relative_path if relative_path else None
        if candidate and candidate.exists() and candidate.is_file():
            preview = candidate.read_text(encoding="utf-8", errors="replace").strip()[:120]
            assistant_text = f"Read uploaded file `{relative_path}`. Preview: {preview or '(empty file)'}"
            tool_calls = [
                {
                    "id": "tool-read-file",
                    "type": "function",
                    "function": {"name": "read_file"},
                }
            ]
        elif content.startswith("Analyze the repository or project located at"):
            assistant_text = "Repository analysis ready. Purpose, architecture, and next steps were summarized."
        elif content.startswith("Explain the following error in plain English"):
            assistant_text = "Plain-English error explanation ready with likely cause and next debugging steps."
        elif content.startswith("Open the file at"):
            assistant_text = "Workspace file summary ready with key behavior and follow-up actions."
            tool_calls = [
                {
                    "id": "tool-open-workspace-file",
                    "type": "function",
                    "function": {"name": "read_file"},
                }
            ]
        elif active_records:
            first_record = active_records[0]
            tool_name = (
                first_record.get("tool_names", ["echo_message"])[0]
                if isinstance(first_record.get("tool_names"), list)
                else "echo_message"
            )
            assistant_text = (
                f"Active MCP servers: {', '.join(sorted(enabled_servers.keys()))}. "
                f"Used tool `{tool_name}` successfully."
            )
            tool_calls = [
                {
                    "id": "tool-active-mcp",
                    "type": "function",
                    "function": {"name": tool_name},
                }
            ]
        else:
            assistant_text = "The fake E2E runtime responded successfully without MCP tools."

        if tool_calls:
            session.add_message("assistant", assistant_text, tool_calls=tool_calls)
        else:
            session.add_message("assistant", assistant_text)
        session_manager.save(session)

        return {
            "content": assistant_text,
            "usage": _fake_usage(content),
            "provider": health["provider"],
            "model": health["model"],
        }

    async def fake_send_mcp_test_message(admin, server_name: str, content: str) -> dict[str, Any]:
        record = await fake_test_server(server_name)
        if record["status"] != "active":
            raise ValueError(record["last_error"] or "Run a successful MCP test before chatting.")

        config = config_service.load()
        session_manager = SessionManager(config.workspace_path)
        session = session_manager.get_or_create(agent_service._mcp_test_session_key(admin, server_name))
        session.add_message("user", content)
        tool_name = (record.get("tool_names") or ["echo_message"])[0]
        assistant_text = f"MCP `{server_name}` replied successfully using `{tool_name}`."
        session.add_message(
            "assistant",
            assistant_text,
            tool_calls=[
                {
                    "id": f"tool-{server_name}",
                    "type": "function",
                    "function": {"name": tool_name},
                }
            ],
        )
        session_manager.save(session)
        return {
            "content": assistant_text,
            "usage": _fake_usage(content),
            "provider": config.get_provider_name() or config.agents.defaults.provider,
            "model": config.agents.defaults.model,
        }

    def fake_restart_process(logger) -> None:
        logger.warning("instance_restart_stubbed")

    agent_service.check_runtime = fake_check_runtime
    agent_service.send_message = fake_send_message
    agent_service.send_mcp_test_message = fake_send_mcp_test_message
    mcp_service.analyze_repository = fake_analyze_repository
    mcp_service.install_repository = fake_install_repository
    mcp_service.test_server = fake_test_server
    gui_app_module._restart_process = fake_restart_process

    @app.post("/__e2e/reset")
    async def e2e_reset() -> JSONResponse:
        reset_instance()
        return JSONResponse({"ok": True})

    reset_instance()


def main() -> None:
    import nanobot.gui.app as gui_app_module
    from nanobot.config.schema import MCPServerConfig
    from nanobot.gui.app import GUISettings, create_gui_app
    from nanobot.gui.mcp_service import _parse_repository_source
    from nanobot.session.manager import SessionManager

    temp_root = ROOT / "tmp" / "e2e"
    runtime_dir = temp_root / "gui-runtime"
    workspace_dir = temp_root / "workspace"
    port = int(os.getenv("NANOBOT_GUI_E2E_PORT", "18795"))
    host = os.getenv("NANOBOT_GUI_E2E_HOST", "127.0.0.1")

    temp_root.mkdir(parents=True, exist_ok=True)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    settings = GUISettings(
        config_path=runtime_dir / "config.json",
        workspace=str(workspace_dir),
        host=host,
        port=port,
        instance_name="nanobot-e2e",
        public_url=os.getenv("NANOBOT_GUI_PUBLIC_URL", "").strip() or None,
        restart_mode="self",
        update_check_enabled=False,
        community_api_url=os.getenv("NANOBOT_GUI_COMMUNITY_API_URL", "").strip() or None,
        community_public_url=os.getenv("NANOBOT_GUI_COMMUNITY_PUBLIC_URL", "").strip() or None,
        community_api_token=os.getenv("NANOBOT_GUI_COMMUNITY_API_TOKEN", "").strip() or None,
    )
    app = create_gui_app(settings)
    _install_e2e_harness(
        app,
        runtime_dir,
        workspace_dir,
        gui_app_module=gui_app_module,
        MCPServerConfig=MCPServerConfig,
        SessionManager=SessionManager,
        parse_repository_source=_parse_repository_source,
    )
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
