import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nanobot.config.schema import MCPServerConfig
from nanobot.gui.app import GUISettings, _render_chat_message_html, create_gui_app
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


def test_login_accepts_email_identifier(tmp_path: Path):
    client, _app = _make_client(tmp_path)

    _bootstrap_admin(client)

    response = client.post(
        "/login",
        data={"identifier": "admin@example.com", "password": "BackendFlow!123"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/setup/provider"


def test_dashboard_exposes_operational_shortcuts_after_setup(tmp_path: Path):
    client, _app = _make_client(tmp_path)

    _bootstrap_admin(client)
    _complete_setup(client)

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert "Manage MCP Servers" in response.text
    assert "Configure Channels" in response.text
    assert "Change Provider" in response.text


def test_dashboard_shows_usage_24h_metric_after_usage_events(tmp_path: Path):
    client, app = _make_client(tmp_path)

    _bootstrap_admin(client)
    _complete_setup(client)
    app.state.config_service.record_usage_event(
        {
            "source": "chat",
            "provider": "openrouter",
            "model": "openai/gpt-4.1-mini",
            "prompt_tokens": 120,
            "completion_tokens": 30,
            "total_tokens": 150,
            "estimated_cost_usd": 0.0,
        }
    )

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert "Usage 24h" in response.text
    assert "150" in response.text


def test_whatsapp_partial_includes_bridge_guidance(tmp_path: Path):
    client, _app = _make_client(tmp_path)

    _bootstrap_admin(client)

    response = client.get("/partials/channel-fields?channel=whatsapp")

    assert response.status_code == 200
    assert "WhatsApp setup" in response.text
    assert "Scan the QR code" in response.text


def test_chat_markdown_renderer_formats_headings_lists_and_code() -> None:
    rendered = _render_chat_message_html(
        "## Functioning and configured\n- Agent configuration\n- API access\n\nUse `nanobot`.",
        role="assistant",
    )

    assert "<h3>Functioning and configured</h3>" in rendered
    assert "<li>Agent configuration</li>" in rendered
    assert "<code>nanobot</code>" in rendered


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


def test_settings_and_topbar_render_new_tooltips(tmp_path: Path):
    client, _app = _make_client(tmp_path)

    _bootstrap_admin(client)

    settings = client.get("/settings")
    assert settings.status_code == 200
    assert "Allows this instance to receive recommended MCP servers and metadata updates from the community hub." in settings.text
    assert "Displays marketplace statistics such as installs, reliability scores, and signals in the dashboard and discovery pages." in settings.text

    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200
    assert "Open the chat interface to interact directly with the agent." in dashboard.text
    assert "Run a deeper diagnostic test of the agent runtime and tool execution pipeline." in dashboard.text


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
            "install_confidence": {
                "score": 9.4,
                "label": "High confidence",
                "tone": "good",
                "based_on_instances": 42,
            },
            "permission_hints": [
                "Runs local runtime processes",
                "Needs secrets or API credentials",
            ],
            "usage_trend": {
                "runs_24h": 8,
                "runs_7d": 21,
                "label": "Growing",
                "tone": "good",
            },
            "known_fixes": [
                {
                    "title": "Increase timeout to the community default",
                    "summary": "Community telemetry links timeout errors to shorter timeouts.",
                    "evidence_count": 4,
                }
            ],
            "error_clusters": [
                {
                    "error_code": "timeout",
                    "event_count": 4,
                    "instance_count": 2,
                    "summary": "Observed timeout failures 4 times across 2 instance fingerprints.",
                }
            ],
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
    assert "Community Fixes" in detail.text
    assert "Error Clusters" in detail.text
    assert "Runs in 24h" in detail.text
    assert "Confidence" in detail.text
    assert "Security and permissions" in detail.text

    install = client.post("/community/install/mcp/echo-mcp", follow_redirects=True)
    assert install.status_code == 200
    assert "MCP Detail" in install.text

    config = app.state.config_service.load()
    record = app.state.config_service.get_mcp_record("echo")
    assert "echo" in config.tools.mcp_servers
    assert record["community_slug"] == "echo-mcp"
    assert record["enabled"] is False
    assert install_metrics == ["echo-mcp"]


def test_gui_community_discover_supports_language_and_reliability_filters(tmp_path: Path):
    client, app = _make_client(
        tmp_path,
        community_api_url="http://community-hub.test/api/v1",
        community_api_token="hub-write-token",
    )

    _bootstrap_admin(client)
    _complete_setup(client)

    captured: dict[str, object] = {}

    async def fake_marketplace(
        *,
        query: str = "",
        category: str = "",
        language: str = "",
        runtime: str = "",
        min_reliability: int = 0,
        sort: str = "trending",
    ):
        captured.update(
            {
                "query": query,
                "category": category,
                "language": language,
                "runtime": runtime,
                "min_reliability": min_reliability,
                "sort": sort,
            }
        )
        return {
            "items": [
                {
                    "slug": "context7",
                    "name": "Context7",
                    "description": "Research context MCP.",
                    "category": "Research",
                    "language": "Remote",
                    "install_method": "remote",
                    "runtime_engine": {"label": "Remote/API", "tone": "community"},
                    "verified": True,
                    "active_instances": 1200,
                    "installs": 9800,
                    "success_rate": 0.97,
                    "avg_latency_ms": 1800,
                    "recent_runs": 44,
                    "tools": ["fetch_doc", "search_context"],
                    "tool_count": 2,
                    "dependencies": ["Remote MCP endpoint"],
                    "permission_hints": ["Makes outbound network requests"],
                    "best_for": ["Research agents"],
                    "difficulty": {"label": "Beginner", "tone": "good"},
                    "reliability": {"label": "Stable", "tone": "good", "percent": 97, "bar_width": 97},
                    "install_confidence": {
                        "score": 9.7,
                        "label": "High confidence",
                        "tone": "good",
                        "based_on_instances": 1200,
                    },
                    "usage_trend": {
                        "runs_24h": 12,
                        "runs_7d": 44,
                        "label": "Growing",
                        "tone": "good",
                    },
                    "known_fixes": [
                        {
                            "title": "Review required environment variables",
                            "summary": "Missing secrets are a common cause of failure.",
                            "evidence_count": 3,
                        }
                    ],
                }
            ],
            "categories": ["Coding", "Research"],
            "languages": ["Node.js", "Remote"],
            "runtime_options": ["Node", "Python", "Docker", "Remote/API"],
            "reliability_options": [0, 80, 90, 95],
        }

    app.state.community_service.marketplace = fake_marketplace  # type: ignore[method-assign]

    response = client.get(
        "/community/discover",
        params={
            "q": "context",
            "category": "Research",
            "language": "Remote",
            "runtime": "Remote/API",
            "min_reliability": 95,
            "sort": "reliable",
        },
    )
    assert response.status_code == 200
    body = response.text
    assert "All languages" in body
    assert "Any runtime" in body
    assert "95% reliability" in body
    assert "Known fix" in body
    assert "Confidence" in body
    assert "Usage trend" in body
    assert "Makes outbound network requests" in body
    assert captured == {
        "query": "context",
        "category": "Research",
        "language": "Remote",
        "runtime": "Remote/API",
        "min_reliability": 95,
        "sort": "reliable",
    }


def test_gui_community_stats_page_renders_network_health_insights(tmp_path: Path):
    client, app = _make_client(
        tmp_path,
        community_api_url="http://community-hub.test/api/v1",
        community_api_token="hub-write-token",
    )

    _bootstrap_admin(client)
    _complete_setup(client)

    async def fake_overview():
        return {
            "registry_count": 6,
            "verified_count": 5,
            "active_instances": 3200,
            "runs_today": 540,
            "top_category": "Research",
            "telemetry_active": True,
            "average_success_rate": 0.945,
            "average_latency_ms": 1840.0,
            "top_categories": [{"name": "Research", "count": 3}, {"name": "Coding", "count": 2}],
            "trending_mcps": [{"slug": "context7", "name": "Context7", "recent_runs": 44, "active_instances": 1200}],
            "most_reliable_mcps": [
                {
                    "slug": "context7",
                    "name": "Context7",
                    "avg_latency_ms": 1800,
                    "reliability": {"label": "Stable", "tone": "good", "percent": 97},
                }
            ],
            "top_mcps": [{"slug": "context7", "name": "Context7", "active_instances": 1200, "installs": 9800, "category": "Research"}],
        }

    app.state.community_service.overview = fake_overview  # type: ignore[method-assign]

    response = client.get("/community/stats")
    assert response.status_code == 200
    assert "Avg Success" in response.text
    assert "Top Categories" in response.text


def test_gui_memory_page_shows_filenames_and_extended_markdown_help(tmp_path: Path):
    client, _app = _make_client(tmp_path)

    _bootstrap_admin(client)
    _complete_setup(client)

    response = client.get("/memory?doc=heartbeat")
    assert response.status_code == 200
    assert "/ HEARTBEAT.md" in response.text
    assert "heartbeat steps or recurring routines." in response.text
    assert "Store API keys in config or environment variables" in response.text


def test_gui_community_discover_renders_tool_preview_and_compact_install_meta(tmp_path: Path):
    client, app = _make_client(
        tmp_path,
        community_api_url="http://community-hub.test/api/v1",
        community_api_token="hub-write-token",
    )

    _bootstrap_admin(client)
    _complete_setup(client)

    async def fake_marketplace(
        *,
        query: str = "",
        category: str = "",
        language: str = "",
        runtime: str = "",
        min_reliability: int = 0,
        sort: str = "trending",
    ):
        return {
            "items": [
                {
                    "slug": "context7",
                    "name": "Context7",
                    "description": "Research context MCP.",
                    "category": "Research",
                    "language": "Remote",
                    "install_method": "remote",
                    "runtime_engine": {"label": "Remote/API", "tone": "community"},
                    "verified": True,
                    "active_instances": 1200,
                    "installs": 9800,
                    "success_rate": 0.97,
                    "avg_latency_ms": 1800,
                    "recent_runs": 44,
                    "tools": ["fetch_doc", "search_context"],
                    "tool_count": 2,
                    "dependencies": ["Remote MCP endpoint"],
                    "permission_hints": ["Makes outbound network requests"],
                    "best_for": ["Research agents"],
                    "difficulty": {"label": "Beginner", "tone": "good"},
                    "reliability": {"label": "Stable", "tone": "good", "percent": 97, "bar_width": 97},
                    "install_confidence": {
                        "score": 9.7,
                        "label": "High confidence",
                        "tone": "good",
                        "based_on_instances": 1200,
                    },
                    "usage_trend": {
                        "runs_24h": 12,
                        "runs_7d": 44,
                        "label": "Growing",
                        "tone": "good",
                    },
                    "known_fixes": [],
                }
            ],
            "categories": ["Coding", "Research"],
            "languages": ["Node.js", "Remote"],
            "runtime_options": ["Node", "Python", "Docker", "Remote/API"],
            "reliability_options": [0, 80, 90, 95],
        }

    app.state.community_service.marketplace = fake_marketplace  # type: ignore[method-assign]

    response = client.get("/community/discover")
    assert response.status_code == 200
    assert "Tool preview (2 total)" in response.text
    assert "The hub currently tracks tool names." in response.text
    assert "Dependencies" in response.text
    assert "Tools" in response.text
    assert "Security and permissions" in response.text
    assert "Remote/API" in response.text


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


def test_gui_community_apply_fix_updates_local_mcp_config(tmp_path: Path):
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

    install = client.post(
        "/mcp/install",
        data={"source": "https://github.com/example/echo-mcp"},
        follow_redirects=True,
    )
    assert install.status_code == 200

    config = app.state.config_service.load()
    config.tools.mcp_servers["echo"].type = "sse"
    config.tools.mcp_servers["echo"].tool_timeout = 15
    app.state.config_service.save(config)
    app.state.config_service.set_mcp_record(
        "echo",
        {
            **app.state.config_service.get_mcp_record("echo"),
            "community_slug": "echo-mcp",
            "last_error": "Timeout while probing the MCP server.",
            "status": "error",
            "status_label": "Probe failed",
            "transport": "sse",
            "tool_timeout": 15,
            "missing_runtimes": [],
        },
    )

    async def fake_marketplace_detail(slug: str):
        assert slug == "echo-mcp"
        return {
            "slug": "echo-mcp",
            "name": "Echo MCP",
            "repo_url": "https://github.com/example/echo-mcp",
            "description": "Fixture-backed community MCP.",
            "recommended_config": {
                "transport": "stdio",
                "timeout": 60,
                "retries": 1,
                "confidence_score": 0.93,
                "based_on_instances": 20,
            },
            "reliability": {"label": "Stable", "tone": "good", "percent": 98, "bar_width": 98},
            "best_for": ["Automation agents"],
            "dependencies": ["Python 3.11+"],
            "permission_hints": ["Runs local runtime processes"],
            "known_issues": ["Increase timeout on slower systems."],
            "active_instances": 42,
        }

    async def fake_marketplace_fixes(*_args, **_kwargs):
        return {
            "slug": "echo-mcp",
            "fixes": [
                {
                    "id": "apply-recommended-config",
                    "title": "Apply the community-recommended config",
                    "summary": "Switch transport to stdio, then increase timeout to 60s.",
                    "action_type": "apply_recommended_config",
                    "config_changes": {"transport": "stdio", "tool_timeout": 60},
                    "recommended_config": {"transport": "stdio", "timeout": 60, "retries": 1},
                    "confidence_score": 0.93,
                    "based_on_instances": 20,
                }
            ],
        }

    app.state.community_service.marketplace_detail = fake_marketplace_detail  # type: ignore[method-assign]
    app.state.community_service.marketplace_fixes = fake_marketplace_fixes  # type: ignore[method-assign]

    detail = client.get("/mcp/echo")
    assert detail.status_code == 200
    assert "Apply Recommended Config" in detail.text
    assert "Security and permissions" in detail.text

    response = client.post(
        "/mcp/apply-community-fix/echo",
        data={"fix_id": "apply-recommended-config", "next": "/mcp/echo"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Run the MCP test again now" in response.text

    updated_config = app.state.config_service.load()
    updated_record = app.state.config_service.get_mcp_record("echo")
    assert updated_config.tools.mcp_servers["echo"].type == "stdio"
    assert updated_config.tools.mcp_servers["echo"].tool_timeout == 60
    assert updated_record["status"] == "registered"
    assert updated_record["enabled"] is False


def test_gui_chat_error_links_to_local_mcp_detail_for_community_backed_servers(tmp_path: Path):
    client, app = _make_client(
        tmp_path,
        community_api_url="http://community-hub.test/api/v1",
        community_api_token="hub-write-token",
    )

    _bootstrap_admin(client)
    _complete_setup(client)
    _install_fixture_mcp_backend(app)

    install = client.post(
        "/mcp/install",
        data={"source": "https://github.com/example/echo-mcp"},
        follow_redirects=True,
    )
    assert install.status_code == 200

    config = app.state.config_service.load()
    config.tools.mcp_servers["echo"].type = "stdio"
    app.state.config_service.save(config)
    app.state.config_service.set_mcp_record(
        "echo",
        {
            **app.state.config_service.get_mcp_record("echo"),
            "enabled": True,
            "status": "error",
            "status_label": "Probe failed",
            "community_slug": "echo-mcp",
            "last_error": "Timeout while probing the MCP server.",
        },
    )
    app.state.config_service.set_last_error(
        {
            "title": "Chat runtime failed",
            "raw": "Timeout while probing the MCP server.",
            "explanation": "The active MCP failed while the chat runtime was using it.",
            "next_action": "Review the MCP detail page and compare the community-backed recommendation.",
            "action_label": "Open MCP registry",
            "action_url": "/mcp",
        }
    )

    response = client.get("/chat")
    assert response.status_code == 200
    assert "Why did this fail?" in response.text
    assert "/mcp/echo" in response.text


def test_gui_community_submit_stack_and_showcase_routes(tmp_path: Path):
    client, app = _make_client(
        tmp_path,
        community_api_url="http://community-hub.test/api/v1",
        community_api_token="hub-write-token",
    )

    _bootstrap_admin(client)
    _complete_setup(client)
    app.state.config_service.set_community_preferences(
        share_anonymous_metrics=False,
        receive_recommendations=True,
        show_marketplace_stats=True,
        allow_public_mcp_submissions=True,
    )

    submitted_stack_payloads: list[dict[str, object]] = []
    submitted_showcase_payloads: list[dict[str, object]] = []

    async def fake_submit_stack(payload: dict[str, object]):
        submitted_stack_payloads.append(payload)
        return {
            "created": True,
            "item": {
                "slug": "docs-stack",
                "title": "Docs Stack",
                "description": "Research docs with browser support.",
                "recommended_model": "moonshot/kimi-k2.5",
                "use_case": "Inspect docs and summarize stable guidance.",
                "example_prompt": "Summarize the docs.",
                "imports_count": 0,
                "rating": 0.0,
                "items": [
                    {"slug": "context7", "name": "Context7", "repo_url": "https://github.com/upstash/context7"},
                ],
                "difficulty": {"label": "Beginner", "tone": "good"},
            },
        }

    async def fake_stack_detail(slug: str):
        assert slug == "docs-stack"
        return {
            "slug": "docs-stack",
            "title": "Docs Stack",
            "description": "Research docs with browser support.",
            "recommended_model": "moonshot/kimi-k2.5",
            "use_case": "Inspect docs and summarize stable guidance.",
            "example_prompt": "Summarize the docs.",
            "imports_count": 0,
            "rating": 0.0,
            "items": [
                {"slug": "context7", "name": "Context7", "repo_url": "https://github.com/upstash/context7"},
            ],
            "difficulty": {"label": "Beginner", "tone": "good"},
        }

    async def fake_submit_showcase(payload: dict[str, object]):
        submitted_showcase_payloads.append(payload)
        return {
            "created": True,
            "item": {
                "slug": "docs-assistant",
                "title": "Docs Assistant",
            },
        }

    async def fake_stacks(query: str = ""):
        return {"items": [{"slug": "docs-stack", "title": "Docs Stack"}]}

    async def fake_showcase(query: str = "", category: str = ""):
        return {
            "items": [
                {
                    "slug": "docs-assistant",
                    "title": "Docs Assistant",
                    "description": "Practical docs research setup.",
                    "category": category or "Research",
                    "use_case": "Inspect docs and summarize stable guidance.",
                    "example_prompt": "Summarize the docs.",
                    "stack": {"slug": "docs-stack", "title": "Docs Stack", "recommended_model": "moonshot/kimi-k2.5"},
                    "stack_items": [],
                    "imports_count": 0,
                }
            ]
        }

    app.state.community_service.submit_stack = fake_submit_stack  # type: ignore[method-assign]
    app.state.community_service.stack_detail = fake_stack_detail  # type: ignore[method-assign]
    app.state.community_service.submit_showcase = fake_submit_showcase  # type: ignore[method-assign]
    app.state.community_service.stacks = fake_stacks  # type: ignore[method-assign]
    app.state.community_service.showcase = fake_showcase  # type: ignore[method-assign]

    stack_response = client.post(
        "/community/submit/stack",
        data={
            "title": "Docs Stack",
            "description": "Research docs with browser support.",
            "use_case": "Inspect docs and summarize stable guidance.",
            "recommended_model": "moonshot/kimi-k2.5",
            "example_prompt": "Summarize the docs.",
            "items": "context7",
        },
        follow_redirects=True,
    )
    assert stack_response.status_code == 200
    assert "Docs Stack" in stack_response.text
    assert submitted_stack_payloads[0]["title"] == "Docs Stack"

    showcase_response = client.post(
        "/community/submit/showcase",
        data={
            "title": "Docs Assistant",
            "description": "Practical docs research setup.",
            "use_case": "Inspect docs and summarize stable guidance.",
            "category": "Research",
            "example_prompt": "Summarize the docs.",
            "stack_slug": "docs-stack",
        },
        follow_redirects=True,
    )
    assert showcase_response.status_code == 200
    assert "Saved showcase" in showcase_response.text
    assert submitted_showcase_payloads[0]["stack_slug"] == "docs-stack"


def test_gui_community_showcase_uses_category_select_and_removes_try_prompt(tmp_path: Path):
    client, app = _make_client(
        tmp_path,
        community_api_url="http://community-hub.test/api/v1",
        community_api_token="hub-write-token",
    )

    _bootstrap_admin(client)
    _complete_setup(client)
    app.state.config_service.set_community_preferences(
        share_anonymous_metrics=False,
        receive_recommendations=True,
        show_marketplace_stats=True,
        allow_public_mcp_submissions=True,
    )

    async def fake_stacks(query: str = ""):
        return {"items": [{"slug": "docs-stack", "title": "Docs Stack"}]}

    async def fake_showcase(query: str = "", category: str = ""):
        return {
            "items": [
                {
                    "slug": "docs-assistant",
                    "title": "Docs Assistant",
                    "description": "Find answers in **docs**.",
                    "category": "Knowledge workflows",
                    "use_case": "Gather sources quickly for documentation questions.",
                    "example_prompt": "Find docs sources.",
                    "stack": {"slug": "docs-stack", "title": "Docs Stack", "recommended_model": "moonshot/kimi-k2.5"},
                    "stack_items": [],
                    "imports_count": 0,
                    "demo_ready": True,
                }
            ],
            "categories": ["Knowledge workflows", "Research"],
        }

    app.state.community_service.stacks = fake_stacks  # type: ignore[method-assign]
    app.state.community_service.showcase = fake_showcase  # type: ignore[method-assign]

    response = client.get("/community/showcase")
    assert response.status_code == 200
    assert '<select name="category"' in response.text
    assert "Knowledge workflows" in response.text
    assert "Supports basic Markdown" in response.text
    assert "Try Prompt in Chat" not in response.text


def test_gui_community_vote_routes_proxy_to_hub(tmp_path: Path):
    client, app = _make_client(
        tmp_path,
        community_api_url="http://community-hub.test/api/v1",
        community_api_token="hub-write-token",
    )

    _bootstrap_admin(client)
    _complete_setup(client)

    recorded: list[tuple[str, str]] = []

    async def fake_vote_mcp(slug: str, *, vote_type: str, voter_key: str):
        recorded.append((slug, vote_type))
        assert voter_key
        return {"ok": True}

    async def fake_vote_stack(slug: str, *, vote_type: str, voter_key: str):
        recorded.append((slug, vote_type))
        assert voter_key
        return {"ok": True}

    app.state.community_service.vote_mcp = fake_vote_mcp  # type: ignore[method-assign]
    app.state.community_service.vote_stack = fake_vote_stack  # type: ignore[method-assign]

    mcp_response = client.post("/community/vote/mcp/context7", data={"vote_type": "up"}, follow_redirects=False)
    assert mcp_response.status_code == 303
    assert mcp_response.headers["location"] == "/community/mcp/context7"

    stack_response = client.post(
        "/community/vote/stack/github-developer-stack",
        data={"vote_type": "down"},
        follow_redirects=False,
    )
    assert stack_response.status_code == 303
    assert stack_response.headers["location"] == "/community/stacks/github-developer-stack"
    assert recorded == [("context7", "up"), ("github-developer-stack", "down")]


def test_gui_community_showcase_import_marks_metric_and_redirects_to_stack_import(tmp_path: Path):
    client, app = _make_client(
        tmp_path,
        community_api_url="http://community-hub.test/api/v1",
        community_api_token="hub-write-token",
    )

    _bootstrap_admin(client)
    _complete_setup(client)
    app.state.config_service.set_community_preferences(
        share_anonymous_metrics=False,
        receive_recommendations=True,
        show_marketplace_stats=True,
        allow_public_mcp_submissions=False,
    )

    marked_showcases: list[str] = []
    imported_stacks: list[str] = []

    async def fake_showcase_detail(slug: str):
        assert slug == "docs-assistant"
        return {
            "slug": "docs-assistant",
            "title": "Docs Assistant",
            "stack": {"slug": "docs-stack", "title": "Docs Stack"},
        }

    async def fake_mark_showcase_import(slug: str):
        marked_showcases.append(slug)
        return {"ok": True}

    async def fake_stack_detail(slug: str):
        imported_stacks.append(slug)
        return {
            "slug": "docs-stack",
            "title": "Docs Stack",
            "recommended_model": "moonshot/kimi-k2.5",
            "items": [],
        }

    app.state.community_service.showcase_detail = fake_showcase_detail  # type: ignore[method-assign]
    app.state.community_service.mark_showcase_import = fake_mark_showcase_import  # type: ignore[method-assign]
    app.state.community_service.stack_detail = fake_stack_detail  # type: ignore[method-assign]
    app.state.agent_service.check_runtime = lambda: {  # type: ignore[assignment]
        "ok": True,
        "provider": "openrouter",
        "model": "openai/gpt-4.1-mini",
        "latency_ms": 12,
        "checked_at": "2026-03-10T00:00:00+00:00",
        "usage": {"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12},
    }

    response = client.post("/community/import/showcase/docs-assistant", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/community/import/stack/docs-stack"
    assert marked_showcases == ["docs-assistant"]
    assert imported_stacks == []


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
            "tag_name": "v0.3.2",
            "latest_version": "0.3.2",
            "release_url": "https://github.com/lucmuss/nanobot-webgui/releases/tag/v0.3.2",
            "release_notes_url": "https://github.com/lucmuss/nanobot-webgui/releases/tag/v0.3.2",
            "release_name": "v0.3.2",
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
    assert "New version available: v0.3.2" in login_response.text
    assert "View release notes" in login_response.text
    assert "Update now" in login_response.text
    assert calls == ["lucmuss/nanobot-webgui"]

    dashboard_response = client.get("/dashboard")
    assert dashboard_response.status_code == 200
    assert "New version available: v0.3.2" in dashboard_response.text
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
            "current_version": "0.3.1",
            "latest_version": "0.3.2",
            "tag_name": "v0.3.2",
            "available": True,
            "checked_at": "2026-03-10T00:00:00+00:00",
            "release_url": "https://github.com/lucmuss/nanobot-webgui/releases/tag/v0.3.2",
            "release_notes_url": "https://github.com/lucmuss/nanobot-webgui/releases/tag/v0.3.2",
            "release_name": "v0.3.2",
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
            "tag_name": "v0.3.2",
            "latest_version": "0.3.2",
            "release_url": "https://github.com/lucmuss/nanobot-webgui/releases/tag/v0.3.2",
            "release_notes_url": "https://github.com/lucmuss/nanobot-webgui/releases/tag/v0.3.2",
            "release_name": "v0.3.2",
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
    assert calls == [("/usr/local/bin/nanobot-webgui-update.sh", "0.3.2")]
    assert app.state.config_service.get_update_status()["updating"] is True
