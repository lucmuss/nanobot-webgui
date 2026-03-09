"""MCP repository analysis, install, and runtime probing for the GUI."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from nanobot.config.schema import MCPServerConfig
from nanobot.gui.config_service import GUIConfigService


class GUIMCPService:
    """Analyze and install MCP servers from GitHub repositories."""

    def __init__(self, config_service: GUIConfigService, logger: logging.Logger) -> None:
        self.config_service = config_service
        self.logger = logger

    async def analyze_repository(self, source: str) -> dict[str, Any]:
        """Inspect a GitHub repository and derive an install plan."""
        repo = _parse_repository_source(source)
        checkout_dir = await self._clone_repository(repo["clone_url"])
        try:
            analysis = self._inspect_checkout(checkout_dir, repo)
        finally:
            shutil.rmtree(checkout_dir, ignore_errors=True)
        return analysis

    async def install_repository(self, source: str) -> dict[str, Any]:
        """Install and register a GitHub-hosted MCP server."""
        analysis = await self.analyze_repository(source)
        server_name = analysis["server_name"]
        install_dir = self.config_service.mcp_installs_dir / analysis["install_slug"]
        install_dir.parent.mkdir(parents=True, exist_ok=True)
        install_logs: list[str] = []

        if install_dir.exists():
            await self._update_checkout(install_dir)
            install_logs.append("$ git pull --ff-only\nUpdated existing checkout.")
        else:
            await self._clone_repository(analysis["clone_url"], target_dir=install_dir)
            install_logs.append(f"$ git clone --depth 1 {analysis['clone_url']} {install_dir}\nClone completed.")

        for step in analysis["install_steps"]:
            output, error = await self._run_command(step["command"], cwd=install_dir, timeout=step["timeout"])
            tail = "\n".join((output or error or "(no output)").splitlines()[-12:])
            install_logs.append(f"$ {step['display']}\n{tail}")

        config = self.config_service.load()
        existing = config.tools.mcp_servers.get(server_name)
        server_cfg = self._build_server_config(analysis, install_dir, existing, config)
        config.tools.mcp_servers[server_name] = server_cfg
        self.config_service.save(config)

        existing_record = self.config_service.get_mcp_record(server_name)
        provisional = {
            "server_name": server_name,
            "title": analysis["title"],
            "summary": analysis["summary"],
            "repo_url": analysis["repo_url"],
            "clone_url": analysis["clone_url"],
            "install_dir": str(install_dir),
            "install_steps": [step["display"] for step in analysis["install_steps"]],
            "required_env": analysis["required_env"],
            "optional_env": analysis["optional_env"],
            "healthcheck": analysis["healthcheck"],
            "evidence": analysis["evidence"],
            "last_installed_at": _utc_now(),
            "enabled": bool(existing_record.get("enabled", False)),
            "log_tail": "\n\n".join(install_logs)[-4000:],
        }
        self.config_service.set_mcp_record(server_name, provisional)

        record = await self.test_server(server_name)
        record.update(provisional)
        self.config_service.set_mcp_record(server_name, record)
        self.logger.info("mcp_installed server=%s source=%s", server_name, analysis["repo_url"])
        return record

    async def test_server(self, server_name: str) -> dict[str, Any]:
        """Probe one registered MCP server and persist its status."""
        config = self.config_service.load()
        cfg = config.tools.mcp_servers.get(server_name)
        if cfg is None:
            raise ValueError(f"MCP server '{server_name}' is not registered.")

        existing = self.config_service.get_mcp_record(server_name)
        missing_env = _missing_env_vars(existing.get("required_env", []), cfg.env)
        result: dict[str, Any] = {
            **existing,
            "server_name": server_name,
            "transport": _resolve_transport(cfg),
            "command": cfg.command,
            "args": list(cfg.args),
            "url": cfg.url,
            "tool_timeout": cfg.tool_timeout,
            "missing_env": missing_env,
            "tool_names": [],
            "last_checked_at": _utc_now(),
            "enabled": bool(existing.get("enabled", False)),
            "friendly_error": {},
            "log_tail": str(existing.get("log_tail", "")).strip(),
        }

        if missing_env:
            result["status"] = "needs_configuration"
            result["status_label"] = "Needs configuration"
            result["last_test_status"] = result["status"]
            result["last_test_label"] = result["status_label"]
            result["last_error"] = "Missing required environment variables: " + ", ".join(missing_env)
            result["log_tail"] = _append_log(result["log_tail"], result["last_error"])
            self.config_service.set_mcp_record(server_name, result)
            return result

        preflight = await self._preflight_server(cfg)
        if preflight:
            result["status"] = "error"
            result["status_label"] = "Probe failed"
            result["last_test_status"] = result["status"]
            result["last_test_label"] = result["status_label"]
            result["last_error"] = preflight
            result["log_tail"] = _append_log(result["log_tail"], preflight)
            self.config_service.set_mcp_record(server_name, result)
            return result

        try:
            tool_names = await self._list_server_tools(cfg)
        except Exception as exc:
            message = str(exc).strip() or f"{type(exc).__name__}"
            result["status"] = "error"
            result["status_label"] = "Probe failed"
            result["last_test_status"] = result["status"]
            result["last_test_label"] = result["status_label"]
            result["last_error"] = message
            result["log_tail"] = _append_log(result["log_tail"], message)
            self.config_service.set_mcp_record(server_name, result)
            self.logger.warning("mcp_probe_failed server=%s error=%s", server_name, message)
            return result

        result["tool_names"] = tool_names
        result["last_error"] = ""
        result["status"] = "active"
        result["status_label"] = "Active"
        result["last_test_status"] = result["status"]
        result["last_test_label"] = result["status_label"]
        result["log_tail"] = _append_log(
            result["log_tail"],
            "Connected successfully. Tools: " + (", ".join(tool_names) if tool_names else "(none)"),
        )
        self.config_service.set_mcp_record(server_name, result)
        self.logger.info("mcp_probe_ok server=%s tools=%s", server_name, len(tool_names))
        return result

    def remove_server(self, server_name: str) -> dict[str, Any]:
        """Remove one MCP server from config and delete its managed checkout when safe."""
        record = self.config_service.get_mcp_record(server_name)
        config = self.config_service.load()
        config.tools.mcp_servers.pop(server_name, None)
        self.config_service.save(config)

        install_dir_raw = record.get("install_dir")
        removed_checkout = False
        if isinstance(install_dir_raw, str) and install_dir_raw:
            install_dir = Path(install_dir_raw).expanduser()
            base_dir = self.config_service.mcp_installs_dir.resolve()
            try:
                if install_dir.resolve().is_relative_to(base_dir) and install_dir.exists():
                    shutil.rmtree(install_dir)
                    removed_checkout = True
            except FileNotFoundError:
                removed_checkout = False

        self.config_service.remove_mcp_record(server_name)
        self.logger.info("mcp_removed server=%s checkout_removed=%s", server_name, removed_checkout)
        return {"checkout_removed": removed_checkout}

    async def _clone_repository(self, clone_url: str, target_dir: Path | None = None) -> Path:
        """Clone a GitHub repository into a temp directory or the chosen target path."""
        if target_dir is None:
            tmp_root = self.config_service.runtime_dir / "tmp"
            tmp_root.mkdir(parents=True, exist_ok=True)
            target_dir = Path(tempfile.mkdtemp(prefix="mcp-analyze-", dir=tmp_root))
            await self._run_command(
                ["git", "clone", "--depth", "1", clone_url, str(target_dir)],
                cwd=tmp_root,
                timeout=180,
            )
            return target_dir

        await self._run_command(
            ["git", "clone", "--depth", "1", clone_url, str(target_dir)],
            cwd=target_dir.parent,
            timeout=300,
        )
        return target_dir

    async def _update_checkout(self, checkout_dir: Path) -> None:
        """Refresh an existing checkout without destroying local changes."""
        git_dir = checkout_dir / ".git"
        if not git_dir.exists():
            raise ValueError(f"Install directory exists but is not a git checkout: {checkout_dir}")
        await self._run_command(["git", "pull", "--ff-only"], cwd=checkout_dir, timeout=180)

    async def _run_command(
        self,
        command: list[str],
        *,
        cwd: Path,
        timeout: int,
    ) -> tuple[str, str]:
        """Run one installation command and raise a concise error on failure."""
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise ValueError(f"Command timed out after {timeout}s: {' '.join(command)}") from exc

        output = stdout.decode("utf-8", errors="replace").strip()
        error = stderr.decode("utf-8", errors="replace").strip()
        if process.returncode != 0:
            tail = "\n".join((error or output).splitlines()[-20:]).strip()
            raise ValueError(
                f"Command failed: {' '.join(command)}"
                + (f"\n{tail}" if tail else "")
            )
        return output, error

    def _inspect_checkout(self, checkout_dir: Path, repo: dict[str, str]) -> dict[str, Any]:
        """Build a best-effort install plan from the repository contents."""
        package_json = _read_json(checkout_dir / "package.json")
        pyproject = _read_text(checkout_dir / "pyproject.toml")
        readme_summary = _extract_readme_summary(checkout_dir / "README.md")
        example_config = _load_mcp_example(checkout_dir)
        required_env, optional_env = _collect_env_requirements(checkout_dir, example_config)

        install_steps: list[dict[str, Any]] = []
        run_command = ""
        run_args: list[str] = []
        transport = "stdio"
        evidence: list[str] = []

        if example_config:
            evidence.append(f"Example MCP config: {example_config['source_file']}")
            if example_config.get("transport") == "stdio":
                run_command = str(example_config.get("command", "")).strip()
                run_args = list(example_config.get("args", []))
            transport = str(example_config.get("transport", "stdio"))

        if package_json:
            package_name = str(package_json.get("name", "")).strip()
            scripts = package_json.get("scripts") or {}
            install_steps.append(
                {
                    "command": ["npm", "ci"] if (checkout_dir / "package-lock.json").exists() else ["npm", "install"],
                    "display": "npm ci" if (checkout_dir / "package-lock.json").exists() else "npm install",
                    "timeout": 900,
                }
            )
            if "build" in scripts:
                install_steps.append(
                    {"command": ["npm", "run", "build"], "display": "npm run build", "timeout": 900}
                )
                evidence.append("package.json scripts.build")
            if not run_command:
                run_command, run_args = _derive_node_entry(checkout_dir, package_json)
            evidence.append(f"package.json name={package_name or repo['repo']}")
        elif pyproject:
            install_steps.append(
                {"command": ["uv", "pip", "install", "-e", "."], "display": "uv pip install -e .", "timeout": 900}
            )
            evidence.append("pyproject.toml")
            if not run_command:
                run_command, run_args = _derive_python_entry(checkout_dir)
        else:
            raise ValueError("Could not derive an install plan for this repository.")

        if not run_command:
            raise ValueError("Could not derive the MCP startup command from the repository.")

        server_name = _derive_server_name(repo["repo"], example_config, package_json)
        install_slug = f"{repo['owner']}__{repo['repo']}".lower()
        summary = readme_summary or str((package_json or {}).get("description", "")).strip() or "No summary available."

        return {
            "server_name": server_name,
            "title": f"{repo['owner']}/{repo['repo']}",
            "summary": summary,
            "repo_url": repo["repo_url"],
            "clone_url": repo["clone_url"],
            "install_slug": install_slug,
            "transport": transport,
            "run_command": run_command,
            "run_args": run_args,
            "install_steps": install_steps,
            "required_env": required_env,
            "optional_env": optional_env,
            "healthcheck": "Start the MCP transport and list tools through an MCP client handshake.",
            "evidence": evidence,
        }

    def _build_server_config(
        self,
        analysis: dict[str, Any],
        install_dir: Path,
        existing: MCPServerConfig | None,
        config,
    ) -> MCPServerConfig:
        """Create the MCP config entry using the derived install plan."""
        env_defaults = _guess_env_defaults(
            config=config,
            server_name=analysis["server_name"],
            required_env=analysis["required_env"],
            optional_env=analysis["optional_env"],
            workspace=self.config_service.default_workspace,
        )
        existing_env = dict(existing.env) if existing else {}
        env = {**env_defaults, **existing_env}
        args = [_expand_install_path(str(arg), install_dir) for arg in analysis["run_args"]]
        return MCPServerConfig(
            type=analysis["transport"] or None,
            command=analysis["run_command"],
            args=args,
            env=env,
            url="",
            headers=dict(existing.headers) if existing else {},
            tool_timeout=existing.tool_timeout if existing else 30,
        )

    async def _preflight_server(self, cfg: MCPServerConfig) -> str:
        """Run a short stdio preflight so hard startup failures surface with stderr."""
        if _resolve_transport(cfg) != "stdio" or not cfg.command:
            return ""

        env = os.environ.copy()
        env.update({key: value for key, value in (cfg.env or {}).items() if value})
        process = await asyncio.create_subprocess_exec(
            cfg.command,
            *cfg.args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            await asyncio.sleep(2)
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
                return ""

            stderr = await process.stderr.read() if process.stderr is not None else b""
            return stderr.decode("utf-8", errors="replace").strip()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def _list_server_tools(self, cfg: MCPServerConfig) -> list[str]:
        """Connect to the MCP server and return its exposed tool names."""
        transport = _resolve_transport(cfg)
        if transport == "stdio":
            return await _list_stdio_tools(cfg)
        if transport == "sse":
            return await _list_sse_tools(cfg)
        if transport == "streamableHttp":
            return await _list_streamable_http_tools(cfg)
        raise ValueError(f"Unsupported MCP transport: {transport}")


def _parse_repository_source(source: str) -> dict[str, str]:
    """Normalize supported repository inputs into GitHub clone metadata."""
    raw = source.strip()
    if not raw:
        raise ValueError("Enter a GitHub repository URL before analyzing or installing.")

    if re.fullmatch(r"[\w.-]+/[\w.-]+", raw):
        owner, repo = raw.split("/", 1)
        repo = repo.removesuffix(".git")
        return {
            "owner": owner,
            "repo": repo,
            "repo_url": f"https://github.com/{owner}/{repo}",
            "clone_url": f"https://github.com/{owner}/{repo}.git",
        }

    if raw.startswith("git@github.com:"):
        _, path = raw.split(":", 1)
        owner, repo = path.split("/", 1)
        repo = repo.removesuffix(".git")
        return {
            "owner": owner,
            "repo": repo,
            "repo_url": f"https://github.com/{owner}/{repo}",
            "clone_url": f"https://github.com/{owner}/{repo}.git",
        }

    parsed = urlparse(raw)
    host = parsed.netloc.lower()
    if host not in {"github.com", "www.github.com"}:
        raise ValueError("Only direct GitHub repository URLs are supported right now.")

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        raise ValueError("Enter a full GitHub repository URL like https://github.com/owner/repo.")

    owner, repo = parts[0], parts[1].removesuffix(".git")
    return {
        "owner": owner,
        "repo": repo,
        "repo_url": f"https://github.com/{owner}/{repo}",
        "clone_url": f"https://github.com/{owner}/{repo}.git",
    }


def _read_text(path: Path) -> str:
    """Read one text file if it exists."""
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _read_json(path: Path) -> dict[str, Any]:
    """Read one JSON file if it exists and is valid."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _extract_readme_summary(path: Path) -> str:
    """Return the first useful README paragraph."""
    content = _read_text(path)
    if not content:
        return ""
    blocks = [block.strip() for block in re.split(r"\n\s*\n", content) if block.strip()]
    for block in blocks:
        if block.startswith("#"):
            continue
        clean = " ".join(line.strip() for line in block.splitlines())
        if clean:
            return clean[:400]
    return ""


