import asyncio
import json
import logging
import shutil
from pathlib import Path

import pytest

import nanobot_webgui.mcp_service as mcp_service_module
from nanobot.config.schema import MCPServerConfig
from nanobot_webgui.config_service import GUIConfigService
from nanobot_webgui.mcp_service import GUIMCPService, _extract_readme_summary, _guess_env_defaults, _parse_repository_source
from tests.helpers.mcp_fixtures import FIXTURE_ROOT


def _build_service(tmp_path: Path) -> GUIMCPService:
    config_path = tmp_path / "runtime" / "config.json"
    workspace_path = tmp_path / "workspace"
    config_service = GUIConfigService(config_path, str(workspace_path))
    return GUIMCPService(config_service, logging.getLogger("test.gui.mcp"))


class _FakeStream:
    def __init__(self, payload: bytes = b"") -> None:
        self._payload = payload

    async def read(self) -> bytes:
        return self._payload


class _FakeProcess:
    def __init__(self, *, returncode: int | None = None, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self.stderr = _FakeStream(stderr)
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        return 0 if self.returncode is None else self.returncode


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


def test_inspect_checkout_detects_server_package_outside_packages_dir(tmp_path: Path):
    checkout_dir = tmp_path / "monorepo"
    (checkout_dir / "servers" / "filesystem").mkdir(parents=True)
    (checkout_dir / "README.md").write_text("Monorepo MCP server.", encoding="utf-8")
    (checkout_dir / "package.json").write_text(
        json.dumps({"name": "monorepo-root", "private": True}),
        encoding="utf-8",
    )
    (checkout_dir / "servers" / "filesystem" / "package.json").write_text(
        json.dumps(
            {
                "name": "@modelcontextprotocol/server-filesystem",
                "version": "0.1.0",
                "bin": {"mcp-filesystem": "dist/index.js"},
                "mcpName": "filesystem",
            }
        ),
        encoding="utf-8",
    )

    service = _build_service(tmp_path)
    analysis = service._inspect_checkout(
        checkout_dir,
        {
            "owner": "modelcontextprotocol",
            "repo": "servers",
            "repo_url": "https://github.com/modelcontextprotocol/servers",
            "clone_url": "https://github.com/modelcontextprotocol/servers.git",
        },
    )

    assert analysis["install_mode"] == "workspace_package"
    assert analysis["run_command"] == "npx"
    assert analysis["run_args"] == ["-y", "@modelcontextprotocol/server-filesystem"]
    assert any("workspace package path=servers/filesystem/package.json" in item for item in analysis["evidence"])


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
    assert enriched["next_action"]


def test_enrich_analysis_detects_node_engine_version_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    checkout_dir = tmp_path / "email-mcp"
    checkout_dir.mkdir(parents=True)
    (checkout_dir / "README.md").write_text("Email MCP server.", encoding="utf-8")
    (checkout_dir / "package.json").write_text(
        json.dumps(
            {
                "name": "@codefuturist/email-mcp",
                "version": "0.2.1",
                "bin": {"email-mcp": "./dist/main.js"},
                "engines": {"node": ">=99.0.0"},
            }
        ),
        encoding="utf-8",
    )

    service = _build_service(tmp_path)
    analysis = service._inspect_checkout(
        checkout_dir,
        {
            "owner": "codefuturist",
            "repo": "email-mcp",
            "repo_url": "https://github.com/codefuturist/email-mcp",
            "clone_url": "https://github.com/codefuturist/email-mcp.git",
        },
    )

    monkeypatch.setattr(
        mcp_service_module.shutil,
        "which",
        lambda name: {"node": "/usr/bin/node", "npx": "/usr/bin/npx", "npm": "/usr/bin/npm"}.get(name, ""),
    )

    class _Completed:
        stdout = "v20.20.1\n"
        stderr = ""

    monkeypatch.setattr(mcp_service_module.subprocess, "run", lambda *args, **kwargs: _Completed())

    enriched = service._enrich_analysis(analysis)

    node_status = next(item for item in enriched["runtime_status"] if item["name"] == "node")
    assert node_status["available"] is False
    assert node_status["reason"] == "version_mismatch"
    assert node_status["provisionable"] is True
    assert node_status["required_version"] == ">=99.0.0"
    assert enriched["missing_runtimes"] == []
    assert enriched["can_install"] is True
    assert "will provision a matching local Node runtime" in enriched["next_action"]


def test_inspect_checkout_uses_pyproject_console_script_for_python_mcp(tmp_path: Path):
    checkout_dir = tmp_path / "caldav-mcp"
    (checkout_dir / "src" / "mcp_caldav").mkdir(parents=True)
    (checkout_dir / "README.md").write_text(
        "Python MCP server for CalDAV integrations.",
        encoding="utf-8",
    )
    (checkout_dir / "pyproject.toml").write_text(
        """
[project]
name = "mcp-caldav"
version = "0.1.0"
description = "CalDAV MCP"

[project.scripts]
mcp-caldav = "mcp_caldav:main"
""".strip(),
        encoding="utf-8",
    )
    (checkout_dir / "src" / "mcp_caldav" / "__init__.py").write_text(
        "def main():\n    raise SystemExit(0)\n",
        encoding="utf-8",
    )

    service = _build_service(tmp_path)
    analysis = service._inspect_checkout(
        checkout_dir,
        {
            "owner": "madbonez",
            "repo": "caldav-mcp",
            "repo_url": "https://github.com/madbonez/caldav-mcp",
            "clone_url": "https://github.com/madbonez/caldav-mcp.git",
        },
    )

    assert analysis["install_mode"] == "source"
    assert analysis["run_command"] == "uv"
    assert analysis["run_args"] == ["run", "--directory", "./", "mcp-caldav"]
    assert any(step["display"] == "uv sync" for step in analysis["install_steps"])
    assert "pyproject.toml" in analysis["evidence"]


def test_inspect_checkout_uses_uv_run_for_python_entry_files(tmp_path: Path):
    checkout_dir = tmp_path / "python-mcp"
    (checkout_dir / "src").mkdir(parents=True)
    (checkout_dir / "pyproject.toml").write_text(
        """
[project]
name = "calendar-mcp"
version = "0.1.0"
""".strip(),
        encoding="utf-8",
    )
    (checkout_dir / "src" / "main.py").write_text("print('hello')\n", encoding="utf-8")

    service = _build_service(tmp_path)
    analysis = service._inspect_checkout(
        checkout_dir,
        {
            "owner": "example",
            "repo": "calendar-mcp",
            "repo_url": "https://github.com/example/calendar-mcp",
            "clone_url": "https://github.com/example/calendar-mcp.git",
        },
    )

    assert analysis["run_command"] == "uv"
    assert analysis["run_args"] == ["run", "--directory", "./", "python", "./src/main.py"]
    assert any(step["display"] == "uv sync" for step in analysis["install_steps"])


def test_inspect_checkout_uses_django_stdio_manage_command_for_pyproject_repo(tmp_path: Path):
    checkout_dir = tmp_path / "django-mcp-server"
    (checkout_dir / "examples" / "mcpexample" / "mcpexample").mkdir(parents=True)
    (checkout_dir / "mcp_server" / "management" / "commands").mkdir(parents=True)
    (checkout_dir / "README.md").write_text(
        "Django MCP server.",
        encoding="utf-8",
    )
    (checkout_dir / "pyproject.toml").write_text(
        """
[project]
name = "django-mcp-server"
version = "0.1.0"
dependencies = ["django>=4.0", "mcp>=1.8.0"]

[tool.poetry.group.dev.dependencies]
rest-framework-csv = "^3.0.2"
""".strip(),
        encoding="utf-8",
    )
    (checkout_dir / "mcp_server" / "management" / "commands" / "stdio_server.py").write_text(
        "print('placeholder')\n",
        encoding="utf-8",
    )
    (checkout_dir / "examples" / "mcpexample" / "manage.py").write_text(
        "print('manage')\n",
        encoding="utf-8",
    )

    service = _build_service(tmp_path)
    analysis = service._inspect_checkout(
        checkout_dir,
        {
            "owner": "gts360",
            "repo": "django-mcp-server",
            "repo_url": "https://github.com/gts360/django-mcp-server",
            "clone_url": "https://github.com/gts360/django-mcp-server.git",
        },
    )

    assert analysis["install_mode"] == "source"
    assert analysis["run_command"] == "uv"
    assert analysis["run_args"] == [
        "run",
        "--directory",
        "./",
        "python",
        "./examples/mcpexample/manage.py",
        "stdio_server",
    ]
    assert any(step["display"] == "uv sync" for step in analysis["install_steps"])
    assert any(
        step["display"] == (
            "uv pip install --python .venv/bin/python "
            "rest-framework-csv"
        )
        or step["display"].startswith(
            "uv pip install --python .venv/bin/python "
        )
        for step in analysis["install_steps"]
    )
    assert "pyproject.toml" in analysis["evidence"]


def test_inspect_checkout_uses_requirements_python_fallback_for_flat_repo(tmp_path: Path):
    checkout_dir = tmp_path / "curl-mcp"
    checkout_dir.mkdir(parents=True)
    (checkout_dir / "README.md").write_text(
        "Natural language curl MCP server.",
        encoding="utf-8",
    )
    (checkout_dir / "requirements.txt").write_text(
        "httpx>=0.28.1\nmcp[cli]>=1.6.0\nrich>=10.0.0\n",
        encoding="utf-8",
    )
    (checkout_dir / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (checkout_dir / "main.py").write_text(
        "from mcp.server.fastmcp import FastMCP\nmcp = FastMCP('curl-mcp')\n"
        "if __name__ == '__main__':\n    mcp.run(transport='stdio')\n",
        encoding="utf-8",
    )

    service = _build_service(tmp_path)
    analysis = service._inspect_checkout(
        checkout_dir,
        {
            "owner": "MartinPSDev",
            "repo": "curl-mcp",
            "repo_url": "https://github.com/MartinPSDev/curl-mcp",
            "clone_url": "https://github.com/MartinPSDev/curl-mcp.git",
        },
    )

    assert analysis["install_mode"] == "source"
    assert analysis["repo_type"] == "python"
    assert analysis["analysis_confidence"] >= 0.55
    assert analysis["run_command"] == ".venv/bin/python"
    assert analysis["run_args"] == ["./main.py"]
    assert any(step["display"] == "uv venv .venv" for step in analysis["install_steps"])
    assert any(
        step["display"] == "uv pip install --python .venv/bin/python -r requirements.txt"
        for step in analysis["install_steps"]
    )
    assert "requirements.txt" in analysis["evidence"]
    assert "uv.lock" in analysis["evidence"]


def test_inspect_checkout_ignores_placeholder_example_runtime_for_built_node_repo(tmp_path: Path):
    checkout_dir = tmp_path / "dalle-mcp"
    checkout_dir.mkdir(parents=True)
    (checkout_dir / "README.md").write_text(
        "DALL-E MCP server.",
        encoding="utf-8",
    )
    (checkout_dir / "package.json").write_text(
        json.dumps(
            {
                "name": "dalle-mcp-server",
                "version": "0.1.0",
                "bin": {"dalle-mcp-server": "./build/index.js"},
                "scripts": {
                    "build": "tsc",
                    "start": "node build/index.js",
                },
            }
        ),
        encoding="utf-8",
    )
    (checkout_dir / "mcp-settings-example.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "dalle": {
                        "command": "node",
                        "args": ["/path/to/dalle-mcp-server/build/index.js"],
                        "env": {"OPENAI_API_KEY": "sk-example"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    service = _build_service(tmp_path)
    analysis = service._inspect_checkout(
        checkout_dir,
        {
            "owner": "Garoth",
            "repo": "dalle-mcp",
            "repo_url": "https://github.com/Garoth/dalle-mcp",
            "clone_url": "https://github.com/Garoth/dalle-mcp.git",
        },
    )

    assert analysis["run_command"] == "node"
    assert analysis["run_args"] == ["./build/index.js"]
    assert any(step["display"] == "npm install" for step in analysis["install_steps"])
    assert any(step["display"] == "npm run build" for step in analysis["install_steps"])
    assert "OPENAI_API_KEY" in analysis["required_env"]
    assert any("Example MCP config: mcp-settings-example.json" == item for item in analysis["evidence"])
    assert any("placeholder values" in item for item in analysis["evidence"])


def test_build_server_config_expands_relative_venv_command(tmp_path: Path):
    service = _build_service(tmp_path)
    config = service.config_service.ensure_instance()
    install_dir = tmp_path / "workspace" / "mcp-installs" / "martinpsdev__curl-mcp"

    cfg = service._build_server_config(
        {
            "server_name": "curl-mcp",
            "transport": "stdio",
            "run_command": ".venv/bin/python",
            "run_args": ["./main.py"],
            "run_url": "",
            "required_env": [],
            "optional_env": [],
        },
        install_dir,
        existing=None,
        config=config,
    )

    assert cfg.command == str(install_dir / ".venv/bin/python")
    assert cfg.args == [str(install_dir / "main.py")]


def test_build_server_config_expands_relative_node_command(tmp_path: Path):
    service = _build_service(tmp_path)
    config = service.config_service.ensure_instance()
    install_dir = tmp_path / "workspace" / "mcp-installs" / "garoth__dalle-mcp"

    cfg = service._build_server_config(
        {
            "server_name": "dalle",
            "transport": "stdio",
            "run_command": "node",
            "run_args": ["./build/index.js"],
            "run_url": "",
            "required_env": ["OPENAI_API_KEY"],
            "optional_env": ["SAVE_DIR"],
        },
        install_dir,
        existing=None,
        config=config,
    )

    assert cfg.command == "node"
    assert cfg.args == [str(install_dir / "build/index.js")]


def test_build_server_config_uses_bound_node_runtime_for_npx_packages(tmp_path: Path):
    service = _build_service(tmp_path)
    config = service.config_service.ensure_instance()

    cfg = service._build_server_config(
        {
            "server_name": "email-mcp",
            "transport": "stdio",
            "run_command": "npx",
            "run_args": ["-y", "@codefuturist/email-mcp"],
            "run_url": "",
            "required_env": [],
            "optional_env": [],
        },
        install_dir=None,
        existing=None,
        config=config,
        runtime_bindings={
            "node": {
                "node_executable": "/workspace/mcp-runtimes/node/bin/node",
                "npx_cli_path": "/workspace/mcp-runtimes/node/lib/node_modules/npm/bin/npx-cli.js",
            }
        },
    )

    assert cfg.command == "/workspace/mcp-runtimes/node/bin/node"
    assert cfg.args == [
        "/workspace/mcp-runtimes/node/lib/node_modules/npm/bin/npx-cli.js",
        "-y",
        "@codefuturist/email-mcp",
    ]
    assert cfg.env["PATH"].split(":")[0] == "/workspace/mcp-runtimes/node/bin"


def test_resolve_install_step_command_uses_bound_node_runtime_for_npm(tmp_path: Path):
    service = _build_service(tmp_path)

    command = service._resolve_install_step_command(
        ["npm", "ci"],
        {
            "node": {
                "node_executable": "/workspace/mcp-runtimes/node/bin/node",
                "npm_cli_path": "/workspace/mcp-runtimes/node/lib/node_modules/npm/bin/npm-cli.js",
            }
        },
    )

    assert command == [
        "/workspace/mcp-runtimes/node/bin/node",
        "/workspace/mcp-runtimes/node/lib/node_modules/npm/bin/npm-cli.js",
        "ci",
    ]


def test_guess_env_defaults_prefills_workspace_path_like_env_names(tmp_path: Path):
    service = _build_service(tmp_path)
    config = service.config_service.ensure_instance()
    workspace = tmp_path / "workspace"

    defaults = _guess_env_defaults(
        config=config,
        server_name="elevenlabs",
        required_env=["ELEVENLABS_MCP_BASE_PATH"],
        optional_env=["SAVE_DIR", "CACHE_DIR"],
        workspace=workspace,
    )

    expected = str(workspace / "mcp-output" / "elevenlabs")
    assert defaults["ELEVENLABS_MCP_BASE_PATH"] == expected
    assert defaults["SAVE_DIR"] == expected
    assert defaults["CACHE_DIR"] == expected
    assert (workspace / "mcp-output" / "elevenlabs").exists()


def test_guess_env_defaults_skips_generic_and_non_filesystem_path_names(tmp_path: Path):
    service = _build_service(tmp_path)
    config = service.config_service.ensure_instance()

    defaults = _guess_env_defaults(
        config=config,
        server_name="example",
        required_env=["PATH", "PYTHONPATH", "MCP_ENDPOINT_PATH"],
        optional_env=["NODE_PATH"],
        workspace=tmp_path / "workspace",
    )

    assert "PATH" not in defaults
    assert "PYTHONPATH" not in defaults
    assert "NODE_PATH" not in defaults
    assert "MCP_ENDPOINT_PATH" not in defaults


@pytest.mark.asyncio
async def test_preflight_server_keeps_stdio_stdin_open_for_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service = _build_service(tmp_path)
    process = _FakeProcess(returncode=None, stderr=b"[INFO] waiting on stdio")
    cfg = MCPServerConfig(
        type="stdio",
        command="node",
        args=["build/index.js"],
        env={"OPENAI_API_KEY": "dummy-test-key"},
        url="",
        headers={},
        tool_timeout=30,
    )

    async def fake_create_subprocess_exec(*args, **kwargs):
        assert args == ("node", "build/index.js")
        assert kwargs["stdin"] is asyncio.subprocess.PIPE
        assert kwargs["stdout"] is asyncio.subprocess.DEVNULL
        assert kwargs["stderr"] is asyncio.subprocess.PIPE
        return process

    async def fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("nanobot_webgui.mcp_service.asyncio.create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr("nanobot_webgui.mcp_service.asyncio.sleep", fake_sleep)

    result = await service._preflight_server(cfg)

    assert result == ""
    assert process.terminated is True
    assert process.killed is False


@pytest.mark.asyncio
async def test_test_server_replaces_generic_connection_closed_with_stdio_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    service = _build_service(tmp_path)
    config = service.config_service.ensure_instance()
    config.tools.mcp_servers["email-mcp"] = MCPServerConfig(
        type="stdio",
        command="node",
        args=["npx-cli.js", "-y", "@codefuturist/email-mcp"],
        env={},
        url="",
        headers={},
        tool_timeout=30,
    )
    service.config_service.save(config)
    service.config_service.set_mcp_record("email-mcp", {"required_env": []})

    async def fake_preflight(_cfg, *, settle_seconds: float = 2.0):
        if settle_seconds > 2:
            return "Fatal error: No configuration found."
        return ""

    async def fake_list_tools(_cfg):
        raise RuntimeError("Connection closed")

    monkeypatch.setattr(service, "_preflight_server", fake_preflight)
    monkeypatch.setattr(service, "_list_server_tools", fake_list_tools)

    result = await service.test_server("email-mcp")

    assert result["status"] == "error"
    assert result["last_error"] == "Fatal error: No configuration found."
    assert result["last_test_checks"][2]["detail"] == "Fatal error: No configuration found."


def test_inspect_checkout_collects_env_requirements_from_source_and_readme_fallback(tmp_path: Path):
    checkout_dir = tmp_path / "email-mcp"
    (checkout_dir / "src").mkdir(parents=True)
    (checkout_dir / "dist").mkdir(parents=True)
    (checkout_dir / "README.md").write_text(
        """
# Email MCP

## Environment Variables

| Name | Required |
| --- | --- |
| `README_ONLY_TOKEN` | optional |
""".strip(),
        encoding="utf-8",
    )
    (checkout_dir / "package.json").write_text(
        json.dumps(
            {
                "name": "@codefuturist/email-mcp",
                "version": "0.2.1",
                "bin": {"email-mcp": "./dist/main.js"},
            }
        ),
        encoding="utf-8",
    )
    (checkout_dir / "dist" / "main.js").write_text("console.log('start')\n", encoding="utf-8")
    (checkout_dir / "src" / "config.ts").write_text(
        """
const emailAddress = process.env.MCP_EMAIL_ADDRESS;
const timeout = process.env.MCP_TIMEOUT ?? "10";
""".strip(),
        encoding="utf-8",
    )

    service = _build_service(tmp_path)
    analysis = service._inspect_checkout(
        checkout_dir,
        {
            "owner": "codefuturist",
            "repo": "email-mcp",
            "repo_url": "https://github.com/codefuturist/email-mcp",
            "clone_url": "https://github.com/codefuturist/email-mcp.git",
        },
    )

    assert "MCP_EMAIL_ADDRESS" in analysis["required_env"]
    assert "MCP_TIMEOUT" in analysis["optional_env"]
    assert any(
        item["name"] == "README_ONLY_TOKEN" and item["confidence"] == "low"
        for item in analysis["env_requirements"]
    )


@pytest.mark.asyncio
async def test_test_server_refreshes_env_requirements_from_install_dir_before_probe(tmp_path: Path):
    install_dir = tmp_path / "workspace" / "mcp-installs" / "codefuturist__email-mcp"
    (install_dir / "src" / "config").mkdir(parents=True)
    (install_dir / "src" / "config" / "loader.ts").write_text(
        """
const emailAddress = process.env.MCP_EMAIL_ADDRESS;
const emailPassword = process.env.MCP_EMAIL_PASSWORD;
""".strip(),
        encoding="utf-8",
    )

    service = _build_service(tmp_path)
    config = service.config_service.ensure_instance()
    config.tools.mcp_servers["email-mcp"] = MCPServerConfig(
        type="stdio",
        command="node",
        args=["dist/main.js"],
        env={},
        url="",
        headers={},
        tool_timeout=30,
    )
    service.config_service.save(config)
    service.config_service.set_mcp_record(
        "email-mcp",
        {
            "install_dir": str(install_dir),
            "required_env": [],
            "optional_env": [],
        },
    )

    result = await service.test_server("email-mcp")

    assert result["status"] == "needs_configuration"
    assert result["missing_env"] == ["MCP_EMAIL_ADDRESS", "MCP_EMAIL_PASSWORD"]
    assert any(
        item["name"] == "MCP_EMAIL_ADDRESS" and "source_scan:src/config/loader.ts" in item["sources"]
        for item in result["env_requirements"]
    )


@pytest.mark.asyncio
async def test_test_server_promotes_runtime_error_envs_into_configuration_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    service = _build_service(tmp_path)
    config = service.config_service.ensure_instance()
    config.tools.mcp_servers["email-mcp"] = MCPServerConfig(
        type="stdio",
        command="node",
        args=["dist/main.js"],
        env={},
        url="",
        headers={},
        tool_timeout=30,
    )
    service.config_service.save(config)
    service.config_service.set_mcp_record("email-mcp", {"required_env": [], "optional_env": []})

    async def fake_preflight(_cfg, *, settle_seconds: float = 2.0):
        assert settle_seconds == 2.0
        return (
            "Fatal error: No configuration found.\n"
            "Set environment variables (MCP_EMAIL_ADDRESS, MCP_EMAIL_PASSWORD, etc.)"
        )

    monkeypatch.setattr(service, "_preflight_server", fake_preflight)

    result = await service.test_server("email-mcp")

    assert result["status"] == "needs_configuration"
    assert result["missing_env"] == ["MCP_EMAIL_ADDRESS", "MCP_EMAIL_PASSWORD"]
    assert result["required_env"] == ["MCP_EMAIL_ADDRESS", "MCP_EMAIL_PASSWORD"]
    assert any(item["name"] == "MCP_EMAIL_PASSWORD" and "runtime_error" in item["sources"] for item in result["env_requirements"])


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
    monkeypatch.setattr("nanobot_webgui.mcp_service.shutil.which", lambda _name: None)

    with pytest.raises(ValueError, match="Missing required runtime tools"):
        await service.install_repository(
            "https://github.com/firecrawl/firecrawl-mcp-server",
            allow_ai_fallback=False,
        )


@pytest.mark.asyncio
async def test_install_repository_uses_local_node_runtime_for_engine_constrained_npm_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    service = _build_service(tmp_path)

    async def fake_analyze(_source: str, *, allow_ai_fallback: bool = False) -> dict[str, object]:
        assert allow_ai_fallback is False
        return {
            "server_name": "email-mcp",
            "title": "codefuturist/email-mcp",
            "summary": "Email MCP package.",
            "repo_url": "https://github.com/codefuturist/email-mcp",
            "clone_url": "https://github.com/codefuturist/email-mcp.git",
            "install_slug": "codefuturist__email-mcp",
            "install_mode": "npm",
            "transport": "stdio",
            "run_command": "npx",
            "run_args": ["-y", "@codefuturist/email-mcp"],
            "run_url": "",
            "install_steps": [],
            "required_env": [],
            "optional_env": [],
            "healthcheck": "list tools",
            "evidence": [],
            "repo_type": "npm",
            "analysis_mode": "deterministic",
            "analysis_confidence": 0.95,
            "required_runtimes": ["node", "npx"],
            "runtime_constraints": {"node": ">=24.0.0"},
            "runtime_status": [
                {
                    "name": "node",
                    "available": False,
                    "executable": "node",
                    "version": "20.20.1",
                    "required_version": ">=24.0.0",
                    "reason": "version_mismatch",
                    "provisionable": True,
                },
                {
                    "name": "npx",
                    "available": True,
                    "executable": "npx",
                    "version": "10.8.2",
                    "required_version": ">=24.0.0",
                    "reason": "",
                    "provisionable": True,
                },
            ],
            "missing_runtimes": [],
            "can_install": True,
            "next_action": "Install the MCP and Nanobot will provision a matching local Node runtime for this server (>=24.0.0).",
        }

    async def fake_prepare(_analysis: dict[str, object], _record: dict[str, object]) -> dict[str, object]:
        return {
            "node": {
                "constraint": ">=24.0.0",
                "resolved_version": "24.10.0",
                "root_dir": "/workspace/mcp-runtimes/node-v24.10.0-linux-x64",
                "node_executable": "/workspace/mcp-runtimes/node-v24.10.0-linux-x64/bin/node",
                "npm_cli_path": "/workspace/mcp-runtimes/node-v24.10.0-linux-x64/lib/node_modules/npm/bin/npm-cli.js",
                "npx_cli_path": "/workspace/mcp-runtimes/node-v24.10.0-linux-x64/lib/node_modules/npm/bin/npx-cli.js",
            }
        }

    async def fake_test(server_name: str) -> dict[str, object]:
        record = service.config_service.get_mcp_record(server_name)
        return {
            **record,
            "server_name": server_name,
            "status": "active",
            "status_label": "Active",
            "last_test_status": "active",
            "last_test_label": "Active",
            "tool_names": ["send_email"],
            "last_test_checks": [
                {"label": "Connection established", "ok": True, "detail": "Fixture package responded."}
            ],
            "enabled": False,
        }

    class _Completed:
        stdout = "v24.10.0\n"
        stderr = ""

    monkeypatch.setattr(service, "analyze_repository", fake_analyze)
    monkeypatch.setattr(service, "_prepare_runtime_bindings", fake_prepare)
    monkeypatch.setattr(service, "test_server", fake_test)
    monkeypatch.setattr(mcp_service_module.subprocess, "run", lambda *args, **kwargs: _Completed())

    record = await service.install_repository("https://github.com/codefuturist/email-mcp")

    cfg = service.config_service.load().tools.mcp_servers["email-mcp"]
    assert cfg.command == "/workspace/mcp-runtimes/node-v24.10.0-linux-x64/bin/node"
    assert cfg.args == [
        "/workspace/mcp-runtimes/node-v24.10.0-linux-x64/lib/node_modules/npm/bin/npx-cli.js",
        "-y",
        "@codefuturist/email-mcp",
    ]
    assert record["runtime_bindings"]["node"]["resolved_version"] == "24.10.0"
    assert record["missing_runtimes"] == []


@pytest.mark.asyncio
async def test_install_repository_falls_back_to_source_checkout_after_npm_module_resolution_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    service = _build_service(tmp_path)
    source_repo = tmp_path / "email-mcp-source"
    (source_repo / "dist").mkdir(parents=True)
    (source_repo / "README.md").write_text("Email MCP source checkout.", encoding="utf-8")
    (source_repo / "package.json").write_text(
        json.dumps(
            {
                "name": "@codefuturist/email-mcp",
                "version": "0.2.1",
                "bin": {"email-mcp": "./dist/main.js"},
                "scripts": {"build": "tsc"},
            }
        ),
        encoding="utf-8",
    )
    (source_repo / "dist" / "main.js").write_text("console.log('email-mcp');\n", encoding="utf-8")

    async def fake_analyze(_source: str, *, allow_ai_fallback: bool = False) -> dict[str, object]:
        assert allow_ai_fallback is False
        return {
            "server_name": "email-mcp",
            "title": "codefuturist/email-mcp",
            "summary": "Email MCP package.",
            "repo_url": "https://github.com/codefuturist/email-mcp",
            "clone_url": "https://github.com/codefuturist/email-mcp.git",
            "install_slug": "codefuturist__email-mcp",
            "install_mode": "npm",
            "transport": "stdio",
            "run_command": "npx",
            "run_args": ["-y", "@codefuturist/email-mcp"],
            "run_url": "",
            "install_steps": [
                {
                    "command": [],
                    "display": "Register npm package runtime via npx @codefuturist/email-mcp",
                    "timeout": 0,
                }
            ],
            "required_env": [],
            "optional_env": [],
            "healthcheck": "list tools",
            "evidence": ["server.json npm=@codefuturist/email-mcp"],
            "repo_type": "server_json",
            "analysis_mode": "deterministic",
            "analysis_confidence": 0.75,
            "required_runtimes": ["node", "npx"],
            "runtime_constraints": {"node": ">=24.0.0"},
            "runtime_status": [
                {
                    "name": "node",
                    "available": False,
                    "executable": "node",
                    "version": "20.20.1",
                    "required_version": ">=24.0.0",
                    "reason": "version_mismatch",
                    "provisionable": True,
                }
            ],
            "missing_runtimes": [],
            "can_install": True,
            "next_action": "Install the MCP and Nanobot will provision a matching local Node runtime for this server (>=24.0.0).",
        }

    async def fake_prepare(_analysis: dict[str, object], _record: dict[str, object]) -> dict[str, object]:
        return {
            "node": {
                "constraint": ">=24.0.0",
                "resolved_version": "24.10.0",
                "root_dir": "/workspace/mcp-runtimes/node-v24.10.0-linux-x64",
                "node_executable": "/workspace/mcp-runtimes/node-v24.10.0-linux-x64/bin/node",
                "npm_cli_path": "/workspace/mcp-runtimes/node-v24.10.0-linux-x64/lib/node_modules/npm/bin/npm-cli.js",
                "npx_cli_path": "/workspace/mcp-runtimes/node-v24.10.0-linux-x64/lib/node_modules/npm/bin/npx-cli.js",
            }
        }

    async def fake_clone(_clone_url: str, target_dir: Path | None = None) -> Path:
        assert target_dir is not None
        shutil.copytree(source_repo, target_dir)
        (target_dir / ".git").mkdir()
        return target_dir

    executed_commands: list[list[str]] = []

    async def fake_run_command(command: list[str], *, cwd: Path, timeout: int, env: dict[str, str] | None = None):
        executed_commands.append(list(command))
        return "", ""

    test_calls: list[tuple[str, list[str]]] = []

    async def fake_test(server_name: str) -> dict[str, object]:
        cfg = service.config_service.load().tools.mcp_servers[server_name]
        test_calls.append((cfg.command, list(cfg.args)))
        if len(test_calls) == 1:
            return {
                "server_name": server_name,
                "status": "error",
                "status_label": "Probe failed",
                "last_test_status": "error",
                "last_test_label": "Probe failed",
                "last_error": (
                    "Error: Cannot find package '/tmp/node_modules/zod-to-json-schema/index.js' "
                    "code: ERR_MODULE_NOT_FOUND"
                ),
                "tool_names": [],
                "last_test_checks": [],
                "enabled": False,
            }
        return {
            "server_name": server_name,
            "status": "active",
            "status_label": "Active",
            "last_test_status": "active",
            "last_test_label": "Active",
            "last_error": "",
            "tool_names": ["send_email"],
            "last_test_checks": [],
            "enabled": False,
        }

    class _Completed:
        stdout = "v24.10.0\n"
        stderr = ""

    monkeypatch.setattr(service, "analyze_repository", fake_analyze)
    monkeypatch.setattr(service, "_prepare_runtime_bindings", fake_prepare)
    monkeypatch.setattr(service, "_clone_repository", fake_clone)
    monkeypatch.setattr(service, "_run_command", fake_run_command)
    monkeypatch.setattr(service, "test_server", fake_test)
    monkeypatch.setattr(mcp_service_module.subprocess, "run", lambda *args, **kwargs: _Completed())

    record = await service.install_repository("https://github.com/codefuturist/email-mcp")

    install_dir = tmp_path / "workspace" / "mcp-installs" / "codefuturist__email-mcp"
    cfg = service.config_service.load().tools.mcp_servers["email-mcp"]
    assert record["status"] == "active"
    assert record["install_dir"] == str(install_dir)
    assert cfg.command == "/workspace/mcp-runtimes/node-v24.10.0-linux-x64/bin/node"
    assert cfg.args == [str(install_dir / "dist" / "main.js")]
    assert any(
        command == [
            "/workspace/mcp-runtimes/node-v24.10.0-linux-x64/bin/node",
            "/workspace/mcp-runtimes/node-v24.10.0-linux-x64/lib/node_modules/npm/bin/npm-cli.js",
            "install",
        ]
        for command in executed_commands
    )
    assert any(
        command == [
            "/workspace/mcp-runtimes/node-v24.10.0-linux-x64/bin/node",
            "/workspace/mcp-runtimes/node-v24.10.0-linux-x64/lib/node_modules/npm/bin/npm-cli.js",
            "run",
            "build",
        ]
        for command in executed_commands
    )
    assert any("fallback:source_checkout_after_npm_runtime_error" == item for item in record["evidence"])
    assert "Automatic source-checkout fallback was applied" in record["log_tail"]


def test_refresh_runtime_requirements_uses_bound_node_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service = _build_service(tmp_path)
    service.config_service.set_mcp_record(
        "email-mcp",
        {
            "required_runtimes": ["node", "npx"],
            "runtime_constraints": {"node": ">=24.0.0"},
            "runtime_bindings": {
                "node": {
                    "constraint": ">=24.0.0",
                    "resolved_version": "24.10.0",
                    "node_executable": "/workspace/mcp-runtimes/node-v24.10.0-linux-x64/bin/node",
                    "npm_cli_path": "/workspace/mcp-runtimes/node-v24.10.0-linux-x64/lib/node_modules/npm/bin/npm-cli.js",
                    "npx_cli_path": "/workspace/mcp-runtimes/node-v24.10.0-linux-x64/lib/node_modules/npm/bin/npx-cli.js",
                }
            },
        },
    )

    class _Completed:
        stdout = "v24.10.0\n"
        stderr = ""

    monkeypatch.setattr(mcp_service_module.subprocess, "run", lambda *args, **kwargs: _Completed())

    refreshed = service.refresh_runtime_requirements("email-mcp")

    assert refreshed["missing_runtimes"] == []
    assert all(item["available"] is True for item in refreshed["runtime_status"])


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


def test_extract_readme_summary_decodes_html_entities(tmp_path: Path):
    readme = tmp_path / "README.md"
    readme.write_text(
        "# Example MCP\n\n"
        "Context &amp; docs helper for &lt;purple&gt; themed workflows.\n",
        encoding="utf-8",
    )

    summary = _extract_readme_summary(readme)

    assert summary == "Context & docs helper for themed workflows."


def test_parse_repository_source_accepts_generic_clone_urls():
    repo = _parse_repository_source("https://gitlab.com/example/team-mcp.git")

    assert repo["owner"] == "example"
    assert repo["repo"] == "team-mcp"
    assert repo["clone_url"] == "https://gitlab.com/example/team-mcp.git"
    assert repo["repo_url"] == "https://gitlab.com/example/team-mcp"


def test_parse_repository_source_rejects_non_github_http_repository_pages():
    with pytest.raises(ValueError, match="Only direct GitHub repository URLs are supported right now."):
        _parse_repository_source("https://example.com/not-github")


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
async def test_install_repository_rejects_duplicate_repo_urls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service = _build_service(tmp_path)
    config = service.config_service.ensure_instance()
    config.tools.mcp_servers["existing"] = MCPServerConfig(
        type="streamableHttp",
        command="",
        args=[],
        env={},
        url="https://example.com/mcp",
        headers={},
        tool_timeout=30,
    )
    service.config_service.save(config)
    service.config_service.set_mcp_record(
        "existing",
        {
            "server_name": "existing",
            "repo_url": "https://github.com/example/duplicate-mcp",
            "enabled": True,
            "status": "active",
            "status_label": "Active",
        },
    )

    async def fake_analyze(_source: str, *, allow_ai_fallback: bool = False) -> dict[str, object]:
        assert allow_ai_fallback is False
        return {
            "server_name": "duplicate",
            "title": "example/duplicate-mcp",
            "summary": "Duplicate repo fixture.",
            "repo_url": "https://github.com/example/duplicate-mcp",
            "clone_url": "https://github.com/example/duplicate-mcp.git",
            "install_slug": "example__duplicate-mcp",
            "install_mode": "remote",
            "transport": "streamableHttp",
            "run_command": "",
            "run_args": [],
            "run_url": "https://example.com/mcp",
            "install_steps": [],
            "required_env": [],
            "optional_env": [],
            "healthcheck": "list tools",
            "evidence": [],
            "repo_type": "remote",
            "analysis_mode": "deterministic",
            "analysis_confidence": 0.95,
            "required_runtimes": [],
            "runtime_status": [],
            "missing_runtimes": [],
            "next_action": "Install the MCP, verify the runtime test, then enable it for chat.",
        }

    monkeypatch.setattr(service, "analyze_repository", fake_analyze)

    with pytest.raises(ValueError, match="already installed"):
        await service.install_repository("https://github.com/example/duplicate-mcp")


@pytest.mark.asyncio
async def test_install_repository_rejects_reinstall_for_same_repo_and_server_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    service = _build_service(tmp_path)
    config = service.config_service.ensure_instance()
    config.tools.mcp_servers["echo"] = MCPServerConfig(
        type="streamableHttp",
        command="",
        args=[],
        env={},
        url="https://example.com/mcp",
        headers={},
        tool_timeout=30,
    )
    service.config_service.save(config)
    service.config_service.set_mcp_record(
        "echo",
        {
            "server_name": "echo",
            "repo_url": "https://github.com/example/echo-mcp",
            "enabled": True,
            "status": "active",
            "status_label": "Active",
        },
    )

    async def fake_analyze(_source: str, *, allow_ai_fallback: bool = False) -> dict[str, object]:
        assert allow_ai_fallback is False
        return {
            "server_name": "echo",
            "title": "example/echo-mcp",
            "summary": "Existing repo fixture.",
            "repo_url": "https://github.com/example/echo-mcp",
            "clone_url": "https://github.com/example/echo-mcp.git",
            "install_slug": "example__echo-mcp",
            "install_mode": "remote",
            "transport": "streamableHttp",
            "run_command": "",
            "run_args": [],
            "run_url": "https://example.com/mcp",
            "install_steps": [],
            "required_env": [],
            "optional_env": [],
            "healthcheck": "list tools",
            "evidence": [],
            "repo_type": "remote",
            "analysis_mode": "deterministic",
            "analysis_confidence": 0.95,
            "required_runtimes": [],
            "runtime_status": [],
            "missing_runtimes": [],
            "next_action": "Install the MCP, verify the runtime test, then enable it for chat.",
        }

    monkeypatch.setattr(service, "analyze_repository", fake_analyze)

    with pytest.raises(ValueError, match="already installed as 'echo'"):
        await service.install_repository("https://github.com/example/echo-mcp")


@pytest.mark.asyncio
async def test_install_repository_auto_enables_first_successful_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service = _build_service(tmp_path)

    async def fake_analyze(_source: str, *, allow_ai_fallback: bool = False) -> dict[str, object]:
        assert allow_ai_fallback is False
        return {
            "server_name": "echo",
            "title": "example/echo-mcp",
            "summary": "Remote MCP fixture.",
            "repo_url": "https://github.com/example/echo-mcp",
            "clone_url": "https://github.com/example/echo-mcp.git",
            "install_slug": "example__echo-mcp",
            "install_mode": "remote",
            "transport": "streamableHttp",
            "run_command": "",
            "run_args": [],
            "run_url": "https://example.com/mcp",
            "install_steps": [],
            "required_env": [],
            "optional_env": [],
            "healthcheck": "list tools",
            "evidence": [],
            "repo_type": "remote",
            "analysis_mode": "deterministic",
            "analysis_confidence": 0.95,
            "required_runtimes": [],
            "runtime_status": [],
            "missing_runtimes": [],
            "next_action": "Install the MCP, verify the runtime test, then enable it for chat.",
        }

    async def fake_test(server_name: str) -> dict[str, object]:
        record = service.config_service.get_mcp_record(server_name)
        return {
            **record,
            "server_name": server_name,
            "status": "active",
            "status_label": "Active",
            "last_test_status": "active",
            "last_test_label": "Active",
            "tool_names": ["echo"],
            "last_test_checks": [
                {"label": "Connection established", "ok": True, "detail": "Fixture transport responded."}
            ],
            "enabled": False,
        }

    monkeypatch.setattr(service, "analyze_repository", fake_analyze)
    monkeypatch.setattr(service, "test_server", fake_test)

    record = await service.install_repository("https://github.com/example/echo-mcp")

    assert record["enabled"] is True
    assert record["auto_enabled"] is True
    assert service.config_service.is_mcp_enabled("echo") is True


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
