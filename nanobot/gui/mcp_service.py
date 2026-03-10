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
from nanobot.gui.repair_worker import REPAIR_RECIPE_DETAILS, supported_repair_recipes


class GUIMCPService:
    """Analyze and install MCP servers from GitHub repositories."""

    def __init__(self, config_service: GUIConfigService, logger: logging.Logger) -> None:
        self.config_service = config_service
        self.logger = logger
        self.ai_plan_builder = None
        self.ai_repair_planner = None

    async def analyze_repository(self, source: str, *, allow_ai_fallback: bool = False) -> dict[str, Any]:
        """Inspect a GitHub repository and derive an install plan."""
        repo = _parse_repository_source(source)
        checkout_dir = await self._clone_repository(repo["clone_url"])
        try:
            repo_bundle = self._build_repository_bundle(checkout_dir, repo)
            try:
                analysis = self._inspect_checkout(checkout_dir, repo)
            except ValueError as exc:
                analysis = None
                if allow_ai_fallback and self.ai_plan_builder is not None:
                    fallback = await self._plan_with_ai_fallback(
                        repo=repo,
                        repo_bundle=repo_bundle,
                        reason=str(exc),
                    )
                    if fallback is not None:
                        return fallback
                raise

            analysis = self._enrich_analysis(analysis)
            if allow_ai_fallback and self.ai_plan_builder is not None and _analysis_needs_ai_fallback(analysis):
                fallback = await self._plan_with_ai_fallback(
                    repo=repo,
                    repo_bundle=repo_bundle,
                    deterministic=analysis,
                    reason=str(analysis.get("fallback_reason", "")).strip(),
                )
                if fallback is not None:
                    analysis = fallback
        finally:
            shutil.rmtree(checkout_dir, ignore_errors=True)
        return analysis

    async def install_repository(self, source: str, *, allow_ai_fallback: bool = False) -> dict[str, Any]:
        """Install and register a GitHub-hosted MCP server."""
        analysis = await self.analyze_repository(source, allow_ai_fallback=allow_ai_fallback)
        server_name = analysis["server_name"]
        install_logs: list[str] = []
        install_mode = str(analysis.get("install_mode", "source")).strip() or "source"
        install_dir: Path | None = None

        missing_runtimes = [str(item) for item in analysis.get("missing_runtimes", [])]
        if missing_runtimes:
            raise ValueError(
                "Missing required runtime tools for this MCP: " + ", ".join(missing_runtimes)
            )

        if install_mode == "source":
            install_dir = self.config_service.mcp_installs_dir / analysis["install_slug"]
            install_dir.parent.mkdir(parents=True, exist_ok=True)

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
        else:
            for step in analysis["install_steps"]:
                install_logs.append(f"$ {step['display']}\nRegistered without a managed checkout.")

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
            "install_dir": str(install_dir) if install_dir is not None else "",
            "install_steps": [step["display"] for step in analysis["install_steps"]],
            "required_env": analysis["required_env"],
            "optional_env": analysis["optional_env"],
            "healthcheck": analysis["healthcheck"],
            "evidence": analysis["evidence"],
            "repo_type": analysis.get("repo_type", ""),
            "analysis_mode": analysis.get("analysis_mode", "deterministic"),
            "analysis_confidence": analysis.get("analysis_confidence", 0.0),
            "required_runtimes": analysis.get("required_runtimes", []),
            "runtime_status": analysis.get("runtime_status", []),
            "missing_runtimes": analysis.get("missing_runtimes", []),
            "next_action": analysis.get("next_action", ""),
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
            message = _summarize_exception(exc)
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

    async def build_repair_plan(self, server_name: str, *, allow_unrestricted: bool = False) -> dict[str, Any]:
        """Build a bounded repair plan for one installed MCP server."""
        config = self.config_service.load()
        cfg = config.tools.mcp_servers.get(server_name)
        if cfg is None:
            raise ValueError(f"MCP server '{server_name}' is not registered.")

        record = self.refresh_runtime_requirements(server_name)
        missing_runtimes = [str(item) for item in record.get("missing_runtimes", []) if str(item).strip()]
        missing_env = _missing_env_vars(record.get("required_env", []), cfg.env)
        recipes = supported_repair_recipes(missing_runtimes)
        plan = {
            "server_name": server_name,
            "missing_runtime": missing_runtimes[0] if missing_runtimes else "",
            "missing_runtimes": missing_runtimes,
            "required_env": missing_env,
            "recommended_recipe": recipes[0] if recipes else "",
            "available_recipes": recipes,
            "next_step": _describe_repair_next_step(missing_runtimes=missing_runtimes, missing_env=missing_env),
            "confidence": 0.95 if recipes else 0.45,
            "shell_command": "",
            "source": "deterministic",
            "supported": bool(recipes),
        }
        if recipes:
            return plan

        if self.ai_repair_planner is None:
            return plan

        bundle = {
            "server_name": server_name,
            "repo_url": str(record.get("repo_url", "")),
            "repo_type": str(record.get("repo_type", "")),
            "analysis_mode": str(record.get("analysis_mode", "")),
            "required_runtimes": list(record.get("required_runtimes", [])),
            "runtime_status": list(record.get("runtime_status", [])),
            "missing_runtimes": missing_runtimes,
            "missing_env": missing_env,
            "required_env": list(record.get("required_env", [])),
            "last_error": str(record.get("last_error", "")),
            "next_action": str(record.get("next_action", "")),
            "allow_unrestricted_agent_shell": bool(allow_unrestricted),
        }
        try:
            ai_plan = await self.ai_repair_planner(bundle)
            return _normalize_ai_repair_plan(
                ai_plan,
                deterministic=plan,
                allow_unrestricted=allow_unrestricted,
            )
        except Exception as exc:
            self.logger.warning("mcp_ai_repair_fallback_failed server=%s error=%s", server_name, _summarize_exception(exc))
            return plan

    def refresh_runtime_requirements(self, server_name: str) -> dict[str, Any]:
        """Re-evaluate runtime availability for one stored MCP record."""
        record = self.config_service.get_mcp_record(server_name)
        required_runtimes = [str(item) for item in record.get("required_runtimes", []) if str(item).strip()]
        runtime_status = _check_runtime_requirements(required_runtimes)
        missing_runtimes = [item["name"] for item in runtime_status if not item["available"]]
        updated = {
            **record,
            "runtime_status": runtime_status,
            "missing_runtimes": missing_runtimes,
            "can_install": not missing_runtimes,
            "next_action": _describe_repair_next_step(
                missing_runtimes=missing_runtimes,
                missing_env=[str(item) for item in record.get("missing_env", []) if str(item).strip()],
            ),
        }
        self.config_service.set_mcp_record(server_name, updated)
        return updated

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
        server_manifest = _load_server_manifest(checkout_dir)
        workspace_package = _find_workspace_mcp_package(checkout_dir)
        readme_summary = _extract_readme_summary(checkout_dir / "README.md")
        example_config = _load_mcp_example(checkout_dir)
        required_env, optional_env = _collect_env_requirements(checkout_dir, example_config, server_manifest)

        install_steps: list[dict[str, Any]] = []
        run_command = ""
        run_args: list[str] = []
        transport = "stdio"
        run_url = ""
        evidence: list[str] = []
        install_mode = "source"

        if example_config:
            evidence.append(f"Example MCP config: {example_config['source_file']}")
            if example_config.get("transport") == "stdio":
                run_command = str(example_config.get("command", "")).strip()
                run_args = list(example_config.get("args", []))
            elif example_config.get("transport") in {"sse", "streamableHttp"}:
                run_url = str(example_config.get("url", "")).strip()
            transport = str(example_config.get("transport", "stdio"))

        manifest_choice = _select_server_manifest_install(server_manifest)
        if manifest_choice:
            install_mode = str(manifest_choice.get("type", "")).strip() or install_mode
            transport = str(manifest_choice.get("transport", "")).strip() or transport
            evidence.extend(manifest_choice.get("evidence", []))
            if install_mode == "npm":
                package_spec = _package_spec(
                    str(manifest_choice.get("identifier", "")).strip(),
                    str(manifest_choice.get("version", "")).strip(),
                )
                run_command = "npx"
                run_args = ["-y", package_spec]
                install_steps.append(
                    {
                        "command": [],
                        "display": f"Register npm package runtime via npx {package_spec}",
                        "timeout": 0,
                    }
                )
            elif install_mode == "remote":
                run_url = str(manifest_choice.get("url", "")).strip()
                install_steps.append(
                    {
                        "command": [],
                        "display": f"Register remote MCP endpoint {run_url}",
                        "timeout": 0,
                    }
                )
            elif install_mode == "oci":
                image = str(manifest_choice.get("identifier", "")).strip()
                run_command = "docker"
                run_args = ["run", "-i", "--rm", *_build_oci_runtime_args(manifest_choice), image]
                install_steps.append(
                    {
                        "command": [],
                        "display": f"Register OCI runtime via docker {image}",
                        "timeout": 0,
                    }
                )

        if not run_command and not run_url and package_json:
            if workspace_package:
                workspace_spec = _package_spec(
                    str(workspace_package.get("name", "")).strip(),
                    str(workspace_package.get("version", "")).strip(),
                )
                run_command = "npx"
                run_args = ["-y", workspace_spec]
                install_mode = "workspace_package"
                install_steps.append(
                    {
                        "command": [],
                        "display": f"Register workspace MCP package via npx {workspace_spec}",
                        "timeout": 0,
                    }
                )
                evidence.append(f"workspace package name={workspace_package['name']}")
                evidence.append(f"workspace package path={workspace_package['path']}")
            else:
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
        elif not run_command and not run_url and pyproject:
            install_steps.append(
                {"command": ["uv", "pip", "install", "-e", "."], "display": "uv pip install -e .", "timeout": 900}
            )
            evidence.append("pyproject.toml")
            if not run_command:
                run_command, run_args = _derive_python_entry(checkout_dir)
        elif not run_command and not run_url:
            raise ValueError("Could not derive an install plan for this repository.")

        if not run_command and not run_url:
            raise ValueError("Could not derive the MCP startup command from the repository.")

        if run_url and transport not in {"streamableHttp", "sse"}:
            transport = "streamableHttp"

        server_name = _derive_server_name(repo["repo"], example_config, package_json, server_manifest, workspace_package)
        install_slug = f"{repo['owner']}__{repo['repo']}".lower()
        summary = readme_summary or str((package_json or {}).get("description", "")).strip() or "No summary available."

        return {
            "server_name": server_name,
            "title": f"{repo['owner']}/{repo['repo']}",
            "summary": summary,
            "repo_url": repo["repo_url"],
            "clone_url": repo["clone_url"],
            "install_slug": install_slug,
            "install_mode": install_mode,
            "transport": transport,
            "run_command": run_command,
            "run_args": run_args,
            "run_url": run_url,
            "install_steps": install_steps,
            "required_env": required_env,
            "optional_env": optional_env,
            "healthcheck": "Start the MCP transport and list tools through an MCP client handshake.",
            "evidence": evidence,
            "repo_type": _detect_repo_type(
                install_mode=install_mode,
                package_json=package_json,
                pyproject=pyproject,
                server_manifest=server_manifest,
                workspace_package=workspace_package,
                run_url=run_url,
                checkout_dir=checkout_dir,
            ),
            "analysis_mode": "deterministic",
            "analysis_confidence": _estimate_analysis_confidence(
                install_mode=install_mode,
                example_config=example_config,
                server_manifest=server_manifest,
                workspace_package=workspace_package,
                package_json=package_json,
                pyproject=pyproject,
                run_url=run_url,
            ),
        }

    def _build_repository_bundle(self, checkout_dir: Path, repo: dict[str, str]) -> dict[str, Any]:
        """Collect bounded repository evidence for optional AI fallback planning."""
        workspace_packages = []
        for package_path in sorted(checkout_dir.glob("packages/*/package.json"))[:6]:
            workspace_packages.append(
                {
                    "path": str(package_path.relative_to(checkout_dir)),
                    "package_json": _read_json(package_path),
                }
            )

        files = sorted(
            str(path.relative_to(checkout_dir))
            for path in checkout_dir.iterdir()
            if path.name not in {".git", "node_modules"}
        )
        return {
            "repo": repo,
            "top_level_files": files[:40],
            "readme_excerpt": _limit_text(_read_text(checkout_dir / "README.md"), 8000),
            "package_json": _read_json(checkout_dir / "package.json"),
            "pyproject_toml": _limit_text(_read_text(checkout_dir / "pyproject.toml"), 6000),
            "server_json": _read_json(checkout_dir / "server.json"),
            "dockerfile": _limit_text(_read_text(checkout_dir / "Dockerfile"), 4000),
            "example_mcp_config": _load_mcp_example(checkout_dir),
            "workspace_packages": workspace_packages,
        }

    async def _plan_with_ai_fallback(
        self,
        *,
        repo: dict[str, str],
        repo_bundle: dict[str, Any],
        deterministic: dict[str, Any] | None = None,
        reason: str = "",
    ) -> dict[str, Any] | None:
        """Ask the configured AI planner for a bounded fallback plan and validate it."""
        if self.ai_plan_builder is None:
            return None

        bundle = {
            **repo_bundle,
            "deterministic_analysis": deterministic or {},
            "fallback_reason": reason,
        }
        try:
            raw_plan = await self.ai_plan_builder(bundle)
            analysis = _normalize_ai_plan(raw_plan, repo=repo, deterministic=deterministic or {})
        except Exception as exc:
            self.logger.warning("mcp_ai_fallback_failed repo=%s error=%s", repo["repo_url"], _summarize_exception(exc))
            return None
        self.logger.info("mcp_ai_fallback_used repo=%s server=%s", repo["repo_url"], analysis["server_name"])
        return self._enrich_analysis(analysis)

    def _enrich_analysis(self, analysis: dict[str, Any]) -> dict[str, Any]:
        """Add runtime checks and next-step guidance to one install plan."""
        enriched = dict(analysis)
        required_runtimes = _derive_required_runtimes(enriched)
        runtime_status = _check_runtime_requirements(required_runtimes)
        missing_runtimes = [item["name"] for item in runtime_status if not item["available"]]
        enriched["required_runtimes"] = required_runtimes
        enriched["runtime_status"] = runtime_status
        enriched["missing_runtimes"] = missing_runtimes
        enriched["can_install"] = not missing_runtimes
        enriched["next_action"] = _describe_next_mcp_action(enriched)
        return enriched

    def _build_server_config(
        self,
        analysis: dict[str, Any],
        install_dir: Path | None,
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
        headers = dict(existing.headers) if existing else {}
        if analysis.get("run_url"):
            return MCPServerConfig(
                type=analysis["transport"] or None,
                command="",
                args=[],
                env=env,
                url=str(analysis.get("run_url", "")).strip(),
                headers=headers,
                tool_timeout=existing.tool_timeout if existing else 30,
            )

        if install_dir is None:
            args = [str(arg) for arg in analysis["run_args"]]
        else:
            args = [_expand_install_path(str(arg), install_dir) for arg in analysis["run_args"]]
        return MCPServerConfig(
            type=analysis["transport"] or None,
            command=analysis["run_command"],
            args=args,
            env=env,
            url="",
            headers=headers,
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
        if block.startswith("#") or block.startswith("```"):
            continue
        clean = _sanitize_summary_text(block)
        if clean:
            return clean[:400]
    return ""


def _sanitize_summary_text(raw: str) -> str:
    """Strip non-readable markup from README summary candidates."""
    text = raw.strip()
    if not text:
        return ""

    text = re.sub(r"!\[[^\]]*]\([^)]*\)", " ", text)
    text = re.sub(r"<img\b[^>]*>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"</?[^>]+>", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = " ".join(line.strip() for line in text.splitlines() if line.strip())
    text = re.sub(r"\s+", " ", text).strip()
    if not re.search(r"[A-Za-z0-9]", text):
        return ""
    return text


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
            "url": str(server_cfg.get("url", "")).strip(),
            "env": {
                str(key): str(value)
                for key, value in (server_cfg.get("env") or {}).items()
                if str(key).strip()
            },
        }
    return {}


def _load_server_manifest(checkout_dir: Path) -> dict[str, Any]:
    """Load the standard MCP server manifest if present."""
    data = _read_json(checkout_dir / "server.json")
    return data if data else {}


def _select_server_manifest_install(server_manifest: dict[str, Any]) -> dict[str, Any]:
    """Pick the most practical install target from a server.json manifest."""
    if not server_manifest:
        return {}

    title = str(server_manifest.get("title", "")).strip()
    name = str(server_manifest.get("name", "")).strip()
    evidence = []
    if title or name:
        evidence.append(f"server.json name={title or name}")

    remotes = server_manifest.get("remotes")
    if isinstance(remotes, list):
        for remote in remotes:
            if not isinstance(remote, dict):
                continue
            remote_type = str(remote.get("type", "")).strip().lower()
            url = str(remote.get("url", "")).strip()
            if remote_type in {"streamable-http", "streamablehttp"} and url:
                return {
                    "type": "remote",
                    "transport": "streamableHttp",
                    "url": url,
                    "evidence": [*evidence, f"server.json remote={url}"],
                }
            if remote_type == "sse" and url:
                return {
                    "type": "remote",
                    "transport": "sse",
                    "url": url,
                    "evidence": [*evidence, f"server.json remote={url}"],
                }

    packages = server_manifest.get("packages")
    if isinstance(packages, list):
        npm_package = None
        oci_package = None
        for package in packages:
            if not isinstance(package, dict):
                continue
            registry_type = str(package.get("registryType", "")).strip().lower()
            if registry_type == "npm" and npm_package is None:
                npm_package = package
            if registry_type == "oci" and oci_package is None:
                oci_package = package
        if npm_package is not None:
            return {
                "type": "npm",
                "transport": _normalize_manifest_transport(npm_package),
                "identifier": str(npm_package.get("identifier", "")).strip(),
                "version": str(npm_package.get("version", "")).strip(),
                "evidence": [*evidence, f"server.json npm={npm_package.get('identifier', '')}"],
            }
        if oci_package is not None:
            return {
                "type": "oci",
                "transport": _normalize_manifest_transport(oci_package),
                "identifier": str(oci_package.get("identifier", "")).strip(),
                "runtimeArguments": list(oci_package.get("runtimeArguments") or []),
                "evidence": [*evidence, f"server.json oci={oci_package.get('identifier', '')}"],
            }

    return {}


def _normalize_manifest_transport(payload: dict[str, Any]) -> str:
    """Normalize manifest transport names to the config schema vocabulary."""
    transport = payload.get("transport")
    if not isinstance(transport, dict):
        return "stdio"
    transport_type = str(transport.get("type", "")).strip().lower()
    if transport_type in {"streamable-http", "streamablehttp"}:
        return "streamableHttp"
    if transport_type == "sse":
        return "sse"
    return "stdio"


def _find_workspace_mcp_package(checkout_dir: Path) -> dict[str, str]:
    """Look for a nested workspace package that exposes the actual MCP runtime."""
    candidates: list[dict[str, str | int]] = []
    for package_path in sorted(checkout_dir.glob("packages/*/package.json")):
        package_json = _read_json(package_path)
        if not package_json:
            continue
        package_name = str(package_json.get("name", "")).strip()
        mcp_name = str(package_json.get("mcpName", "")).strip()
        bin_map = package_json.get("bin") or {}
        score = 0
        if mcp_name:
            score += 5
        if isinstance(bin_map, dict) and bin_map:
            score += 3
        if "mcp" in package_name.lower():
            score += 2
        if "mcp" in package_path.parent.name.lower():
            score += 1
        if score <= 0 or not package_name:
            continue
        candidates.append(
            {
                "name": package_name,
                "version": str(package_json.get("version", "")).strip(),
                "path": str(package_path.relative_to(checkout_dir)),
                "score": score,
            }
        )

    if not candidates:
        return {}
    selected = max(candidates, key=lambda item: (int(item["score"]), len(str(item["name"]))))
    return {
        "name": str(selected["name"]),
        "version": str(selected["version"]),
        "path": str(selected["path"]),
    }


def _package_spec(identifier: str, version: str) -> str:
    """Return an npx-friendly package spec."""
    if not identifier:
        return ""
    return identifier


def _limit_text(content: str, limit: int) -> str:
    """Trim large file content before sending it to the AI fallback planner."""
    text = content.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]..."


def _detect_repo_type(
    *,
    install_mode: str,
    package_json: dict[str, Any],
    pyproject: str,
    server_manifest: dict[str, Any],
    workspace_package: dict[str, str],
    run_url: str,
    checkout_dir: Path,
) -> str:
    """Classify the repository into a coarse install family for the UI."""
    if server_manifest:
        return "server_json"
    if workspace_package or any(checkout_dir.glob("packages/*/package.json")):
        return "monorepo"
    if run_url:
        return "remote"
    if package_json:
        return "npm"
    if pyproject:
        return "python"
    if (checkout_dir / "Dockerfile").exists() or install_mode == "oci":
        return "docker"
    return "unknown"


def _estimate_analysis_confidence(
    *,
    install_mode: str,
    example_config: dict[str, Any],
    server_manifest: dict[str, Any],
    workspace_package: dict[str, str],
    package_json: dict[str, Any],
    pyproject: str,
    run_url: str,
) -> float:
    """Return a coarse confidence score for deterministic MCP analysis."""
    score = 0.35
    if example_config:
        score += 0.3
    if server_manifest:
        score += 0.25
    if workspace_package:
        score += 0.15
    if package_json:
        score += 0.1
    if pyproject:
        score += 0.1
    if run_url:
        score += 0.05
    if install_mode in {"remote", "npm", "workspace_package"}:
        score += 0.05
    return round(min(score, 0.99), 2)


def _analysis_needs_ai_fallback(analysis: dict[str, Any]) -> bool:
    """Decide when the deterministic plan should ask the bounded AI fallback for help."""
    confidence = float(analysis.get("analysis_confidence", 0.0) or 0.0)
    repo_type = str(analysis.get("repo_type", "")).strip()
    if confidence < 0.55:
        analysis["fallback_reason"] = "Deterministic analysis confidence is low."
        return True
    if repo_type == "monorepo" and not analysis.get("run_command") and not analysis.get("run_url"):
        analysis["fallback_reason"] = "Monorepo detected without a clear MCP runtime package."
        return True
    return False


def _derive_required_runtimes(analysis: dict[str, Any]) -> list[str]:
    """List the host runtimes needed to execute the analyzed install plan."""
    runtimes: list[str] = []
    install_mode = str(analysis.get("install_mode", "")).strip()
    run_command = str(analysis.get("run_command", "")).strip()

    if install_mode in {"npm", "workspace_package"}:
        runtimes.extend(["node", "npx"])
    elif install_mode == "oci":
        runtimes.append("docker")

    for step in analysis.get("install_steps", []):
        command = step.get("command") if isinstance(step, dict) else []
        if not isinstance(command, list) or not command:
            continue
        head = str(command[0]).strip()
        if head == "npm":
            runtimes.extend(["node", "npm"])
        elif head == "uv":
            runtimes.append("uv")
        elif head in {"pip", "python", "python3"}:
            runtimes.extend(["python", "pip"])

    if run_command == "npx":
        runtimes.extend(["node", "npx"])
    elif run_command == "node":
        runtimes.append("node")
    elif run_command in {"python", "python3"}:
        runtimes.append("python")
    elif run_command in {"uv", "uvx"}:
        runtimes.append("uv")
    elif run_command == "docker":
        runtimes.append("docker")

    deduped: list[str] = []
    for name in runtimes:
        if name and name not in deduped:
            deduped.append(name)
    return deduped


def _check_runtime_requirements(required_runtimes: list[str]) -> list[dict[str, Any]]:
    """Check whether the container host currently exposes the required runtimes."""
    results: list[dict[str, Any]] = []
    for runtime in required_runtimes:
        checks = _runtime_exec_candidates(runtime)
        available_exec = next((candidate for candidate in checks if shutil.which(candidate)), "")
        results.append(
            {
                "name": runtime,
                "available": bool(available_exec),
                "executable": available_exec,
            }
        )
    return results


def _runtime_exec_candidates(runtime: str) -> list[str]:
    """Map one runtime family to concrete executables on the host."""
    mapping = {
        "node": ["node"],
        "npm": ["npm"],
        "npx": ["npx"],
        "python": ["python3", "python"],
        "pip": ["pip3", "pip"],
        "uv": ["uv", "uvx"],
        "uvx": ["uvx", "uv"],
        "docker": ["docker"],
    }
    return mapping.get(runtime, [runtime])


def _describe_next_mcp_action(analysis: dict[str, Any]) -> str:
    """Give the GUI a simple next-step message for the install preview."""
    missing_runtimes = [str(item) for item in analysis.get("missing_runtimes", [])]
    if missing_runtimes:
        return "Install or expose these runtimes in the container first: " + ", ".join(missing_runtimes)
    required_env = [str(item) for item in analysis.get("required_env", [])]
    if required_env:
        return "Install first, then enter the required secrets, run the test, and enable the MCP for chat."
    return "Install the MCP, verify the runtime test, then enable it for chat."


def _describe_repair_next_step(*, missing_runtimes: list[str], missing_env: list[str]) -> str:
    """Return the next operator hint for MCP repair mode."""
    if missing_runtimes:
        return "Apply a supported repair for the missing runtimes, then run the MCP test again."
    if missing_env:
        return "Fill in the missing secrets first, then run the MCP test again."
    return "Run the MCP test again. If it still fails, review the MCP logs and startup command."


def _summarize_exception(exc: BaseException) -> str:
    """Extract the most useful leaf message from nested async exception groups."""
    leaves: list[str] = []

    def walk(current: BaseException) -> None:
        sub_exceptions = getattr(current, "exceptions", None)
        if sub_exceptions:
            for sub in sub_exceptions:
                if isinstance(sub, BaseException):
                    walk(sub)
            return

        message = str(current).strip()
        if message and message not in leaves:
            leaves.append(message)
        cause = getattr(current, "__cause__", None)
        context = getattr(current, "__context__", None)
        if isinstance(cause, BaseException):
            walk(cause)
        if isinstance(context, BaseException) and context is not cause:
            walk(context)

    walk(exc)
    return leaves[0] if leaves else (str(exc).strip() or exc.__class__.__name__)


def _normalize_ai_plan(
    payload: dict[str, Any],
    *,
    repo: dict[str, str],
    deterministic: dict[str, Any],
) -> dict[str, Any]:
    """Validate and normalize one AI-generated MCP install plan."""
    install_mode = str(payload.get("install_mode", "")).strip()
    if install_mode not in {"source", "npm", "workspace_package", "remote", "oci"}:
        raise ValueError("AI fallback returned an invalid install_mode.")

    transport = str(payload.get("transport", "")).strip() or "stdio"
    if transport not in {"stdio", "sse", "streamableHttp"}:
        raise ValueError("AI fallback returned an invalid transport.")

    repo_type = str(payload.get("repo_type", "")).strip() or str(deterministic.get("repo_type", "")).strip() or "unknown"
    if repo_type not in {"npm", "python", "docker", "remote", "monorepo", "server_json", "unknown"}:
        repo_type = "unknown"

    run_command = str(payload.get("run_command", "")).strip()
    if run_command and run_command not in {"npx", "node", "python", "python3", "uv", "uvx", "docker"}:
        raise ValueError("AI fallback returned an unsupported run command.")

    run_url = str(payload.get("run_url", "")).strip()
    run_args = [str(item).strip() for item in payload.get("run_args", []) if str(item).strip()]
    install_steps = _normalize_ai_install_steps(payload.get("install_steps", []))
    required_env = _normalize_env_names(payload.get("required_env", []))
    optional_env = [
        item for item in _normalize_env_names(payload.get("optional_env", []))
        if item not in required_env
    ]

    if transport in {"sse", "streamableHttp"} and not run_url:
        raise ValueError("AI fallback selected a remote transport without a URL.")
    if transport == "stdio" and not run_command:
        raise ValueError("AI fallback selected stdio without a run command.")

    evidence = [str(item).strip() for item in payload.get("evidence", []) if str(item).strip()]
    evidence.append("analysis:ai_fallback")
    if deterministic.get("evidence"):
        evidence.extend(
            str(item).strip()
            for item in deterministic.get("evidence", [])
            if str(item).strip() and str(item).strip() not in evidence
        )

    confidence_raw = payload.get("confidence", deterministic.get("analysis_confidence", 0.0))
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        confidence = 0.35
    confidence = round(max(0.0, min(confidence, 0.99)), 2)

    server_name = str(payload.get("server_name", "")).strip() or str(
        deterministic.get("server_name", "")
    ).strip() or _slug_server_name(repo["repo"])
    summary = str(payload.get("summary", "")).strip() or str(deterministic.get("summary", "")).strip() or "No summary available."

    return {
        "server_name": server_name,
        "title": f"{repo['owner']}/{repo['repo']}",
        "summary": summary,
        "repo_url": repo["repo_url"],
        "clone_url": repo["clone_url"],
        "install_slug": f"{repo['owner']}__{repo['repo']}".lower(),
        "install_mode": install_mode,
        "transport": transport,
        "run_command": run_command,
        "run_args": run_args,
        "run_url": run_url,
        "install_steps": install_steps,
        "required_env": required_env,
        "optional_env": optional_env,
        "healthcheck": "Start the MCP transport and list tools through an MCP client handshake.",
        "evidence": evidence,
        "repo_type": repo_type,
        "analysis_mode": "ai_fallback",
        "analysis_confidence": confidence,
    }


def _normalize_ai_repair_plan(
    payload: dict[str, Any],
    *,
    deterministic: dict[str, Any],
    allow_unrestricted: bool,
) -> dict[str, Any]:
    """Validate and normalize one AI-generated MCP repair plan."""
    recipe = str(payload.get("recommended_recipe", "")).strip()
    if recipe and recipe not in REPAIR_RECIPE_DETAILS:
        raise ValueError("The AI repair planner returned an unsupported recipe.")
    if recipe == "unrestricted_agent_shell" and not allow_unrestricted:
        raise ValueError("Unrestricted Agent + Shell mode is disabled.")

    shell_command = str(payload.get("shell_command", "")).strip() if recipe == "unrestricted_agent_shell" else ""
    required_env = _normalize_env_names(payload.get("required_env", [])) if isinstance(payload.get("required_env"), list) else list(deterministic.get("required_env", []))
    confidence_raw = payload.get("confidence", deterministic.get("confidence", 0.0))
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        confidence = 0.35
    confidence = round(max(0.0, min(confidence, 0.99)), 2)

    available_recipes = [str(item) for item in deterministic.get("available_recipes", []) if str(item).strip()]
    if recipe and recipe not in available_recipes:
        available_recipes.append(recipe)

    return {
        **deterministic,
        "missing_runtime": str(payload.get("missing_runtime", deterministic.get("missing_runtime", ""))).strip(),
        "required_env": required_env,
        "recommended_recipe": recipe,
        "available_recipes": available_recipes,
        "next_step": str(payload.get("next_step", deterministic.get("next_step", ""))).strip()
        or str(deterministic.get("next_step", "")),
        "confidence": confidence,
        "shell_command": shell_command,
        "source": "ai_fallback",
        "supported": bool(recipe),
    }


def _normalize_ai_install_steps(steps: Any) -> list[dict[str, Any]]:
    """Limit AI-generated install steps to a safe allowlist."""
    if not isinstance(steps, list):
        return []

    normalized: list[dict[str, Any]] = []
    for item in steps:
        command: list[str] = []
        display = ""
        timeout = 900
        if isinstance(item, dict):
            raw_command = item.get("command")
            if isinstance(raw_command, list):
                command = [str(part).strip() for part in raw_command if str(part).strip()]
            display = str(item.get("display", "")).strip()
            try:
                timeout = int(item.get("timeout", 900) or 900)
            except (TypeError, ValueError):
                timeout = 900
        elif isinstance(item, str):
            display = item.strip()
            command = _command_from_known_display(display)

        if not command and display:
            command = _command_from_known_display(display)
        if not command or not _is_allowed_install_command(command):
            raise ValueError("AI fallback proposed an unsupported install command.")
        normalized.append(
            {
                "display": display or " ".join(command),
                "command": command,
                "timeout": max(30, min(timeout, 1800)),
            }
        )
    return normalized


def _command_from_known_display(display: str) -> list[str]:
    """Translate well-known install display strings back into safe commands."""
    mapping = {
        "npm ci": ["npm", "ci"],
        "npm install": ["npm", "install"],
        "npm run build": ["npm", "run", "build"],
        "uv pip install -e .": ["uv", "pip", "install", "-e", "."],
        "uv sync": ["uv", "sync"],
        "pip install -e .": ["pip", "install", "-e", "."],
        "python -m pip install -e .": ["python", "-m", "pip", "install", "-e", "."],
        "python3 -m pip install -e .": ["python3", "-m", "pip", "install", "-e", "."],
    }
    return list(mapping.get(display.strip(), []))


def _is_allowed_install_command(command: list[str]) -> bool:
    """Return whether one proposed install command is in the safe allowlist."""
    allowlist = {
        ("npm", "ci"),
        ("npm", "install"),
        ("npm", "run", "build"),
        ("uv", "pip", "install", "-e", "."),
        ("uv", "sync"),
        ("pip", "install", "-e", "."),
        ("python", "-m", "pip", "install", "-e", "."),
        ("python3", "-m", "pip", "install", "-e", "."),
    }
    return tuple(command) in allowlist


def _normalize_env_names(items: Any) -> list[str]:
    """Normalize and validate environment variable names from repository or AI output."""
    if not isinstance(items, list):
        return []
    normalized: list[str] = []
    for item in items:
        value = str(item).strip()
        if not value:
            continue
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", value):
            continue
        if value not in normalized:
            normalized.append(value)
    return normalized


def _slug_server_name(value: str) -> str:
    """Normalize a repo name into a conservative server id."""
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip().lower()).strip("-")
    return slug or "mcp-server"