def _load_mcp_example(checkout_dir: Path) -> dict[str, Any]:
    """Load a repository-provided MCP example config if present."""
    candidates = [
        checkout_dir / "mcp-settings-example.json",
        checkout_dir / "mcp.json",
        checkout_dir / "mcp-settings.json",
    ]
    for candidate in candidates:
        data = _read_json(candidate)
        if not data:
            continue
        servers = data.get("mcpServers")
        if not isinstance(servers, dict) or not servers:
            continue
        server_name, server_cfg = next(iter(servers.items()))
        if not isinstance(server_cfg, dict):
            continue
        return {
            "source_file": candidate.name,
            "server_name": str(server_name),
            "transport": "stdio" if server_cfg.get("command") else "streamableHttp" if server_cfg.get("url") else "",
            "command": str(server_cfg.get("command", "")).strip(),
            "args": [str(item) for item in server_cfg.get("args", []) if str(item).strip()],
            "env": {
                str(key): str(value)
                for key, value in (server_cfg.get("env") or {}).items()
                if str(key).strip()
            },
        }
    return {}


def _collect_env_requirements(
    checkout_dir: Path,
    example_config: dict[str, Any],
) -> tuple[list[str], list[str]]:
    """Infer required and optional environment variables from repo examples."""
    required: list[str] = []
    optional: list[str] = []

    for filename in (".env.example", ".env.sample", ".env.template"):
        path = checkout_dir / filename
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("#"):
                match = re.match(r"#\s*([A-Z][A-Z0-9_]*)=", line)
                if match:
                    optional.append(match.group(1))
                continue
            match = re.match(r"([A-Z][A-Z0-9_]*)=", line)
            if match:
                required.append(match.group(1))

    env_keys = list((example_config.get("env") or {}).keys())
    for key in env_keys:
        if key not in required and key not in optional:
            required.append(key)

    return _unique(required), _unique(optional)


