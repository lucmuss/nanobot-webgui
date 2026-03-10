import logging
import shutil
from pathlib import Path

import pytest

from nanobot.config.schema import MCPServerConfig
from nanobot.gui.config_service import GUIConfigService
from nanobot.gui.mcp_service import GUIMCPService, _extract_readme_summary
from tests.helpers.mcp_fixtures import FIXTURE_ROOT


def _build_service(tmp_path: Path) -> GUIMCPService:
    config_path = tmp_path / "runtime" / "config.json"
    workspace_path = tmp_path / "workspace"
    config_service = GUIConfigService(config_path, str(workspace_path))
    return GUIMCPService(config_service, logging.getLogger("test.gui.mcp"))


def test_inspect_checkout_prefers_server_manifest_npm_package(tmp_path: Path):
    checkout_dir = FIXTURE_ROOT / "manifest-npm"

    service = _build_service(tmp_path)
    analysis = service._inspect_checkout(
        checkout_dir,
        {
            "owner": "firecrawl",
            "repo": "firecrawl-mcp-server",
            "repo_url": "https://github.com/firecrawl/firecrawl-mcp-server",
            "clone_url": "https://github.com/firecrawl/firecrawl-mcp-server.git",
        },
    )

    assert analysis["install_mode"] == "npm"
    assert analysis["run_command"] == "npx"
    assert analysis["run_args"] == ["-y", "firecrawl-mcp"]
    assert analysis["transport"] == "stdio"
    assert analysis["optional_env"] == ["FIRECRAWL_API_KEY"]


def test_inspect_checkout_falls_back_to_workspace_mcp_package(tmp_path: Path):
    checkout_dir = FIXTURE_ROOT / "workspace-playwright"

    service = _build_service(tmp_path)
    analysis = service._inspect_checkout(
        checkout_dir,
        {
            "owner": "microsoft",
            "repo": "playwright-mcp",
            "repo_url": "https://github.com/microsoft/playwright-mcp",
            "clone_url": "https://github.com/microsoft/playwright-mcp.git",
        },
    )

    assert analysis["install_mode"] == "workspace_package"
    assert analysis["run_command"] == "npx"
    assert analysis["run_args"] == ["-y", "@playwright/mcp"]
    assert any("workspace package name=@playwright/mcp" in item for item in analysis["evidence"])


def test_inspect_checkout_prefers_remote_manifest_over_oci(tmp_path: Path):
    checkout_dir = FIXTURE_ROOT / "remote-github"

    service = _build_service(tmp_path)
    analysis = service._inspect_checkout(
        checkout_dir,
        {
            "owner": "github",
            "repo": "github-mcp-server",
            "repo_url": "https://github.com/github/github-mcp-server",
            "clone_url": "https://github.com/github/github-mcp-server.git",
        },
    )

    assert analysis["install_mode"] == "remote"
    assert analysis["transport"] == "streamableHttp"
    assert analysis["run_url"] == "https://api.githubcopilot.com/mcp/"
    assert analysis["run_command"] == ""


def test_enrich_analysis_adds_repo_type_runtime_checks_and_next_step(tmp_path: Path):
    checkout_dir = FIXTURE_ROOT / "workspace-playwright"
    service = _build_service(tmp_path)

    analysis = service._inspect_checkout(
        checkout_dir,
        {
            "owner": "microsoft",
            "repo": "playwright-mcp",
            "repo_url": "https://github.com/microsoft/playwright-mcp",
            "clone_url": "https://github.com/microsoft/playwright-mcp.git",
        },
    )
    enriched = service._enrich_analysis(analysis)

    assert enriched["repo_type"] == "monorepo"
    assert enriched["analysis_mode"] == "deterministic"
    assert "node" in enriched["required_runtimes"]
    assert "npx" in enriched["required_runtimes"]
    assert isinstance(enriched["runtime_status"], list)
    assert "enable it for chat" in enriched["next_action"].lower()


@pytest.mark.asyncio
async def test_analyze_repository_uses_ai_fallback_for_unknown_repo(tmp_path: Path):
    repo_dir = tmp_path / "unknown-repo"
    repo_dir.mkdir(parents=True)
    (repo_dir / "README.md").write_text("Custom MCP server with unclear layout.", encoding="utf-8")

    service = _build_service(tmp_path)

    async def fake_clone_repository(_clone_url: str, target_dir: Path | None = None) -> Path:
        assert target_dir is None
        return repo_dir

    async def fake_ai_plan_builder(bundle: dict[str, object]) -> dict[str, object]:
        assert bundle["repo"]["repo"] == "mystery-mcp"
        return {
            "repo_type": "python",
            "install_mode": "source",
            "transport": "stdio",
            "runtime": ["python", "pip"],
            "run_command": "python3",
            "run_args": ["server.py"],
            "run_url": "",
            "install_steps": [{"display": "python3 -m pip install -e .", "command": ["python3", "-m", "pip", "install", "-e", "."], "timeout": 900}],
            "required_env": ["OPENAI_API_KEY"],
            "optional_env": [],
            "server_name": "mystery-mcp",
            "summary": "AI fallback plan for a custom MCP repo.",
            "evidence": ["README mentions MCP server"],
            "confidence": 0.61,
        }

    service._clone_repository = fake_clone_repository  # type: ignore[method-assign]
    service.ai_plan_builder = fake_ai_plan_builder

    analysis = await service.analyze_repository(
        "https://github.com/example/mystery-mcp",
        allow_ai_fallback=True,
    )

    assert analysis["analysis_mode"] == "ai_fallback"
    assert analysis["run_command"] == "python3"
    assert analysis["required_env"] == ["OPENAI_API_KEY"]
    assert analysis["repo_type"] == "python"
    assert "analysis:ai_fallback" in analysis["evidence"]