def _build_oci_runtime_args(package: dict[str, Any]) -> list[str]:
    """Translate manifest runtimeArguments into docker run arguments."""
    args: list[str] = []
    for item in package.get("runtimeArguments", []):
        if not isinstance(item, dict):
            continue
        arg_name = str(item.get("name", "")).strip()
        value = str(item.get("value", "")).strip()
        if not arg_name:
            continue
        args.append(arg_name)
        if value:
            normalized = re.sub(r"\{[^}]+\}", "", value).strip()
            normalized = normalized.rstrip("=")
            if normalized:
                args.append(normalized)
    return [arg for arg in args if arg]


def _collect_env_requirements(
    checkout_dir: Path,
    example_config: dict[str, Any],
    server_manifest: dict[str, Any],
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

    packages = server_manifest.get("packages")
    if isinstance(packages, list):
        for package in packages:
            if not isinstance(package, dict):
                continue
            for env_var in package.get("environmentVariables", []) or []:
                if not isinstance(env_var, dict):
                    continue
                name = str(env_var.get("name", "")).strip()
                if not name:
                    continue
                if env_var.get("isRequired", False):
                    if name not in required:
                        required.append(name)
                elif name not in required and name not in optional:
                    optional.append(name)

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
    server_manifest: dict[str, Any],
    workspace_package: dict[str, str],
) -> str:
    """Pick a stable MCP server name."""
    preferred = str(example_config.get("server_name", "")).strip()
    if preferred:
        return _slugify(preferred)

    manifest_name = str(server_manifest.get("name", "")).strip()
    manifest_title = str(server_manifest.get("title", "")).strip()
    for candidate in (manifest_name.split("/")[-1] if manifest_name else "", manifest_title):
        if candidate:
            return _slugify(candidate)

    workspace_name = str(workspace_package.get("name", "")).strip()
    if workspace_name:
        return _slugify(workspace_name)

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