def _derive_node_entry(checkout_dir: Path, package_json: dict[str, Any]) -> tuple[str, list[str]]:
    """Best-effort runtime command for Node-based MCP servers."""
    scripts = package_json.get("scripts") or {}
    bin_map = package_json.get("bin") or {}

    if isinstance(bin_map, dict) and bin_map:
        entry = next(iter(bin_map.values()))
        entry_path = Path(str(entry))
        return "node", [str(_expand_install_path(str(entry_path), checkout_dir))]

    if "start" in scripts and (checkout_dir / "build" / "index.js").exists():
        return "node", [str(checkout_dir / "build" / "index.js")]
    if (checkout_dir / "build" / "index.js").exists():
        return "node", [str(checkout_dir / "build" / "index.js")]
    if (checkout_dir / "dist" / "index.js").exists():
        return "node", [str(checkout_dir / "dist" / "index.js")]

    return "", []


def _derive_python_entry(checkout_dir: Path) -> tuple[str, list[str]]:
    """Best-effort runtime command for Python-based MCP servers."""
    if (checkout_dir / "src" / "main.py").exists():
        return "python", [str(checkout_dir / "src" / "main.py")]
    if (checkout_dir / "main.py").exists():
        return "python", [str(checkout_dir / "main.py")]
    return "", []


