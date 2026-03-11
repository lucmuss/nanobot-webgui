import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nanobot.config.schema import MCPServerConfig
from nanobot.gui.app import GUISettings, create_gui_app
from nanobot.gui.mcp_service import _parse_repository_source
from tests.helpers.mcp_fixtures import build_mcp_fixture_analysis, load_mcp_fixture


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9pSdz+gAAAAASUVORK5CYII="
)


def _make_client(
    tmp_path: Path,
    *,
    update_check_enabled: bool = False,
    update_mode: str = "disabled",
    update_command: str | None = None,
    community_api_url: str | None = None,
    community_api_token: str | None = None,
) -> tuple[TestClient, object]:
    config_path = tmp_path / "runtime" / "config.json"
    workspace = tmp_path / "workspace"
    app = create_gui_app(
        GUISettings(
            config_path=config_path,
            workspace=str(workspace),
            host="127.0.0.1",
            port=18795,
            instance_name="nanobot-test",
            gateway_health_url=None,
            update_check_enabled=update_check_enabled,
            update_mode=update_mode,
            update_command=update_command,
            community_api_url=community_api_url,
            community_api_token=community_api_token,
        )
    )
    return TestClient(app), app


def _bootstrap_admin(client: TestClient) -> None:
    response = client.post(
        "/setup/admin",
        data={
            "username": "admin",
            "email": "admin@example.com",
            "password": "BackendFlow!123",
            "password_confirm": "BackendFlow!123",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Provider" in response.text


def _complete_setup(client: TestClient) -> None:
    response = client.post(
        "/setup/provider",
        data={
            "provider": "openrouter",
            "model": "openai/gpt-4.1-mini",
            "api_key": "backend-openrouter-key",
            "api_base": "",
            "extra_headers": "{}",
            "action": "next",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Channel" in response.text

    response = client.post(
        "/setup/channel",
        data={
            "channel": "telegram",
            "token": "123456:ABCDEF",
            "allow_from": "owner-1, owner-2",
            "send_progress": "on",
            "send_tool_hints": "on",
            "action": "next",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Agent" in response.text

    response = client.post(
        "/setup/agent",
        data={
            "model": "openai/gpt-4.1-mini",
            "provider": "openrouter",
            "instruction_content": "# Backend integration instructions\n- Stay deterministic.",
            "response_style": "brief",
            "tools_enabled": "on",
            "restrict_to_workspace": "on",
            "action": "finish",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Dashboard" in response.text


def _install_fixture_mcp_backend(app):
    config_service = app.state.config_service

    def _fixture_name_for_source(source: str) -> str:
        repo = _parse_repository_source(source)
        repo_key = repo["repo"].lower()
        try:
            load_mcp_fixture(repo_key)
            return repo_key
        except FileNotFoundError:
            return "echo-mcp"

    async def fake_check_runtime():
        return {
            "ok": True,
            "provider": "openrouter",
            "model": "openai/gpt-4.1-mini",
            "latency_ms": 12,
            "checked_at": "2026-03-10T00:00:00+00:00",
            "usage": {"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12},
        }

    async def fake_analyze(source: str, **_: object):
        repo = _parse_repository_source(source)
        return build_mcp_fixture_analysis(fixture_name=_fixture_name_for_source(source), repo=repo)

    async def fake_install(source: str, **_: object):
        analysis = await fake_analyze(source)
        fixture = load_mcp_fixture(_fixture_name_for_source(source))
        install_dir = config_service.mcp_installs_dir / analysis["install_slug"]
        install_dir.mkdir(parents=True, exist_ok=True)
        (install_dir / "README.md").write_text(
            f"# {analysis['server_name']}\n\nInstalled from backend integration fixture.\n",
            encoding="utf-8",
        )

        config = config_service.load()
        existing = config.tools.mcp_servers.get(analysis["server_name"])
        env = dict(existing.env) if existing else {}
        config.tools.mcp_servers[analysis["server_name"]] = MCPServerConfig(
            type=analysis["transport"],
            command=analysis["run_command"],
            args=list(analysis["run_args"]),
            env=env,
            url=analysis["run_url"],
            headers={},
            tool_timeout=30,
        )
        config_service.save(config)
        config_service.set_mcp_record(
            analysis["server_name"],
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
                "tool_names": list(analysis["tool_names"]),
                "probe_error": str(fixture.get("probe_error", "")).strip(),
                "repo_type": analysis.get("repo_type", "fixture"),
                "analysis_mode": analysis.get("analysis_mode", "deterministic"),
                "analysis_confidence": analysis.get("analysis_confidence", 0.95),
                "required_runtimes": list(analysis.get("required_runtimes", [])),
                "runtime_status": list(analysis.get("runtime_status", [])),
                "missing_runtimes": list(analysis.get("missing_runtimes", [])),
                "next_action": analysis.get("next_action", ""),
                "enabled": False,
                "last_installed_at": "2026-03-10T00:00:00+00:00",
                "log_tail": "Installed from backend integration fixture.",
            },
        )
        return await fake_test(analysis["server_name"])

    async def fake_test(server_name: str):
        config = config_service.load()
        server = config.tools.mcp_servers[server_name]
        record = config_service.get_mcp_record(server_name)
        missing_env = [
            env_name
            for env_name in record.get("required_env", [])
            if not str(server.env.get(env_name, "")).strip()
        ]
        if missing_env:
            result = {
                **record,
                "status": "needs_configuration",
                "status_label": "Needs configuration",
                "last_test_status": "needs_configuration",
                "last_test_label": "Needs configuration",
                "missing_env": missing_env,
                "last_error": "Missing required environment variables: " + ", ".join(missing_env),
            }
            config_service.set_mcp_record(server_name, result)
            return result

        probe_error = str(record.get("probe_error", "")).strip()
        if probe_error:
            result = {
                **record,
                "status": "error",
                "status_label": "Probe failed",
                "last_test_status": "error",
                "last_test_label": "Probe failed",
                "missing_env": [],
                "last_error": probe_error,
            }
            config_service.set_mcp_record(server_name, result)
            return result

        result = {
            **record,
            "status": "active",
            "status_label": "Active",
            "last_test_status": "active",
            "last_test_label": "Active",
            "missing_env": [],
            "last_error": "",
            "tool_names": list(record.get("tool_names", [])),
        }
        config_service.set_mcp_record(server_name, result)
        return result

    app.state.agent_service.check_runtime = fake_check_runtime
    app.state.mcp_service.analyze_repository = fake_analyze
    app.state.mcp_service.install_repository = fake_install
    app.state.mcp_service.test_server = fake_test


def test_gui_setup_routes_persist_config_and_templates(tmp_path: Path):
    client, app = _make_client(tmp_path)

    _bootstrap_admin(client)
    _complete_setup(client)

    config = app.state.config_service.load()
    assert app.state.config_service.is_setup_complete() is True
    assert config.agents.defaults.provider == "openrouter"
    assert config.agents.defaults.model == "openai/gpt-4.1-mini"
    assert config.providers.openrouter.api_key == "backend-openrouter-key"
    assert config.channels.telegram.enabled is True
    assert config.channels.telegram.allow_from == ["owner-1", "owner-2"]
    assert app.state.config_service.read_markdown_document("agents")["content"].startswith("# Backend integration")
    assert "- [x] Brief and concise" in app.state.config_service.read_markdown_document("user")["content"]


def test_gui_profile_and_settings_routes_persist_backend_state(tmp_path: Path):
    client, app = _make_client(tmp_path)

    _bootstrap_admin(client)
    settings_response = client.post(
        "/settings",
        data={
            "workspace": str((tmp_path / "workspace").resolve()),
            "tools_enabled": "on",
            "restrict_to_workspace": "on",
            "send_progress": "on",
            "send_tool_hints": "on",
            "exec_timeout": "90",
            "path_append": "/usr/local/bin:/custom/bin",
            "dangerous_repair_mode": "on",
        },
        follow_redirects=True,
    )
    assert settings_response.status_code == 200
    assert "Settings saved." in settings_response.text

    profile_response = client.post(
        "/profile",
        data={
            "display_name": "Backend Admin",
            "username": "backend-admin",
            "email": "backend@example.com",
            "password": "BackendFlow!456",
            "password_confirm": "BackendFlow!456",
        },
        files={"avatar": ("avatar.png", PNG_1X1, "image/png")},
        follow_redirects=True,
    )
    assert profile_response.status_code == 200
    assert "Profile updated." in profile_response.text

    config = app.state.config_service.load()
    admin = app.state.auth_service.get_admin(1)
    assert config.tools.enabled is True
    assert config.tools.restrict_to_workspace is True
    assert config.tools.exec.timeout == 90
    assert config.tools.exec.path_append == "/usr/local/bin:/custom/bin"
    assert app.state.config_service.is_unrestricted_agent_shell_enabled() is True
    assert admin is not None
    assert admin.username == "backend-admin"
    assert admin.email == "backend@example.com"
    assert admin.display_name == "Backend Admin"
    assert admin.avatar_path is not None
    assert (app.state.config_service.media_dir / admin.avatar_path).exists()


def test_gui_mcp_routes_with_local_fixtures_persist_registry_and_enable(tmp_path: Path):
    client, app = _make_client(tmp_path)

    _bootstrap_admin(client)
    _complete_setup(client)
    _install_fixture_mcp_backend(app)

    install_response = client.post(
        "/mcp/install",
        data={"source": "https://github.com/example/secret-mcp"},
        follow_redirects=True,
    )
    assert install_response.status_code == 200
    assert "MCP Detail" in install_response.text
    assert "installed, but it still needs configuration" in install_response.text

    config = app.state.config_service.load()
    assert "secret" in config.tools.mcp_servers
    assert app.state.config_service.get_mcp_record("secret")["status"] == "needs_configuration"

    save_response = client.post(
        "/mcp/secret",
        data={
            "transport": "stdio",
            "command": "fixture-secret-mcp",
            "args": "serve",
            "url": "",
            "env_json": "{}",
            "headers_json": "{}",
            "tool_timeout": "30",
            "env__FAKE_API_KEY": "fixture-secret-value",
        },
        follow_redirects=True,
    )
    assert save_response.status_code == 200
    assert "Run the test before enabling it." in save_response.text

    test_response = client.post(
        "/mcp/test/secret",
        data={"next": "/mcp/secret"},
        follow_redirects=True,
    )
    assert test_response.status_code == 200
    assert "is active with" in test_response.text

    toggle_response = client.post(
        "/mcp/toggle/secret",
        data={"next": "/mcp/secret"},
        follow_redirects=True,
    )
    assert toggle_response.status_code == 200
    assert "enabled for the main chat runtime" in toggle_response.text

    record = app.state.config_service.get_mcp_record("secret")
    config = app.state.config_service.load()
    assert config.tools.mcp_servers["secret"].env["FAKE_API_KEY"] == "fixture-secret-value"
    assert record["status"] == "active"
    assert record["enabled"] is True
    assert "fetch_secret" in record["tool_names"]


def test_gui_mcp_analyze_preview_shows_pipeline_metadata(tmp_path: Path):
    client, app = _make_client(tmp_path)

    _bootstrap_admin(client)
    _complete_setup(client)
    _install_fixture_mcp_backend(app)

    response = client.post(
        "/mcp/analyze",
        data={"source": "https://github.com/example/echo-mcp"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "MCP Install Preview" in response.text
    assert "Deterministic install pipeline" in response.text
    assert "Required runtimes" in response.text
    assert "Next step" in response.text


def test_gui_community_detail_and_install_flow_persist_recommendations(tmp_path: Path):
    client, app = _make_client(
        tmp_path,
        community_api_url="http://community-hub.test/api/v1",
        community_api_token="hub-write-token",
    )

    _bootstrap_admin(client)
    _complete_setup(client)
    _install_fixture_mcp_backend(app)
    app.state.config_service.set_community_preferences(
        share_anonymous_metrics=False,
        receive_recommendations=True,
        show_marketplace_stats=True,
        allow_public_mcp_submissions=True,
    )

    install_metrics: list[str] = []

    async def fake_marketplace_detail(slug: str):
        assert slug == "echo-mcp"
        return {
            "slug": "echo-mcp",
            "name": "Echo MCP",
            "repo_url": "https://github.com/example/echo-mcp",
            "description": "Fixture-backed community MCP.",
            "category": "Automation",
            "language": "Python",
            "install_method": "python",
            "verified": True,
            "success_rate": 0.98,
            "active_instances": 42,
            "avg_latency_ms": 1200,
            "tools": ["echo_text"],
            "tool_count": 1,
            "known_issues": ["Requires Python runtime."],
            "recommended_config": {
                "transport": "stdio",
                "timeout": 60,
                "retries": 1,
                "confidence_score": 0.93,
                "based_on_instances": 20,
            },
        }

    async def fake_mark_install(slug: str):
        install_metrics.append(slug)
        return {"ok": True}

    app.state.community_service.marketplace_detail = fake_marketplace_detail  # type: ignore[method-assign]
    app.state.community_service.mark_install = fake_mark_install  # type: ignore[method-assign]

    detail = client.get("/community/mcp/echo-mcp")
    assert detail.status_code == 200
    assert "Recommended Config" in detail.text
    assert "60 seconds" in detail.text
    assert "Install from Community" in detail.text

    install = client.post("/community/install/mcp/echo-mcp", follow_redirects=True)
    assert install.status_code == 200
    assert "MCP Detail" in install.text

    config = app.state.config_service.load()
    record = app.state.config_service.get_mcp_record("echo")
    assert "echo" in config.tools.mcp_servers
    assert record["community_slug"] == "echo-mcp"
    assert record["enabled"] is False
    assert install_metrics == ["echo-mcp"]


def test_gui_community_stack_import_installs_and_enables_active_mcps(tmp_path: Path):
    client, app = _make_client(
        tmp_path,
        community_api_url="http://community-hub.test/api/v1",
        community_api_token="hub-write-token",
    )

    _bootstrap_admin(client)
    _complete_setup(client)
    _install_fixture_mcp_backend(app)
    app.state.config_service.set_community_preferences(
        share_anonymous_metrics=False,
        receive_recommendations=True,
        show_marketplace_stats=True,
        allow_public_mcp_submissions=False,
    )

    install_metrics: list[str] = []
    stack_metrics: list[str] = []

    async def fake_stack_detail(slug: str):
        assert slug == "community-dev-stack"
        return {
            "slug": "community-dev-stack",
            "title": "Community Dev Stack",
            "description": "Install one healthy MCP and one MCP that still needs configuration.",
            "recommended_model": "moonshot/kimi-k2.5",
            "items": [
                {
                    "slug": "echo-mcp",
                    "name": "Echo MCP",
                    "repo_url": "https://github.com/example/echo-mcp",
                },
                {
                    "slug": "secret-mcp",
                    "name": "Secret MCP",
                    "repo_url": "https://github.com/example/secret-mcp",
                },
            ],
        }

    async def fake_mark_install(slug: str):
        install_metrics.append(slug)
        return {"ok": True}

    async def fake_mark_stack_import(slug: str):
        stack_metrics.append(slug)
        return {"ok": True}

    app.state.community_service.stack_detail = fake_stack_detail  # type: ignore[method-assign]
    app.state.community_service.mark_install = fake_mark_install  # type: ignore[method-assign]
    app.state.community_service.mark_stack_import = fake_mark_stack_import  # type: ignore[method-assign]

    response = client.post("/community/import/stack/community-dev-stack", follow_redirects=True)
    assert response.status_code == 200
    assert "MCP" in response.text

    echo_record = app.state.config_service.get_mcp_record("echo")
    secret_record = app.state.config_service.get_mcp_record("secret")
    assert echo_record["community_slug"] == "echo-mcp"
    assert echo_record["enabled"] is True
    assert secret_record["community_slug"] == "secret-mcp"
    assert secret_record["enabled"] is False
    assert install_metrics == ["echo-mcp", "secret-mcp"]
    assert stack_metrics == ["community-dev-stack"]


def test_gui_mcp_repair_route_dispatches_configured_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    client, app = _make_client(
        tmp_path,
        update_check_enabled=False,
        update_mode="disabled",
        update_command=None,
    )
    app.state.settings.repair_mode = "command"
    app.state.settings.repair_command = "nanobot repair-worker"

    _bootstrap_admin(client)
    _complete_setup(client)
    _install_fixture_mcp_backend(app)
    install_response = client.post(
        "/mcp/install",
        data={"source": "https://github.com/example/secret-mcp"},
        follow_redirects=True,
    )
    assert install_response.status_code == 200

    config = app.state.config_service.load()
    config.tools.mcp_servers["secret"].command = "npx"
    app.state.config_service.save(config)
    app.state.config_service.set_mcp_record(
        "secret",
        {
            **app.state.config_service.get_mcp_record("secret"),
            "missing_runtimes": ["node"],
            "required_runtimes": ["node", "npx"],
            "runtime_status": [
                {"name": "node", "available": False, "executable": ""},
                {"name": "npx", "available": False, "executable": ""},
            ],
            "next_action": "Apply a supported repair for the missing runtimes, then run the MCP test again.",
        },
    )

    calls: list[tuple[str, str, str]] = []

    def fake_repair_runner(command, logger, config_service, mcp_service, server_name, repair_plan):
        calls.append((command, server_name, repair_plan["recommended_recipe"]))

    async def fake_build_repair_plan(server_name: str, allow_unrestricted: bool = False):
        assert allow_unrestricted is False
        return {
            "server_name": server_name,
            "missing_runtime": "node",
            "missing_runtimes": ["node"],
            "required_env": [],
            "recommended_recipe": "install_node",
            "available_recipes": ["install_node"],
            "next_step": "Apply a supported repair for the missing runtimes, then run the MCP test again.",
            "confidence": 0.95,
            "shell_command": "",
            "source": "deterministic",
            "supported": True,
        }

    monkeypatch.setattr("nanobot.gui.app._run_mcp_repair_command", fake_repair_runner)
    app.state.mcp_service.build_repair_plan = fake_build_repair_plan  # type: ignore[method-assign]

    response = client.post(
        "/mcp/repair/secret",
        data={"next": "/mcp/secret"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert calls == [("nanobot repair-worker", "secret", "install_node")]
    assert app.state.config_service.get_mcp_record("secret")["repair_status"] == "queued"


def test_gui_update_banner_checks_github_once_per_day_and_renders_actions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    client, app = _make_client(
        tmp_path,
        update_check_enabled=True,
        update_mode="command",
        update_command="/usr/local/bin/nanobot-webgui-update.sh",
    )
    app.state.auth_service.create_admin("admin", "admin@example.com", "BackendFlow!123")
    app.state.config_service.set_setup_complete(True)

    calls: list[str] = []

    def fake_fetch(repo: str) -> dict[str, str]:
        calls.append(repo)
        return {
            "tag_name": "v0.2.1",
            "latest_version": "0.2.1",
            "release_url": "https://github.com/lucmuss/nanobot-webgui/releases/tag/v0.2.1",
            "release_notes_url": "https://github.com/lucmuss/nanobot-webgui/releases/tag/v0.2.1",
            "release_name": "v0.2.1",
            "published_at": "2026-03-10T00:00:00Z",
            "source": "github_release",
        }

    monkeypatch.setattr("nanobot.gui.app._fetch_latest_release_info", fake_fetch)

    login_response = client.post(
        "/login",
        data={"identifier": "admin", "password": "BackendFlow!123"},
        follow_redirects=True,
    )
    assert login_response.status_code == 200
    assert "New version available: v0.2.1" in login_response.text
    assert "View release notes" in login_response.text
    assert "Update now" in login_response.text
    assert calls == ["lucmuss/nanobot-webgui"]

    dashboard_response = client.get("/dashboard")
    assert dashboard_response.status_code == 200
    assert "New version available: v0.2.1" in dashboard_response.text
    assert calls == ["lucmuss/nanobot-webgui"]

    status = app.state.config_service.get_update_status()
    status["checked_at"] = "2026-03-08T00:00:00+00:00"
    app.state.config_service.set_update_status(status)
    dashboard_response = client.get("/dashboard")
    assert dashboard_response.status_code == 200
    assert calls == ["lucmuss/nanobot-webgui", "lucmuss/nanobot-webgui"]


def test_gui_update_action_runs_only_configured_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    client, app = _make_client(
        tmp_path,
        update_check_enabled=True,
        update_mode="command",
        update_command="/usr/local/bin/nanobot-webgui-update.sh",
    )
    app.state.auth_service.create_admin("admin", "admin@example.com", "BackendFlow!123")
    app.state.config_service.set_setup_complete(True)
    app.state.config_service.set_update_status(
        {
            "enabled": True,
            "current_version": "0.2.0",
            "latest_version": "0.2.1",
            "tag_name": "v0.2.1",
            "available": True,
            "checked_at": "2026-03-10T00:00:00+00:00",
            "release_url": "https://github.com/lucmuss/nanobot-webgui/releases/tag/v0.2.1",
            "release_notes_url": "https://github.com/lucmuss/nanobot-webgui/releases/tag/v0.2.1",
            "release_name": "v0.2.1",
            "published_at": "2026-03-10T00:00:00Z",
            "source": "github_release",
            "repo": "lucmuss/nanobot-webgui",
            "error": "",
            "updating": False,
            "last_update_request_at": "",
            "last_update_error": "",
        }
    )

    calls: list[tuple[str, str]] = []

    def fake_run(command: str, logger, config_service, target_version: str) -> None:
        calls.append((command, target_version))

    monkeypatch.setattr("nanobot.gui.app._run_update_command", fake_run)
    monkeypatch.setattr(
        "nanobot.gui.app._fetch_latest_release_info",
        lambda _repo: {
            "tag_name": "v0.2.1",
            "latest_version": "0.2.1",
            "release_url": "https://github.com/lucmuss/nanobot-webgui/releases/tag/v0.2.1",
            "release_notes_url": "https://github.com/lucmuss/nanobot-webgui/releases/tag/v0.2.1",
            "release_name": "v0.2.1",
            "published_at": "2026-03-10T00:00:00Z",
            "source": "github_release",
        },
    )

    login_response = client.post(
        "/login",
        data={"identifier": "admin", "password": "BackendFlow!123"},
        follow_redirects=True,
    )
    assert login_response.status_code == 200

    update_response = client.post("/actions/update")
    assert update_response.status_code == 202
    assert "Updating GUI" in update_response.text
    assert calls == [("/usr/local/bin/nanobot-webgui-update.sh", "0.2.1")]
    assert app.state.config_service.get_update_status()["updating"] is True