@pytest.mark.asyncio
async def test_install_repository_blocks_when_required_runtime_is_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    checkout_dir = FIXTURE_ROOT / "manifest-npm"
    service = _build_service(tmp_path)

    async def fake_clone_repository(_clone_url: str, target_dir: Path | None = None) -> Path:
        assert target_dir is None
        target = tmp_path / "cloned-manifest-npm"
        shutil.copytree(checkout_dir, target)
        return target

    monkeypatch.setattr(service, "_clone_repository", fake_clone_repository)
    monkeypatch.setattr("nanobot.gui.mcp_service.shutil.which", lambda _name: None)

    with pytest.raises(ValueError, match="Missing required runtime tools"):
        await service.install_repository(
            "https://github.com/firecrawl/firecrawl-mcp-server",
            allow_ai_fallback=False,
        )


def test_extract_readme_summary_skips_html_image_blocks(tmp_path: Path):
    readme = tmp_path / "README.md"
    readme.write_text(
        "# Example MCP\n\n"
        '<img src="assets/logo.png" alt="Logo" width="256" height="256">\n\n'
        "An MCP server for generating images with a clean summary.\n",
        encoding="utf-8",
    )

    summary = _extract_readme_summary(readme)

    assert summary == "An MCP server for generating images with a clean summary."


@pytest.mark.asyncio
async def test_build_repair_plan_prefers_supported_runtime_recipe(tmp_path: Path):
    service = _build_service(tmp_path)
    config = service.config_service.ensure_instance()
    config.tools.mcp_servers["repairable"] = MCPServerConfig(
        type="stdio",
        command="npx",
        args=["-y", "example-mcp"],
        env={},
        url="",
        headers={},
        tool_timeout=30,
    )
    service.config_service.save(config)
    service.config_service.set_mcp_record(
        "repairable",
        {
            "required_runtimes": ["node", "npx"],
            "runtime_status": [
                {"name": "node", "available": False, "executable": ""},
                {"name": "npx", "available": False, "executable": ""},
            ],
            "missing_runtimes": ["node", "npx"],
            "required_env": [],
            "next_action": "Apply a supported repair for the missing runtimes, then run the MCP test again.",
        },
    )
    service.refresh_runtime_requirements = lambda _server_name: service.config_service.get_mcp_record("repairable")  # type: ignore[method-assign]

    plan = await service.build_repair_plan("repairable")

    assert plan["supported"] is True
    assert plan["recommended_recipe"] == "install_node"
    assert "install_node" in plan["available_recipes"]


@pytest.mark.asyncio
async def test_build_repair_plan_can_use_ai_unrestricted_fallback(tmp_path: Path):
    service = _build_service(tmp_path)
    config = service.config_service.ensure_instance()
    config.tools.mcp_servers["mystery"] = MCPServerConfig(
        type="stdio",
        command="custom-launcher",
        args=[],
        env={},
        url="",
        headers={},
        tool_timeout=30,
    )
    service.config_service.save(config)
    service.config_service.set_mcp_record(
        "mystery",
        {
            "required_runtimes": [],
            "runtime_status": [],
            "missing_runtimes": [],
            "required_env": [],
            "last_error": "Custom launcher dependency is missing from the runtime.",
        },
    )

    async def fake_ai_repair_planner(bundle: dict[str, object]) -> dict[str, object]:
        assert bundle["allow_unrestricted_agent_shell"] is True
        return {
            "missing_runtime": "custom-launcher",
            "recommended_recipe": "unrestricted_agent_shell",
            "required_env": [],
            "next_step": "Run the shell repair, then retest the MCP.",
            "confidence": 0.62,
            "shell_command": "apt-get update && apt-get install -y custom-launcher",
        }

    service.ai_repair_planner = fake_ai_repair_planner

    plan = await service.build_repair_plan("mystery", allow_unrestricted=True)

    assert plan["supported"] is True
    assert plan["recommended_recipe"] == "unrestricted_agent_shell"
    assert "apt-get update" in plan["shell_command"]