def _derive_server_name(
    repo_name: str,
    example_config: dict[str, Any],
    package_json: dict[str, Any],
) -> str:
    """Pick a stable MCP server name."""
    preferred = str(example_config.get("server_name", "")).strip()
    if preferred:
        return _slugify(preferred)

    package_name = str(package_json.get("name", "")).strip()
    if package_name:
        for suffix in ("-server", "-mcp-server", "-mcp"):
            if package_name.endswith(suffix):
                package_name = package_name[: -len(suffix)]
                break
        if package_name:
            return _slugify(package_name)

    return _slugify(repo_name)


def _guess_env_defaults(
    *,
    config,
    server_name: str,
    required_env: list[str],
    optional_env: list[str],
    workspace: Path,
) -> dict[str, str]:
    """Pre-fill obvious MCP env values from the current nanobot config."""
    defaults: dict[str, str] = {}
    mappings = {
        "OPENAI_API_KEY": config.providers.openai.api_key,
        "ANTHROPIC_API_KEY": config.providers.anthropic.api_key,
        "MOONSHOT_API_KEY": config.providers.moonshot.api_key,
        "OPENROUTER_API_KEY": config.providers.openrouter.api_key,
        "BRAVE_API_KEY": config.tools.web.search.api_key,
    }
    for env_name in required_env + optional_env:
        value = mappings.get(env_name)
        if value:
            defaults[env_name] = value

    if "SAVE_DIR" in required_env or "SAVE_DIR" in optional_env:
        save_dir = workspace / "mcp-output" / server_name
        save_dir.mkdir(parents=True, exist_ok=True)
        defaults.setdefault("SAVE_DIR", str(save_dir))
    return defaults


def _missing_env_vars(required_env: list[str], current_env: dict[str, str]) -> list[str]:
    """Return the required env vars that are still empty."""
    missing = []
    for env_name in required_env:
        if not str((current_env or {}).get(env_name, "")).strip():
            missing.append(env_name)
    return missing


def _expand_install_path(value: str, install_dir: Path) -> str:
    """Replace common placeholder prefixes with the actual install path."""
    if not value:
        return value
    if value.startswith("/path/to/"):
        parts = list(Path(value).parts)
        for marker in ("build", "dist", "src"):
            if marker in parts:
                return str(install_dir / Path(*parts[parts.index(marker) :]))
        return str(install_dir / Path(value).name)
    if value.startswith("./"):
        return str(install_dir / value[2:])
    if value.startswith("build/") or value.startswith("dist/"):
        return str(install_dir / value)
    return value


def _resolve_transport(cfg: MCPServerConfig) -> str:
    """Resolve the effective transport for one MCP server config."""
    if cfg.type:
        return cfg.type
    if cfg.command:
        return "stdio"
    if cfg.url.rstrip("/").endswith("/sse"):
        return "sse"
    return "streamableHttp"


async def _list_stdio_tools(cfg: MCPServerConfig) -> list[str]:
    """List tools from a stdio MCP server."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = os.environ.copy()
    env.update({key: value for key, value in (cfg.env or {}).items() if value})
    params = StdioServerParameters(command=cfg.command, args=cfg.args, env=env)
    async with AsyncExitStack() as stack:
        read, write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read, write))
        await asyncio.wait_for(session.initialize(), timeout=15)
        tools = await asyncio.wait_for(session.list_tools(), timeout=15)
    return [tool.name for tool in tools.tools]


async def _list_sse_tools(cfg: MCPServerConfig) -> list[str]:
    """List tools from an SSE MCP server."""
    import httpx
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    def httpx_client_factory(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        merged_headers = {**(cfg.headers or {}), **(headers or {})}
        return httpx.AsyncClient(
            headers=merged_headers or None,
            follow_redirects=True,
            timeout=timeout,
            auth=auth,
        )

    async with AsyncExitStack() as stack:
        read, write = await stack.enter_async_context(
            sse_client(cfg.url, httpx_client_factory=httpx_client_factory)
        )
        session = await stack.enter_async_context(ClientSession(read, write))
        await asyncio.wait_for(session.initialize(), timeout=15)
        tools = await asyncio.wait_for(session.list_tools(), timeout=15)
    return [tool.name for tool in tools.tools]


async def _list_streamable_http_tools(cfg: MCPServerConfig) -> list[str]:
    """List tools from a streamable HTTP MCP server."""
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with AsyncExitStack() as stack:
        http_client = await stack.enter_async_context(
            httpx.AsyncClient(headers=cfg.headers or None, follow_redirects=True, timeout=None)
        )
        read, write, _ = await stack.enter_async_context(
            streamable_http_client(cfg.url, http_client=http_client)
        )
        session = await stack.enter_async_context(ClientSession(read, write))
        await asyncio.wait_for(session.initialize(), timeout=15)
        tools = await asyncio.wait_for(session.list_tools(), timeout=15)
    return [tool.name for tool in tools.tools]


def _slugify(value: str) -> str:
    """Convert free-form labels into safe MCP server names."""
    clean = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip().lower()).strip("-")
    return clean or "mcp-server"


def _unique(values: list[str]) -> list[str]:
    """Deduplicate string values while preserving order."""
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _utc_now() -> str:
    """Return a compact UTC timestamp for GUI state entries."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _append_log(existing: str, line: str) -> str:
    """Append one line block to the stored MCP log tail."""
    combined = "\n\n".join(part for part in [existing.strip(), line.strip()] if part)
    return combined[-4000:]
