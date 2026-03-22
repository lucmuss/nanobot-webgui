"""MCP repository analysis, install, and runtime probing for the GUI."""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import urlopen

from nanobot.config.schema import MCPServerConfig
from nanobot_webgui.config_service import GUIConfigService
from nanobot_webgui.repair_worker import REPAIR_RECIPE_DETAILS, supported_repair_recipes


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
        normalized_repo_url = _normalize_repo_url(str(analysis.get("repo_url", "")))

        version_mismatches = [
            item
            for item in analysis.get("runtime_status", [])
            if (
                isinstance(item, dict)
                and str(item.get("reason", "")) == "version_mismatch"
                and not bool(item.get("provisionable", False))
            )
        ]
        if version_mismatches:
            raise ValueError(_describe_runtime_version_mismatches(version_mismatches))
        missing_runtimes = [str(item) for item in analysis.get("missing_runtimes", [])]
        if missing_runtimes:
            raise ValueError(
                "Missing required runtime tools for this MCP: " + ", ".join(missing_runtimes)
            )

        config = self.config_service.load()
        existing = config.tools.mcp_servers.get(server_name)
        existing_record = self.config_service.get_mcp_record(server_name)
        if (
            existing
            and existing_record
            and _normalize_repo_url(str(existing_record.get("repo_url", ""))) == normalized_repo_url
        ):
            raise ValueError(
                f"MCP server already installed as '{server_name}'. "
                "Open the existing MCP entry instead of reinstalling it."
            )
        duplicate_server_name = self._find_duplicate_repo_server_name(
            normalized_repo_url,
            current_server_name=server_name,
        )
        if duplicate_server_name:
            raise ValueError(
                f"MCP server already installed from this repository: '{duplicate_server_name}'. "
                "Open the existing MCP entry instead of installing it again."
            )

        runtime_bindings = await self._prepare_runtime_bindings(analysis, existing_record)
        install_dir, install_logs = await self._execute_install_plan(analysis, runtime_bindings)
        provisional = self._register_install_plan(
            analysis=analysis,
            install_dir=install_dir,
            install_logs=install_logs,
            runtime_bindings=runtime_bindings,
            existing=existing,
            existing_record=existing_record,
            normalized_repo_url=normalized_repo_url,
        )
        self.config_service.set_mcp_record(server_name, provisional)

        record = await self.test_server(server_name)
        record.update(provisional)
        if self._should_attempt_source_checkout_fallback(analysis, record):
            record = await self._fallback_npm_probe_to_source_checkout(
                analysis=analysis,
                existing=existing,
                existing_record=existing_record,
                normalized_repo_url=normalized_repo_url,
                failed_record=record,
            )
        if not existing and record.get("status") == "active" and not record.get("enabled"):
            self.config_service.set_mcp_enabled(server_name, True)
            record["enabled"] = True
            record["auto_enabled"] = True
            record["log_tail"] = _append_log(
                str(record.get("log_tail", "")).strip(),
                "Auto-enabled for chat after a successful first install test.",
            )
        self.config_service.set_mcp_record(server_name, record)
        self.logger.info("mcp_installed server=%s source=%s", server_name, analysis["repo_url"])
        return record

    async def _execute_install_plan(
        self,
        analysis: dict[str, Any],
        runtime_bindings: dict[str, Any],
    ) -> tuple[Path | None, list[str]]:
        """Execute one install plan and return the managed checkout path plus operator logs."""
        install_logs: list[str] = []
        install_mode = str(analysis.get("install_mode", "source")).strip() or "source"
        install_dir: Path | None = None
        runtime_env = self._runtime_step_env(runtime_bindings)
        if "node" in runtime_bindings:
            node_binding = runtime_bindings["node"]
            install_logs.append(
                "Prepared local Node runtime "
                f"{node_binding.get('resolved_version', '').strip() or node_binding.get('version', '').strip() or 'unknown'} "
                f"for this MCP ({node_binding.get('constraint', '').strip() or 'repo constraint'})."
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
                command = self._resolve_install_step_command(step["command"], runtime_bindings)
                output, error = await self._run_command(
                    command,
                    cwd=install_dir,
                    timeout=step["timeout"],
                    env=runtime_env,
                )
                tail = "\n".join((output or error or "(no output)").splitlines()[-12:])
                install_logs.append(f"$ {' '.join(command)}\n{tail}")
        else:
            for step in analysis["install_steps"]:
                install_logs.append(f"$ {step['display']}\nRegistered without a managed checkout.")
        return install_dir, install_logs

    def _register_install_plan(
        self,
        *,
        analysis: dict[str, Any],
        install_dir: Path | None,
        install_logs: list[str],
        runtime_bindings: dict[str, Any],
        existing: MCPServerConfig | None,
        existing_record: dict[str, Any],
        normalized_repo_url: str,
    ) -> dict[str, Any]:
        """Persist one install plan into config and return its provisional GUI record."""
        config = self.config_service.load()
        env_default_hints = _guess_env_default_hints(
            config=config,
            server_name=analysis["server_name"],
            required_env=analysis["required_env"],
            optional_env=analysis["optional_env"],
            env_requirements=analysis.get("env_requirements", []),
            workspace=self.config_service.default_workspace,
        )
        env_requirements = _merge_env_requirement_default_hints(
            analysis.get("env_requirements", []),
            env_default_hints,
        )
        server_cfg = self._build_server_config(
            analysis,
            install_dir,
            existing,
            config,
            env_default_hints=env_default_hints,
            runtime_bindings=runtime_bindings,
        )
        config.tools.mcp_servers[analysis["server_name"]] = server_cfg
        self.config_service.save(config)

        resolved_runtime_status = _check_runtime_requirements(
            [str(item) for item in analysis.get("required_runtimes", []) if str(item).strip()],
            analysis.get("runtime_constraints", {}),
            runtime_bindings=runtime_bindings,
        )
        resolved_missing_runtimes = [
            item["name"]
            for item in resolved_runtime_status
            if not item["available"] and not bool(item.get("provisionable", False))
        ]
        resolved_analysis = {
            **analysis,
            "runtime_status": resolved_runtime_status,
            "missing_runtimes": resolved_missing_runtimes,
            "can_install": not resolved_missing_runtimes,
        }
        return {
            "server_name": analysis["server_name"],
            "title": analysis["title"],
            "summary": analysis["summary"],
            "repo_url": analysis["repo_url"],
            "normalized_repo_url": normalized_repo_url,
            "clone_url": analysis["clone_url"],
            "install_dir": str(install_dir) if install_dir is not None else "",
            "install_steps": [step["display"] for step in analysis["install_steps"]],
            "env_requirements": env_requirements,
            "required_env": analysis["required_env"],
            "optional_env": analysis["optional_env"],
            "healthcheck": analysis["healthcheck"],
            "evidence": analysis["evidence"],
            "repo_type": analysis.get("repo_type", ""),
            "analysis_mode": analysis.get("analysis_mode", "deterministic"),
            "analysis_confidence": analysis.get("analysis_confidence", 0.0),
            "required_runtimes": analysis.get("required_runtimes", []),
            "runtime_constraints": analysis.get("runtime_constraints", {}),
            "runtime_bindings": runtime_bindings,
            "runtime_status": resolved_runtime_status,
            "missing_runtimes": resolved_missing_runtimes,
            "can_install": not resolved_missing_runtimes,
            "next_action": _describe_next_mcp_action(resolved_analysis),
            "last_installed_at": _utc_now(),
            "enabled": bool(existing_record.get("enabled", False)),
            "auto_enabled": False,
            "log_tail": "\n\n".join(install_logs)[-4000:],
        }

    def _should_attempt_source_checkout_fallback(
        self,
        analysis: dict[str, Any],
        record: dict[str, Any],
    ) -> bool:
        """Return whether a failed npm package install should retry via source checkout."""
        if str(analysis.get("install_mode", "")).strip() != "npm":
            return False
        if str(record.get("status", "")).strip() != "error":
            return False
        return _looks_like_npm_package_resolution_failure(str(record.get("last_error", "")))

    async def _fallback_npm_probe_to_source_checkout(
        self,
        *,
        analysis: dict[str, Any],
        existing: MCPServerConfig | None,
        existing_record: dict[str, Any],
        normalized_repo_url: str,
        failed_record: dict[str, Any],
    ) -> dict[str, Any]:
        """Retry a broken npm package MCP via repo checkout plus local npm install/build."""
        repo = _parse_repository_source(str(analysis.get("repo_url", "")))
        install_dir = self.config_service.mcp_installs_dir / analysis["install_slug"]
        fallback_logs = [
            "Automatic fallback activated because the published npm runtime failed with a module-resolution error.",
            str(failed_record.get("last_error", "")).strip(),
        ]
        if install_dir.exists() and not (install_dir / ".git").exists():
            shutil.rmtree(install_dir, ignore_errors=True)

        source_analysis: dict[str, Any] | None = None
        try:
            if install_dir.exists():
                await self._update_checkout(install_dir)
                fallback_logs.append("$ git pull --ff-only\nUpdated existing checkout for source fallback.")
            else:
                install_dir.parent.mkdir(parents=True, exist_ok=True)
                await self._clone_repository(analysis["clone_url"], target_dir=install_dir)
                fallback_logs.append(
                    f"$ git clone --depth 1 {analysis['clone_url']} {install_dir}\nCloned checkout for source fallback."
                )

            source_analysis = self._inspect_checkout(install_dir, repo, prefer_source_checkout=True)
            source_analysis["server_name"] = analysis["server_name"]
            source_analysis["title"] = analysis["title"]
            source_analysis["summary"] = str(source_analysis.get("summary", "")).strip() or analysis["summary"]
            source_analysis["repo_url"] = analysis["repo_url"]
            source_analysis["clone_url"] = analysis["clone_url"]
            source_analysis["install_slug"] = analysis["install_slug"]
            source_analysis["analysis_mode"] = "deterministic_source_fallback"
            source_analysis["analysis_confidence"] = max(
                float(source_analysis.get("analysis_confidence", 0.0) or 0.0),
                float(analysis.get("analysis_confidence", 0.0) or 0.0),
            )
            source_analysis["evidence"] = [
                *[str(item) for item in source_analysis.get("evidence", []) if str(item).strip()],
                "fallback:source_checkout_after_npm_runtime_error",
            ]
            source_analysis = self._enrich_analysis(source_analysis)

            version_mismatches = [
                item
                for item in source_analysis.get("runtime_status", [])
                if (
                    isinstance(item, dict)
                    and str(item.get("reason", "")) == "version_mismatch"
                    and not bool(item.get("provisionable", False))
                )
            ]
            if version_mismatches:
                raise ValueError(_describe_runtime_version_mismatches(version_mismatches))

            fallback_bindings = await self._prepare_runtime_bindings(source_analysis, failed_record)
            executed_install_dir, executed_logs = await self._execute_install_plan(source_analysis, fallback_bindings)
            fallback_logs.extend(executed_logs)
            provisional = self._register_install_plan(
                analysis=source_analysis,
                install_dir=executed_install_dir,
                install_logs=fallback_logs,
                runtime_bindings=fallback_bindings,
                existing=existing,
                existing_record=failed_record,
                normalized_repo_url=normalized_repo_url,
            )
            self.config_service.set_mcp_record(source_analysis["server_name"], provisional)
            record = await self.test_server(source_analysis["server_name"])
            record.update(provisional)
            record["log_tail"] = _append_log(
                str(record.get("log_tail", "")).strip(),
                "Automatic source-checkout fallback was applied after the npm package runtime failed.",
            )
            self.config_service.set_mcp_record(source_analysis["server_name"], record)
            return record
        except Exception as exc:
            message = f"Automatic source-checkout fallback failed: {_summarize_exception(exc)}"
            failed_record["log_tail"] = _append_log(
                str(failed_record.get("log_tail", "")).strip(),
                "\n\n".join([*fallback_logs, message]),
            )
            self.config_service.set_mcp_record(analysis["server_name"], failed_record)
            return failed_record

    async def test_server(self, server_name: str) -> dict[str, Any]:
        """Probe one registered MCP server and persist its status."""
        config = self.config_service.load()
        cfg = config.tools.mcp_servers.get(server_name)
        if cfg is None:
            raise ValueError(f"MCP server '{server_name}' is not registered.")

        existing = self.config_service.get_mcp_record(server_name)
        env_requirements = _merge_env_requirements(
            _normalize_env_requirements(
                existing.get("env_requirements", []),
                fallback_required=existing.get("required_env", []),
                fallback_optional=existing.get("optional_env", []),
            ),
            _collect_env_requirements_from_install_dir(existing.get("install_dir", "")),
        )
        required_env, optional_env = _split_env_requirements(env_requirements)
        missing_env = _missing_env_vars(required_env, cfg.env)
        result: dict[str, Any] = {
            **existing,
            "server_name": server_name,
            "transport": _resolve_transport(cfg),
            "command": cfg.command,
            "args": list(cfg.args),
            "url": cfg.url,
            "tool_timeout": cfg.tool_timeout,
            "env_requirements": env_requirements,
            "required_env": required_env,
            "optional_env": optional_env,
            "missing_env": missing_env,
            "tool_names": [],
            "last_checked_at": _utc_now(),
            "enabled": bool(existing.get("enabled", False)),
            "friendly_error": {},
            "log_tail": str(existing.get("log_tail", "")).strip(),
            "last_test_checks": [],
        }

        if missing_env:
            result["last_test_checks"] = [
                {"label": "Secrets provided", "ok": False, "detail": ", ".join(missing_env)},
                {"label": "Startup preflight", "ok": False, "detail": "Blocked until required secrets are configured."},
                {"label": "Connection established", "ok": False, "detail": "Waiting for a valid MCP launch."},
                {"label": "Tool discovery", "ok": False, "detail": "No tools can be listed before the server starts."},
            ]
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
            result = _merge_runtime_error_env_requirements(result, cfg.env, preflight)
            missing_env = [str(item) for item in result.get("missing_env", []) if str(item).strip()]
            if missing_env:
                result["last_test_checks"] = [
                    {"label": "Secrets provided", "ok": False, "detail": ", ".join(missing_env)},
                    {"label": "Startup preflight", "ok": False, "detail": preflight},
                    {"label": "Connection established", "ok": False, "detail": "The MCP process exited before the handshake completed."},
                    {"label": "Tool discovery", "ok": False, "detail": "Tool listing was skipped because startup failed."},
                ]
                result["status"] = "needs_configuration"
                result["status_label"] = "Needs configuration"
                result["last_test_status"] = result["status"]
                result["last_test_label"] = result["status_label"]
                result["last_error"] = "Missing required environment variables: " + ", ".join(missing_env)
                result["log_tail"] = _append_log(result["log_tail"], preflight)
                result["log_tail"] = _append_log(result["log_tail"], result["last_error"])
                self.config_service.set_mcp_record(server_name, result)
                return result
            result["last_test_checks"] = [
                {"label": "Secrets provided", "ok": True, "detail": "Required env vars are present."},
                {"label": "Startup preflight", "ok": False, "detail": preflight},
                {"label": "Connection established", "ok": False, "detail": "The MCP process exited before the handshake completed."},
                {"label": "Tool discovery", "ok": False, "detail": "Tool listing was skipped because startup failed."},
            ]
            result["status"] = "error"
            result["status_label"] = "Probe failed"
            result["last_test_status"] = result["status"]
            result["last_test_label"] = result["status_label"]
            result["last_error"] = preflight
            result["log_tail"] = _append_log(result["log_tail"], preflight)
            fallback = await self._maybe_retry_npm_runtime_via_source_checkout(server_name, cfg, result)
            if fallback is not None:
                return fallback
            self.config_service.set_mcp_record(server_name, result)
            return result

        tool_names: list[str] | None = None
        try:
            tool_names = await self._list_server_tools(cfg)
        except Exception as exc:
            message = _summarize_exception(exc)
            message = await self._diagnose_probe_failure(cfg, message)
            stdio_retry = await self._maybe_retry_npx_runtime_with_stdio_flag(server_name, cfg, message)
            if stdio_retry is not None:
                cfg, retry_tools, retry_message = stdio_retry
                result["command"] = cfg.command
                result["args"] = list(cfg.args)
                if retry_tools is not None:
                    tool_names = retry_tools
                else:
                    message = retry_message
            if tool_names is None:
                result = _merge_runtime_error_env_requirements(result, cfg.env, message)
                missing_env = [str(item) for item in result.get("missing_env", []) if str(item).strip()]
                if missing_env:
                    result["last_test_checks"] = [
                        {"label": "Secrets provided", "ok": False, "detail": ", ".join(missing_env)},
                        {"label": "Startup preflight", "ok": True, "detail": "The MCP process started."},
                        {"label": "Connection established", "ok": False, "detail": message},
                        {"label": "Tool discovery", "ok": False, "detail": "The MCP handshake failed before tools could be listed."},
                    ]
                    result["status"] = "needs_configuration"
                    result["status_label"] = "Needs configuration"
                    result["last_test_status"] = result["status"]
                    result["last_test_label"] = result["status_label"]
                    result["last_error"] = "Missing required environment variables: " + ", ".join(missing_env)
                    result["log_tail"] = _append_log(result["log_tail"], message)
                    result["log_tail"] = _append_log(result["log_tail"], result["last_error"])
                    self.config_service.set_mcp_record(server_name, result)
                    self.logger.warning("mcp_probe_failed server=%s error=%s", server_name, message)
                    return result
                result["last_test_checks"] = [
                    {"label": "Secrets provided", "ok": True, "detail": "Required env vars are present."},
                    {"label": "Startup preflight", "ok": True, "detail": "The MCP process started."},
                    {"label": "Connection established", "ok": False, "detail": message},
                    {"label": "Tool discovery", "ok": False, "detail": "The MCP handshake failed before tools could be listed."},
                ]
                result["status"] = "error"
                result["status_label"] = "Probe failed"
                result["last_test_status"] = result["status"]
                result["last_test_label"] = result["status_label"]
                result["last_error"] = message
                result["log_tail"] = _append_log(result["log_tail"], message)
                fallback = await self._maybe_retry_npm_runtime_via_source_checkout(server_name, cfg, result)
                if fallback is not None:
                    return fallback
                self.config_service.set_mcp_record(server_name, result)
                self.logger.warning("mcp_probe_failed server=%s error=%s", server_name, message)
                return result

        result["tool_names"] = tool_names or []
        result["last_error"] = ""
        result["status"] = "active"
        result["status_label"] = "Active"
        result["last_test_status"] = result["status"]
        result["last_test_label"] = result["status_label"]
        result["last_test_checks"] = [
            {"label": "Secrets provided", "ok": True, "detail": "Required env vars are present."},
            {"label": "Startup preflight", "ok": True, "detail": "The MCP process started successfully."},
            {"label": "Connection established", "ok": True, "detail": f"Transport {result['transport']} responded successfully."},
            {
                "label": "Tool discovery",
                "ok": True,
                "detail": (
                    f"{len(tool_names)} tool(s) discovered: {', '.join(tool_names)}"
                    if tool_names
                    else "The MCP responded successfully but reported no tools."
                ),
            },
        ]
        result["log_tail"] = _append_log(
            result["log_tail"],
            "Connected successfully. Tools: " + (", ".join(tool_names) if tool_names else "(none)"),
        )
        self.config_service.set_mcp_record(server_name, result)
        self.logger.info("mcp_probe_ok server=%s tools=%s", server_name, len(tool_names))
        return result

    async def _diagnose_probe_failure(self, cfg: MCPServerConfig, message: str) -> str:
        """Replace generic stdio handshake errors with a more actionable startup failure when possible."""
        if _resolve_transport(cfg) != "stdio":
            return message
        if not _looks_like_generic_stdio_failure(message):
            return message
        detail = await self._preflight_server(cfg, settle_seconds=8)
        return detail or message

    def _persist_server_config(self, server_name: str, cfg: MCPServerConfig) -> None:
        """Persist one updated MCP server config back to Nanobot state."""
        config = self.config_service.load()
        config.tools.mcp_servers[server_name] = cfg
        self.config_service.save(config)

    async def _maybe_retry_npx_runtime_with_stdio_flag(
        self,
        server_name: str,
        cfg: MCPServerConfig,
        message: str,
    ) -> tuple[MCPServerConfig, list[str] | None, str] | None:
        """Retry a generic npx stdio failure with an explicit --stdio flag when the repo likely expects it."""
        if _resolve_transport(cfg) != "stdio":
            return None
        if not _looks_like_generic_stdio_failure(message):
            return None
        if not _looks_like_npx_package_runtime(cfg):
            return None
        if _args_include_stdio_flag(cfg.args):
            return None

        retry_cfg = _clone_server_config_with_args(cfg, [*list(cfg.args or []), "--stdio"])
        try:
            tool_names = await self._list_server_tools(retry_cfg)
        except Exception as exc:
            retry_message = _summarize_exception(exc)
            retry_message = await self._diagnose_probe_failure(retry_cfg, retry_message)
            if _looks_like_generic_stdio_failure(retry_message):
                return None
            self._persist_server_config(server_name, retry_cfg)
            return retry_cfg, None, retry_message

        self._persist_server_config(server_name, retry_cfg)
        return retry_cfg, tool_names, ""

    async def _maybe_retry_npm_runtime_via_source_checkout(
        self,
        server_name: str,
        cfg: MCPServerConfig,
        failed_record: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Convert a broken npm package runtime into a managed source checkout on retest."""
        analysis = self._analysis_from_failed_runtime_record(server_name, cfg, failed_record)
        if analysis is None:
            return None
        return await self._fallback_npm_probe_to_source_checkout(
            analysis=analysis,
            existing=cfg,
            existing_record=failed_record,
            normalized_repo_url=_normalize_repo_url(str(failed_record.get("repo_url", ""))),
            failed_record=failed_record,
        )

    def _analysis_from_failed_runtime_record(
        self,
        server_name: str,
        cfg: MCPServerConfig,
        failed_record: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Rebuild the minimum analysis payload needed for source fallback from one stored record."""
        if not _looks_like_npm_package_resolution_failure(str(failed_record.get("last_error", ""))):
            return None

        transport = _resolve_transport(cfg)
        if transport != "stdio":
            return None

        install_steps = [str(item) for item in failed_record.get("install_steps", []) if str(item).strip()]
        uses_npm_package_runtime = (
            cfg.command == "npx"
            or any("npx-cli.js" in str(arg) for arg in cfg.args)
            or any("Register npm package runtime via npx" in item for item in install_steps)
        )
        if not uses_npm_package_runtime:
            return None

        repo_url = _normalize_repo_url(str(failed_record.get("repo_url", "")))
        if not repo_url:
            return None
        try:
            repo = _parse_repository_source(repo_url)
        except ValueError:
            return None

        clone_url = str(failed_record.get("clone_url", "")).strip() or repo["clone_url"]
        return {
            "server_name": server_name,
            "title": str(failed_record.get("title", "")).strip() or f"{repo['owner']}/{repo['repo']}",
            "summary": str(failed_record.get("summary", "")).strip() or "No summary available.",
            "repo_url": repo_url,
            "clone_url": clone_url,
            "install_slug": f"{repo['owner']}__{repo['repo']}".lower(),
            "install_mode": "npm",
            "transport": transport,
            "run_command": "npx",
            "run_args": list(cfg.args),
            "run_url": "",
            "install_steps": [{"command": [], "display": item, "timeout": 0} for item in install_steps],
            "env_requirements": list(failed_record.get("env_requirements", []))
            if isinstance(failed_record.get("env_requirements"), list)
            else [],
            "required_env": [str(item) for item in failed_record.get("required_env", [])],
            "optional_env": [str(item) for item in failed_record.get("optional_env", [])],
            "healthcheck": str(failed_record.get("healthcheck", "")).strip()
            or "Start the MCP transport and list tools through an MCP client handshake.",
            "evidence": [str(item) for item in failed_record.get("evidence", []) if str(item).strip()],
            "repo_type": str(failed_record.get("repo_type", "")).strip() or "server_json",
            "analysis_mode": str(failed_record.get("analysis_mode", "")).strip() or "deterministic",
            "analysis_confidence": float(failed_record.get("analysis_confidence", 0.0) or 0.0),
            "required_runtimes": [str(item) for item in failed_record.get("required_runtimes", []) if str(item).strip()],
            "runtime_constraints": dict(failed_record.get("runtime_constraints", {}) or {}),
            "runtime_status": list(failed_record.get("runtime_status", []))
            if isinstance(failed_record.get("runtime_status"), list)
            else [],
            "missing_runtimes": [str(item) for item in failed_record.get("missing_runtimes", []) if str(item).strip()],
            "can_install": bool(failed_record.get("can_install", True)),
            "next_action": str(failed_record.get("next_action", "")).strip(),
        }

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
        runtime_bindings = _normalize_runtime_bindings(record.get("runtime_bindings", {}))
        runtime_status = _check_runtime_requirements(
            required_runtimes,
            record.get("runtime_constraints", {}),
            runtime_bindings=runtime_bindings,
        )
        missing_runtimes = [
            item["name"]
            for item in runtime_status
            if not item["available"] and not bool(item.get("provisionable", False))
        ]
        refreshed = {
            **record,
            "runtime_status": runtime_status,
            "missing_runtimes": missing_runtimes,
            "runtime_constraints": dict(record.get("runtime_constraints", {}) or {}),
            "runtime_bindings": runtime_bindings,
            "can_install": not missing_runtimes,
        }
        updated = {
            **refreshed,
            "next_action": _describe_next_mcp_action(refreshed),
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
        env: dict[str, str] | None = None,
    ) -> tuple[str, str]:
        """Run one installation command and raise a concise error on failure."""
        merged_env = os.environ.copy()
        if env:
            merged_env.update({key: value for key, value in env.items() if value})
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            env=merged_env,
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

    def _inspect_checkout(
        self,
        checkout_dir: Path,
        repo: dict[str, str],
        *,
        prefer_source_checkout: bool = False,
    ) -> dict[str, Any]:
        """Build a best-effort install plan from the repository contents."""
        package_json = _read_json(checkout_dir / "package.json")
        pyproject = _read_text(checkout_dir / "pyproject.toml")
        pyproject_data = _read_toml(checkout_dir / "pyproject.toml")
        requirements_txt = _read_text(checkout_dir / "requirements.txt")
        server_manifest = _load_server_manifest(checkout_dir)
        workspace_package = _find_workspace_mcp_package(checkout_dir)
        readme_path = checkout_dir / "README.md"
        readme_text = _read_text(readme_path)
        readme_summary = _extract_readme_summary(readme_path)
        example_config = _load_mcp_example(checkout_dir)
        env_requirements = _collect_env_requirements(checkout_dir, example_config, server_manifest)
        required_env, optional_env = _split_env_requirements(env_requirements)

        install_steps: list[dict[str, Any]] = []
        run_command = ""
        run_args: list[str] = []
        transport = "stdio"
        run_url = ""
        evidence: list[str] = []
        install_mode = "source"

        if example_config:
            evidence.append(f"Example MCP config: {example_config['source_file']}")
            example_transport = str(example_config.get("transport", "stdio")).strip() or "stdio"
            transport = example_transport
            if _example_runtime_is_actionable(example_config):
                if example_transport == "stdio":
                    run_command = str(example_config.get("command", "")).strip()
                    run_args = [str(item).strip() for item in example_config.get("args", []) if str(item).strip()]
                elif example_transport in {"sse", "streamableHttp"}:
                    run_url = str(example_config.get("url", "")).strip()
            else:
                evidence.append("Example MCP config runtime ignored because it contains placeholder values.")

        manifest_choice = None if prefer_source_checkout else _select_server_manifest_install(server_manifest)
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
            if not run_command:
                run_command, run_args = _derive_python_entry(checkout_dir, pyproject_data)
            install_steps.extend(_derive_python_install_steps(pyproject_data, run_args))
            evidence.append("pyproject.toml")
        elif not run_command and not run_url and requirements_txt:
            install_steps.extend(
                [
                    {
                        "command": ["uv", "venv", ".venv"],
                        "display": "uv venv .venv",
                        "timeout": 900,
                    },
                    {
                        "command": ["uv", "pip", "install", "--python", ".venv/bin/python", "-r", "requirements.txt"],
                        "display": "uv pip install --python .venv/bin/python -r requirements.txt",
                        "timeout": 900,
                    },
                ]
            )
            evidence.append("requirements.txt")
            if (checkout_dir / "uv.lock").exists():
                evidence.append("uv.lock")
            script_entry = _derive_python_file_entry(checkout_dir)
            if script_entry:
                run_command = ".venv/bin/python"
                run_args = [f"./{script_entry}"]
        elif not run_command and not run_url:
            raise ValueError("Could not derive an install plan for this repository.")

        if _runtime_should_append_stdio_flag(
            transport=transport,
            run_command=run_command,
            run_args=run_args,
            readme_text=readme_text,
            package_json=package_json,
        ):
            run_args = [*run_args, "--stdio"]
            evidence.append("README/scripts document --stdio runtime flag")

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
            "env_requirements": env_requirements,
            "required_env": required_env,
            "optional_env": optional_env,
            "healthcheck": "Start the MCP transport and list tools through an MCP client handshake.",
            "evidence": evidence,
            "repo_type": _detect_repo_type(
                install_mode=install_mode,
                package_json=package_json,
                pyproject=pyproject,
                requirements_txt=requirements_txt,
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
                requirements_txt=requirements_txt,
                run_url=run_url,
            ),
            "runtime_constraints": _derive_runtime_constraints(
                install_mode=install_mode,
                run_command=run_command,
                package_json=package_json,
            ),
        }

    def _build_repository_bundle(self, checkout_dir: Path, repo: dict[str, str]) -> dict[str, Any]:
        """Collect bounded repository evidence for optional AI fallback planning."""
        workspace_packages = []
        for package_path in sorted(checkout_dir.rglob("package.json")):
            relative_path = package_path.relative_to(checkout_dir)
            if relative_path == Path("package.json") or "node_modules" in relative_path.parts:
                continue
            if len(relative_path.parts) > 4:
                continue
            workspace_packages.append(
                {
                    "path": str(relative_path),
                    "package_json": _read_json(package_path),
                }
            )
            if len(workspace_packages) >= 6:
                break

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
        runtime_status = _check_runtime_requirements(required_runtimes, enriched.get("runtime_constraints", {}))
        missing_runtimes = [
            item["name"]
            for item in runtime_status
            if not item["available"] and not bool(item.get("provisionable", False))
        ]
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
        *,
        env_default_hints: dict[str, dict[str, str]] | None = None,
        runtime_bindings: dict[str, Any] | None = None,
    ) -> MCPServerConfig:
        """Create the MCP config entry using the derived install plan."""
        default_hints = env_default_hints or _guess_env_default_hints(
            config=config,
            server_name=analysis["server_name"],
            required_env=analysis["required_env"],
            optional_env=analysis["optional_env"],
            env_requirements=analysis.get("env_requirements", []),
            workspace=self.config_service.default_workspace,
        )
        env_defaults = {name: item["value"] for name, item in default_hints.items() if str(item.get("value", "")).strip()}
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

        runtime_bindings = _normalize_runtime_bindings(runtime_bindings or {})
        if install_dir is None:
            command, args = _resolve_runtime_command_and_args(
                command=str(analysis["run_command"]),
                args=[str(arg) for arg in analysis["run_args"]],
                runtime_bindings=runtime_bindings,
            )
        else:
            expanded_command = _expand_install_path(str(analysis["run_command"]), install_dir)
            expanded_args = [_expand_install_path(str(arg), install_dir) for arg in analysis["run_args"]]
            command, args = _resolve_runtime_command_and_args(
                command=expanded_command,
                args=expanded_args,
                runtime_bindings=runtime_bindings,
            )
        env = _merge_runtime_env(env, runtime_bindings)
        return MCPServerConfig(
            type=analysis["transport"] or None,
            command=command,
            args=args,
            env=env,
            url="",
            headers=headers,
            tool_timeout=existing.tool_timeout if existing else 30,
        )

    async def _prepare_runtime_bindings(
        self,
        analysis: dict[str, Any],
        existing_record: dict[str, Any],
    ) -> dict[str, Any]:
        """Provision MCP-local runtimes when the repository declares strict engine constraints."""
        bindings = _normalize_runtime_bindings(existing_record.get("runtime_bindings", {}))
        node_constraint = _node_runtime_constraint(analysis.get("runtime_constraints", {}))
        if not _analysis_needs_local_node_runtime(analysis):
            return bindings

        current_binding = bindings.get("node", {})
        if _node_runtime_binding_satisfies(current_binding, node_constraint):
            return bindings

        node_binding = await asyncio.to_thread(
            _ensure_local_node_runtime,
            self.config_service.default_workspace / "mcp-runtimes" / "node",
            node_constraint,
        )
        bindings["node"] = node_binding
        return bindings

    def _resolve_install_step_command(
        self,
        command: list[str],
        runtime_bindings: dict[str, Any],
    ) -> list[str]:
        """Rewrite install commands to the MCP-local runtime when needed."""
        if not command:
            return []
        normalized = [str(part) for part in command]
        head = normalized[0]
        if head == "npm":
            prefix = _runtime_prefix_for("npm", runtime_bindings)
            if prefix:
                return [*prefix, *normalized[1:]]
        if head == "npx":
            prefix = _runtime_prefix_for("npx", runtime_bindings)
            if prefix:
                return [*prefix, *normalized[1:]]
        return normalized

    def _runtime_step_env(self, runtime_bindings: dict[str, Any]) -> dict[str, str]:
        """Return environment overrides for install steps that use local runtimes."""
        node_binding = runtime_bindings.get("node", {}) if isinstance(runtime_bindings, dict) else {}
        node_bin = str(node_binding.get("node_executable", "")).strip()
        if not node_bin:
            return {}
        node_bin_dir = str(Path(node_bin).parent)
        current_path = os.environ.get("PATH", "")
        return {
            "PATH": f"{node_bin_dir}{os.pathsep}{current_path}" if current_path else node_bin_dir,
        }

    async def _preflight_server(self, cfg: MCPServerConfig, *, settle_seconds: float = 2.0) -> str:
        """Run a short stdio preflight so hard startup failures surface with stderr."""
        if _resolve_transport(cfg) != "stdio" or not cfg.command:
            return ""

        env = os.environ.copy()
        env.update({key: value for key, value in (cfg.env or {}).items() if value})
        process = await asyncio.create_subprocess_exec(
            cfg.command,
            *cfg.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            await asyncio.sleep(settle_seconds)
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

    def _find_duplicate_repo_server_name(self, normalized_repo_url: str, *, current_server_name: str) -> str:
        """Return another installed server name using the same repo URL, if any."""
        if not normalized_repo_url:
            return ""
        config = self.config_service.load()
        for name in config.tools.mcp_servers.keys():
            if name == current_server_name:
                continue
            record = self.config_service.get_mcp_record(str(name))
            if _normalize_repo_url(str(record.get("repo_url", ""))) == normalized_repo_url:
                return str(name)
        return ""


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

    if raw.startswith("git@"):
        host_part, path = raw.split(":", 1)
        host = host_part.split("@", 1)[1]
        path_parts = [part for part in path.split("/") if part]
        if len(path_parts) < 2:
            raise ValueError("Enter a full git repository reference like git@host:owner/repo.git.")
        owner, repo = path_parts[0], path_parts[1].removesuffix(".git")
        clone_url = raw if raw.endswith(".git") else raw + ".git"
        repo_url = f"https://{host}/{owner}/{repo}"
        return {
            "owner": owner,
            "repo": repo,
            "repo_url": repo_url,
            "clone_url": clone_url,
        }

    parsed = urlparse(raw)
    host = parsed.netloc.lower()
    if host and host not in {"github.com", "www.github.com"} and not raw.endswith(".git"):
        raise ValueError("Only direct GitHub repository URLs are supported right now.")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        raise ValueError("Enter a full repository URL like https://github.com/owner/repo.")

    owner, repo = parts[0], parts[1].removesuffix(".git")
    normalized_repo_url = f"{parsed.scheme}://{parsed.netloc}/{owner}/{repo}"
    clone_url = raw
    if host in {"github.com", "www.github.com"}:
        clone_url = f"https://github.com/{owner}/{repo}.git"
        normalized_repo_url = f"https://github.com/{owner}/{repo}"
    elif not clone_url.endswith(".git"):
        clone_url = normalized_repo_url + ".git"
    return {
        "owner": owner,
        "repo": repo,
        "repo_url": normalized_repo_url,
        "clone_url": clone_url,
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


def _read_toml(path: Path) -> dict[str, Any]:
    """Read one TOML file if it exists and is valid."""
    if not path.exists():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError):
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
    text = html.unescape(raw.strip())
    if not text:
        return ""

    text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"!\[[^\]]*]\([^)]*\)", " ", text)
    text = re.sub(r"<img\b[^>]*>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"</?[^>]+>", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"`{1,3}([^`]+)`{1,3}", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"__([^_]+)__", r"\1", text)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"\1", text)
    text = re.sub(r"(?<!_)_([^_]+)_(?!_)", r"\1", text)
    text = re.sub(r"~~([^~]+)~~", r"\1", text)
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
    for package_path in sorted(checkout_dir.rglob("package.json")):
        relative_path = package_path.relative_to(checkout_dir)
        if relative_path == Path("package.json"):
            continue
        if "node_modules" in relative_path.parts:
            continue
        if len(relative_path.parts) > 4:
            continue
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
        if any(part in {"packages", "servers", "src"} for part in relative_path.parts[:-1]):
            score += 1
        if score <= 0 or not package_name:
            continue
        candidates.append(
            {
                "name": package_name,
                "version": str(package_json.get("version", "")).strip(),
                "path": str(relative_path),
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
    requirements_txt: str,
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
    if pyproject or requirements_txt:
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
    requirements_txt: str,
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
    if requirements_txt:
        score += 0.2
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


def _derive_runtime_constraints(
    *,
    install_mode: str,
    run_command: str,
    package_json: dict[str, Any],
) -> dict[str, str]:
    """Return runtime version constraints declared by the repository."""
    constraints: dict[str, str] = {}
    engines = package_json.get("engines") if isinstance(package_json, dict) else {}
    if not isinstance(engines, dict):
        return constraints

    node_constraint = str(engines.get("node", "")).strip()
    if node_constraint and (run_command in {"node", "npx"} or install_mode in {"npm", "workspace_package"}):
        constraints["node"] = node_constraint
    return constraints


def _check_runtime_requirements(
    required_runtimes: list[str],
    runtime_constraints: dict[str, str] | None = None,
    *,
    runtime_bindings: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Check whether the container host currently exposes the required runtimes."""
    constraints = runtime_constraints or {}
    bindings = _normalize_runtime_bindings(runtime_bindings or {})
    results: list[dict[str, Any]] = []
    for runtime in required_runtimes:
        invocation = _runtime_invocation(runtime, bindings)
        available_exec = ""
        if invocation:
            available_exec = " ".join(invocation)
            installed_version = _read_runtime_version(runtime, invocation)
        else:
            checks = _runtime_exec_candidates(runtime)
            available_exec = next((candidate for candidate in checks if shutil.which(candidate)), "")
            installed_version = _read_runtime_version(runtime, [available_exec]) if available_exec else ""
        required_version = _required_runtime_version(runtime, constraints)
        available = bool(available_exec)
        reason = ""
        provisionable = False
        if available and required_version and not _runtime_version_satisfies(installed_version, required_version):
            available = False
            reason = "version_mismatch"
            provisionable = _runtime_is_locally_provisionable(runtime, constraints)
        elif not available:
            reason = "missing"
            provisionable = _runtime_is_locally_provisionable(runtime, constraints)
        results.append(
            {
                "name": runtime,
                "available": available,
                "executable": available_exec,
                "version": installed_version,
                "required_version": required_version,
                "reason": reason,
                "provisionable": provisionable,
            }
        )
    return results


def _normalize_runtime_bindings(value: Any) -> dict[str, dict[str, str]]:
    """Normalize persisted runtime binding metadata into a predictable mapping."""
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, dict[str, str]] = {}
    for runtime_name, payload in value.items():
        if not isinstance(payload, dict):
            continue
        cleaned = {
            str(key): str(raw_value)
            for key, raw_value in payload.items()
            if str(raw_value).strip()
        }
        if cleaned:
            normalized[str(runtime_name)] = cleaned
    return normalized


def _node_runtime_constraint(runtime_constraints: Any) -> str:
    """Return the declared Node constraint when present."""
    if not isinstance(runtime_constraints, dict):
        return ""
    return str(runtime_constraints.get("node", "")).strip()


def _analysis_needs_local_node_runtime(analysis: dict[str, Any]) -> bool:
    """Return whether one analysis should get an MCP-local Node runtime."""
    constraint = _node_runtime_constraint(analysis.get("runtime_constraints", {}))
    if not constraint:
        return False
    required_runtimes = [str(item) for item in analysis.get("required_runtimes", []) if str(item).strip()]
    return any(runtime in {"node", "npm", "npx"} for runtime in required_runtimes)


def _required_runtime_version(runtime: str, constraints: dict[str, str]) -> str:
    """Map runtime families to the constraint that should validate them."""
    if runtime == "node":
        return str(constraints.get("node", "")).strip()
    return str(constraints.get(runtime, "")).strip()


def _runtime_is_locally_provisionable(runtime: str, constraints: dict[str, str]) -> bool:
    """Return whether Nanobot can provision this runtime locally for one MCP."""
    return runtime in {"node", "npm", "npx"} and bool(str(constraints.get("node", "")).strip())


def _runtime_invocation(runtime: str, runtime_bindings: dict[str, Any]) -> list[str]:
    """Return the bound runtime invocation when this MCP uses a local runtime."""
    return _runtime_prefix_for(runtime, runtime_bindings)


def _runtime_prefix_for(runtime: str, runtime_bindings: dict[str, Any]) -> list[str]:
    """Return the command prefix for a possibly bound runtime."""
    node_binding = runtime_bindings.get("node", {}) if isinstance(runtime_bindings, dict) else {}
    node_exec = str(node_binding.get("node_executable", "")).strip()
    if not node_exec:
        return []
    if runtime == "node":
        return [node_exec]
    if runtime == "npm":
        npm_cli = str(node_binding.get("npm_cli_path", "")).strip()
        if npm_cli:
            return [node_exec, npm_cli]
    if runtime == "npx":
        npx_cli = str(node_binding.get("npx_cli_path", "")).strip()
        if npx_cli:
            return [node_exec, npx_cli]
    return []


def _node_runtime_binding_satisfies(binding: Any, constraint: str) -> bool:
    """Return whether one persisted local Node binding is still usable."""
    if not isinstance(binding, dict):
        return False
    node_exec = str(binding.get("node_executable", "")).strip()
    npm_cli = str(binding.get("npm_cli_path", "")).strip()
    npx_cli = str(binding.get("npx_cli_path", "")).strip()
    resolved_version = str(binding.get("resolved_version", "")).strip()
    if not node_exec or not npm_cli or not npx_cli:
        return False
    if not Path(node_exec).exists() or not Path(npm_cli).exists() or not Path(npx_cli).exists():
        return False
    if constraint and not _runtime_version_satisfies(resolved_version, constraint):
        return False
    return True


def _resolve_runtime_command_and_args(
    *,
    command: str,
    args: list[str],
    runtime_bindings: dict[str, Any],
) -> tuple[str, list[str]]:
    """Replace global Node launchers with the MCP-local runtime when configured."""
    if command == "node":
        prefix = _runtime_prefix_for("node", runtime_bindings)
        if prefix:
            return prefix[0], list(args)
    if command == "npx":
        prefix = _runtime_prefix_for("npx", runtime_bindings)
        if prefix:
            return prefix[0], [*prefix[1:], *args]
    return command, list(args)


def _merge_runtime_env(env: dict[str, str], runtime_bindings: dict[str, Any]) -> dict[str, str]:
    """Inject runtime-specific PATH overrides so child processes resolve the bound Node."""
    merged = dict(env)
    node_binding = runtime_bindings.get("node", {}) if isinstance(runtime_bindings, dict) else {}
    node_exec = str(node_binding.get("node_executable", "")).strip()
    if not node_exec:
        return merged

    node_bin_dir = str(Path(node_exec).parent)
    existing_path = str(merged.get("PATH", "")).strip() or os.environ.get("PATH", "")
    path_parts = [part for part in existing_path.split(os.pathsep) if part]
    if node_bin_dir not in path_parts:
        path_parts.insert(0, node_bin_dir)
    merged["PATH"] = os.pathsep.join(path_parts) if path_parts else node_bin_dir
    return merged


def _read_runtime_version(runtime: str, executable: list[str]) -> str:
    """Read one runtime version string when the executable is present."""
    if not executable:
        return ""

    version_commands = {
        "node": [*executable, "--version"],
        "npm": [*executable, "--version"],
        "npx": [*executable, "--version"],
        "python": [*executable, "--version"],
        "pip": [*executable, "--version"],
        "uv": [*executable, "--version"],
        "uvx": [*executable, "--version"],
        "docker": [*executable, "--version"],
    }
    command = version_commands.get(runtime)
    if not command:
        return ""
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    output = (completed.stdout or completed.stderr or "").strip()
    match = re.search(r"v?(\d+(?:\.\d+){0,2})", output)
    return match.group(1) if match else ""


def _looks_like_generic_stdio_failure(message: str) -> bool:
    """Return whether one MCP handshake error is too generic and worth re-diagnosing."""
    normalized = str(message).strip().lower()
    return normalized in {
        "connection closed",
        "stream closed",
        "broken pipe",
        "eof",
    }


def _looks_like_npm_package_resolution_failure(message: str) -> bool:
    """Return whether one npm package runtime failed due to missing published modules."""
    normalized = str(message).strip()
    return (
        "ERR_MODULE_NOT_FOUND" in normalized
        or "Cannot find package" in normalized
        or "Cannot find module" in normalized
    )


def _looks_like_npx_package_runtime(cfg: MCPServerConfig) -> bool:
    """Return whether one MCP config executes an npm package through npx."""
    if str(cfg.command).strip() == "npx":
        return True
    return any("npx-cli.js" in str(arg) for arg in (cfg.args or []))


def _args_include_stdio_flag(args: list[str] | tuple[str, ...]) -> bool:
    """Return whether a command line already opts into stdio transport explicitly."""
    return any(str(arg).strip() == "--stdio" for arg in args)


def _clone_server_config_with_args(cfg: MCPServerConfig, args: list[str]) -> MCPServerConfig:
    """Copy one MCP config while replacing only its CLI arguments."""
    return MCPServerConfig(
        type=cfg.type,
        command=cfg.command,
        args=args,
        env=dict(cfg.env or {}),
        url=cfg.url,
        headers=dict(cfg.headers or {}),
        tool_timeout=cfg.tool_timeout,
        enabled_tools=list(cfg.enabled_tools or []),
    )


def _text_mentions_stdio_flag(text: str) -> bool:
    """Return whether documentation or scripts explicitly mention a --stdio flag."""
    return bool(re.search(r"(?:^|[^\w-])--stdio(?:$|[^\w-])", str(text or ""), flags=re.IGNORECASE))


def _runtime_should_append_stdio_flag(
    *,
    transport: str,
    run_command: str,
    run_args: list[str],
    readme_text: str,
    package_json: dict[str, Any],
) -> bool:
    """Return whether a derived Node/npm stdio runtime should include an explicit --stdio flag."""
    if transport != "stdio":
        return False
    if run_command not in {"npx", "node"}:
        return False
    if _args_include_stdio_flag(run_args):
        return False
    if _text_mentions_stdio_flag(readme_text):
        return True
    scripts = package_json.get("scripts") if isinstance(package_json, dict) else {}
    if isinstance(scripts, dict):
        for value in scripts.values():
            if _text_mentions_stdio_flag(str(value)):
                return True
    return False


def _runtime_version_satisfies(installed_version: str, constraint: str) -> bool:
    """Check simple semver-ish constraints like >=24 or >=24.0.0."""
    if not constraint:
        return True
    match = re.match(r"^\s*>=\s*v?(\d+(?:\.\d+){0,2})\s*$", constraint)
    if not match:
        return True
    required = _version_tuple(match.group(1))
    installed = _version_tuple(installed_version)
    if not installed:
        return False
    return installed >= required


def _version_tuple(value: str) -> tuple[int, ...]:
    """Convert dotted numeric versions into comparable tuples."""
    raw = str(value).strip().lstrip("v")
    if not raw:
        return ()
    parts: list[int] = []
    for piece in raw.split("."):
        if not piece.isdigit():
            break
        parts.append(int(piece))
    return tuple(parts)


def _ensure_local_node_runtime(runtime_root: Path, constraint: str) -> dict[str, str]:
    """Download and cache one Node runtime that satisfies the repository constraint."""
    if not constraint:
        raise ValueError("Cannot provision a local Node runtime without an engines.node constraint.")

    runtime_root.mkdir(parents=True, exist_ok=True)
    required = _version_tuple(_minimum_required_version(constraint))
    cached = _find_cached_node_runtime(runtime_root, required)
    if cached:
        target_dir, version = cached
        return _node_runtime_binding(target_dir, constraint, version)

    release = _select_node_release(constraint)
    archive_name = release["archive_name"]
    target_dir = runtime_root / release["dirname"]
    if not target_dir.exists():
        tmp_dir = runtime_root / f".tmp-{release['dirname']}"
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        archive_path = tmp_dir / archive_name
        with urlopen(release["download_url"], timeout=60) as response:
            archive_path.write_bytes(response.read())
        with tarfile.open(archive_path, mode="r:xz") as tar:
            tar.extractall(path=tmp_dir)
        extracted_dir = tmp_dir / release["dirname"]
        if not extracted_dir.exists():
            raise ValueError(f"Downloaded Node runtime is missing expected directory: {release['dirname']}")
        os.replace(extracted_dir, target_dir)
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return _node_runtime_binding(target_dir, constraint, release["version"])


def _find_cached_node_runtime(runtime_root: Path, required: tuple[int, ...]) -> tuple[Path, str] | None:
    """Reuse the newest cached Node runtime that already satisfies the constraint."""
    candidates: list[tuple[tuple[int, ...], Path]] = []
    for child in runtime_root.iterdir():
        if not child.is_dir():
            continue
        match = re.match(r"node-v(\d+(?:\.\d+){0,2})-", child.name)
        if not match:
            continue
        version_tuple = _version_tuple(match.group(1))
        if version_tuple and version_tuple >= required:
            candidates.append((version_tuple, child))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    version_tuple, target_dir = candidates[0]
    version = ".".join(str(part) for part in version_tuple)
    return target_dir, version


def _minimum_required_version(constraint: str) -> str:
    """Extract the minimum version from a simple >= Node constraint."""
    match = re.match(r"^\s*>=\s*v?(\d+(?:\.\d+){0,2})\s*$", constraint)
    if not match:
        raise ValueError(f"Unsupported Node version constraint: {constraint}")
    return match.group(1)


def _select_node_release(constraint: str) -> dict[str, str]:
    """Pick one downloadable Node distribution that satisfies the repo constraint."""
    minimum = _version_tuple(_minimum_required_version(constraint))
    target_platform = _node_distribution_suffix()
    with urlopen("https://nodejs.org/dist/index.json", timeout=30) as response:
        releases = json.loads(response.read().decode("utf-8"))

    same_major: list[dict[str, Any]] = []
    newer_major: list[dict[str, Any]] = []
    for item in releases:
        if not isinstance(item, dict):
            continue
        version = str(item.get("version", "")).strip().lstrip("v")
        version_tuple = _version_tuple(version)
        if not version_tuple or version_tuple < minimum:
            continue
        files = item.get("files")
        if not isinstance(files, list) or target_platform not in files:
            continue
        bucket = same_major if version_tuple[0] == minimum[0] else newer_major
        bucket.append({**item, "parsed_version": version_tuple, "plain_version": version})

    candidates = same_major or newer_major
    if not candidates:
        raise ValueError(f"No downloadable Node runtime found for constraint {constraint} on {target_platform}.")

    lts_candidates = [item for item in candidates if item.get("lts")]
    selected = sorted(lts_candidates or candidates, key=lambda item: item["parsed_version"], reverse=True)[0]
    version = str(selected["plain_version"])
    dirname = f"node-v{version}-{target_platform}"
    return {
        "version": version,
        "dirname": dirname,
        "archive_name": f"{dirname}.tar.xz",
        "download_url": f"https://nodejs.org/dist/v{version}/{dirname}.tar.xz",
    }


def _node_distribution_suffix() -> str:
    """Map the current container platform to the Node distribution suffix."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    os_map = {
        "linux": "linux",
        "darwin": "darwin",
    }
    arch_map = {
        "x86_64": "x64",
        "amd64": "x64",
        "aarch64": "arm64",
        "arm64": "arm64",
        "armv7l": "armv7l",
    }
    os_part = os_map.get(system)
    arch_part = arch_map.get(machine)
    if not os_part or not arch_part:
        raise ValueError(f"Unsupported platform for local Node runtimes: {system}/{machine}")
    return f"{os_part}-{arch_part}"


def _node_runtime_binding(target_dir: Path, constraint: str, resolved_version: str) -> dict[str, str]:
    """Return the persisted metadata for one provisioned Node runtime."""
    node_exec = target_dir / "bin" / "node"
    npm_cli = target_dir / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js"
    npx_cli = target_dir / "lib" / "node_modules" / "npm" / "bin" / "npx-cli.js"
    return {
        "constraint": constraint,
        "resolved_version": resolved_version,
        "root_dir": str(target_dir),
        "node_executable": str(node_exec),
        "npm_cli_path": str(npm_cli),
        "npx_cli_path": str(npx_cli),
    }


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
    provisionable_node_items = [
        item
        for item in analysis.get("runtime_status", [])
        if (
            isinstance(item, dict)
            and str(item.get("reason", "")).strip() in {"version_mismatch", "missing"}
            and bool(item.get("provisionable", False))
        )
    ]
    if provisionable_node_items:
        constraint = _node_runtime_constraint(analysis.get("runtime_constraints", {}))
        if constraint:
            return (
                "Install the MCP and Nanobot will provision a matching local Node runtime "
                f"for this server ({constraint})."
            )
        return "Install the MCP and Nanobot will provision a matching local runtime for this server."
    version_mismatches = [
        item
        for item in analysis.get("runtime_status", [])
        if isinstance(item, dict) and str(item.get("reason", "")) == "version_mismatch"
    ]
    if version_mismatches:
        return _describe_runtime_version_mismatches(version_mismatches)
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


def _describe_runtime_version_mismatches(items: list[dict[str, Any]]) -> str:
    """Format one or more runtime version mismatches for the UI."""
    parts: list[str] = []
    for item in items:
        name = str(item.get("name", "")).strip() or "runtime"
        current = str(item.get("version", "")).strip() or "not detected"
        required = str(item.get("required_version", "")).strip() or "a newer version"
        parts.append(f"{name} {current} installed, requires {required}")
    if not parts:
        return "The container runtime version is incompatible with this MCP."
    prefix = "Incompatible runtime versions for this MCP: "
    return prefix + "; ".join(parts)


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
    env_requirements = _normalize_env_requirements(
        payload.get("env_requirements", []),
        fallback_required=payload.get("required_env", []),
        fallback_optional=payload.get("optional_env", []),
    )
    required_env, optional_env = _split_env_requirements(env_requirements)

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
        "env_requirements": env_requirements,
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
        "uv pip install --system -e .": ["uv", "pip", "install", "--system", "-e", "."],
        "uv pip install -e .": ["uv", "pip", "install", "-e", "."],
        "uv venv .venv": ["uv", "venv", ".venv"],
        "uv pip install --python .venv/bin/python -r requirements.txt": [
            "uv",
            "pip",
            "install",
            "--python",
            ".venv/bin/python",
            "-r",
            "requirements.txt",
        ],
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
        ("uv", "venv", ".venv"),
        ("uv", "pip", "install", "--python", ".venv/bin/python", "-r", "requirements.txt"),
        ("uv", "pip", "install", "--system", "-e", "."),
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
) -> list[dict[str, Any]]:
    """Infer environment requirements from structured metadata, source, and runtime-adjacent docs."""
    requirements = _merge_env_requirements(
        _collect_env_requirements_from_env_examples(checkout_dir),
        _collect_env_requirements_from_example_config(example_config),
        _collect_env_requirements_from_server_manifest(server_manifest),
        _collect_env_requirements_from_source_scan(checkout_dir),
    )
    known_names = {str(item.get("name", "")).strip() for item in requirements if isinstance(item, dict)}
    readme_fallback = [
        item
        for item in _collect_env_requirements_from_readme(checkout_dir / "README.md")
        if str(item.get("name", "")).strip() not in known_names
    ]
    return _merge_env_requirements(requirements, readme_fallback)


def _collect_env_requirements_from_install_dir(install_dir_raw: Any) -> list[dict[str, Any]]:
    """Refresh env hints from one managed source checkout when available."""
    install_dir = Path(str(install_dir_raw).strip()).expanduser() if str(install_dir_raw).strip() else None
    if install_dir is None or not install_dir.exists() or not install_dir.is_dir():
        return []
    return _collect_env_requirements(install_dir, _load_mcp_example(install_dir), _load_server_manifest(install_dir))


def _collect_env_requirements_from_env_examples(checkout_dir: Path) -> list[dict[str, Any]]:
    """Collect env names from example env files shipped with one repo."""
    requirements: list[dict[str, Any]] = []
    for filename in (".env.example", ".env.sample", ".env.template"):
        path = checkout_dir / filename
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("#"):
                match = re.match(r"#\s*([A-Z][A-Z0-9_]*)=(.*)$", line)
                if match:
                    env_name = match.group(1)
                    requirements.append(
                        _build_env_requirement(
                            env_name,
                            required=False,
                            confidence="high",
                            source=f"env_example:{filename}",
                            reason=f"Commented optional env in {filename}.",
                            default_value=_normalize_env_default_candidate(env_name, match.group(2)),
                            default_source=f"env_example:{filename}",
                        )
                    )
                continue
            match = re.match(r"([A-Z][A-Z0-9_]*)=(.*)$", line)
            if match:
                env_name = match.group(1)
                requirements.append(
                    _build_env_requirement(
                        env_name,
                        required=True,
                        confidence="high",
                        source=f"env_example:{filename}",
                        reason=f"Declared in {filename}.",
                        default_value=_normalize_env_default_candidate(env_name, match.group(2)),
                        default_source=f"env_example:{filename}",
                    )
                )
    return requirements


def _collect_env_requirements_from_example_config(example_config: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect env names from bundled MCP config examples."""
    source_name = str(example_config.get("source_file", "")).strip() or "mcp-example"
    requirements: list[dict[str, Any]] = []
    env_block = example_config.get("env") or {}
    if not isinstance(env_block, dict):
        return requirements
    for key, value in env_block.items():
        env_name = str(key).strip()
        if not env_name:
            continue
        requirements.append(
            _build_env_requirement(
                env_name,
                required=True,
                confidence="high",
                source=f"example_config:{source_name}",
                reason=f"Listed in {source_name}.",
                default_value=_normalize_env_default_candidate(env_name, value),
                default_source=f"example_config:{source_name}",
            )
        )
    return requirements


def _collect_env_requirements_from_server_manifest(server_manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect env names from server.json style MCP manifests."""
    requirements: list[dict[str, Any]] = []
    packages = server_manifest.get("packages")
    if not isinstance(packages, list):
        return requirements
    for package in packages:
        if not isinstance(package, dict):
            continue
        identifier = str(package.get("identifier", "")).strip() or str(package.get("registryType", "")).strip() or "server.json"
        for env_var in package.get("environmentVariables", []) or []:
            if not isinstance(env_var, dict):
                continue
            env_name = env_var.get("name", "")
            default_value = _normalize_env_default_candidate(
                env_name,
                env_var.get("defaultValue", env_var.get("default", env_var.get("value", ""))),
            )
            requirements.append(
                _build_env_requirement(
                    env_name,
                    required=bool(env_var.get("isRequired", False)),
                    confidence="high",
                    source=f"server_manifest:{identifier}",
                    reason=f"Declared in server.json package metadata for {identifier}.",
                    default_value=default_value,
                    default_source=f"server_manifest:{identifier}" if default_value else "",
                )
            )
    return requirements


def _collect_env_requirements_from_source_scan(checkout_dir: Path) -> list[dict[str, Any]]:
    """Collect env names from source files when the repo lacks stronger metadata."""
    requirements: list[dict[str, Any]] = []
    for path in _iter_env_scan_files(checkout_dir):
        content = _read_text(path)
        if not content:
            continue
        relative_path = str(path.relative_to(checkout_dir))
        suffix = path.suffix.lower()
        if suffix in {".js", ".cjs", ".mjs", ".jsx", ".ts", ".tsx"}:
            requirements.extend(_collect_js_env_requirements(content, relative_path))
        elif suffix == ".py":
            requirements.extend(_collect_python_env_requirements(content, relative_path))
        elif suffix == ".go":
            requirements.extend(_collect_go_env_requirements(content, relative_path))
    return requirements


def _collect_env_requirements_from_readme(path: Path) -> list[dict[str, Any]]:
    """Use README env mentions only as a last-resort fallback."""
    content = _read_text(path)
    if not content:
        return []

    requirements: list[dict[str, Any]] = []
    in_env_section = False
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            in_env_section = bool(re.search(r"\b(env|environment|configuration)\b", line, flags=re.IGNORECASE))
            continue
        if not in_env_section and not any(token in line.lower() for token in ("env", "environment", "variable", "config")) and "|" not in line and "`" not in line and "=" not in line:
            continue
        matches = re.findall(r"\b([A-Z][A-Z0-9_]*_[A-Z0-9_]*)\b", line)
        if not matches:
            continue
        is_required = "required" in line.lower() and "optional" not in line.lower()
        for name in _normalize_env_names(matches):
            requirements.append(
                _build_env_requirement(
                    name,
                    required=is_required,
                    confidence="low",
                    source=f"readme:{path.name}",
                    reason=f"Documented in {path.name}.",
                )
            )
    return requirements


def _iter_env_scan_files(checkout_dir: Path) -> list[Path]:
    """Return source files worth scanning for env usage."""
    candidates: list[Path] = []
    ignored_dirs = {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".next",
        ".turbo",
        ".yarn",
        ".pnpm-store",
        "coverage",
        "dist",
        "build",
        "target",
        "vendor",
    }
    allowed_suffixes = {".js", ".cjs", ".mjs", ".jsx", ".ts", ".tsx", ".py", ".go"}
    for root, dirnames, filenames in os.walk(checkout_dir):
        dirnames[:] = [name for name in dirnames if name not in ignored_dirs]
        for filename in filenames:
            path = Path(root) / filename
            if path.suffix.lower() not in allowed_suffixes:
                continue
            try:
                if path.stat().st_size > 256_000:
                    continue
            except OSError:
                continue
            candidates.append(path)
            if len(candidates) >= 200:
                return candidates
    return candidates


def _collect_js_env_requirements(content: str, relative_path: str) -> list[dict[str, Any]]:
    """Collect env names from JS and TS source code."""
    requirements: list[dict[str, Any]] = []
    lines = content.splitlines()
    guard_required_envs = _collect_js_guard_required_envs(lines)
    patterns = [
        re.compile(r"process\.env(?:\?\.)?\.([A-Z][A-Z0-9_]*)"),
        re.compile(r"process\.env\s*\[\s*[\"']([A-Z][A-Z0-9_]*)[\"']\s*\]"),
    ]
    for line in lines:
        for pattern in patterns:
            for match in pattern.finditer(line):
                env_name = match.group(1)
                required = env_name in guard_required_envs
                default_value = _extract_js_env_default(line[match.end() :], env_name)
                optional = (
                    not required
                    or bool(default_value)
                    or _js_env_line_looks_toggle_optional(line, env_name, match.end())
                    or _env_name_prefers_optional(env_name)
                )
                requirements.append(
                    _build_env_requirement(
                        env_name,
                        required=required and not optional,
                        confidence="medium",
                        source=f"source_scan:{relative_path}",
                        reason=(
                            f"Checked by a startup guard in {relative_path}."
                            if required and not optional
                            else f"Referenced via process.env with a default or toggle pattern in {relative_path}."
                            if optional
                            else f"Referenced via process.env without a strict startup guard in {relative_path}."
                        ),
                        default_value=default_value,
                        default_source=f"source_scan:{relative_path}" if default_value else "",
                    )
                )
    return requirements


def _collect_python_env_requirements(content: str, relative_path: str) -> list[dict[str, Any]]:
    """Collect env names from Python source code."""
    requirements: list[dict[str, Any]] = []
    for line in content.splitlines():
        for match in re.finditer(r"os\.environ\[\s*[\"']([A-Z][A-Z0-9_]*)[\"']\s*\]", line):
            requirements.append(
                _build_env_requirement(
                    match.group(1),
                    required=True,
                    confidence="medium",
                    source=f"source_scan:{relative_path}",
                    reason=f"Referenced via os.environ[...] in {relative_path}.",
                )
            )
        for match in re.finditer(r"os\.getenv\(\s*[\"']([A-Z][A-Z0-9_]*)[\"'](?:\s*,\s*([^)]+))?\)", line):
            env_name = match.group(1)
            default_value = _normalize_runtime_default_candidate(env_name, match.group(2) or "")
            requirements.append(
                _build_env_requirement(
                    env_name,
                    required=False,
                    confidence="medium",
                    source=f"source_scan:{relative_path}",
                    reason=(
                        f"Referenced via os.getenv with a fallback in {relative_path}."
                        if match.group(2)
                        else f"Referenced via os.getenv without a strict startup guard in {relative_path}."
                    ),
                    default_value=default_value,
                    default_source=f"source_scan:{relative_path}" if default_value else "",
                )
            )
        for match in re.finditer(r"(?:os\.)?environ\.get\(\s*[\"']([A-Z][A-Z0-9_]*)[\"'](?:\s*,\s*([^)]+))?\)", line):
            env_name = match.group(1)
            default_value = _normalize_runtime_default_candidate(env_name, match.group(2) or "")
            requirements.append(
                _build_env_requirement(
                    env_name,
                    required=False,
                    confidence="medium",
                    source=f"source_scan:{relative_path}",
                    reason=(
                        f"Referenced via environ.get with a fallback in {relative_path}."
                        if match.group(2)
                        else f"Referenced via environ.get without a strict startup guard in {relative_path}."
                    ),
                    default_value=default_value,
                    default_source=f"source_scan:{relative_path}" if default_value else "",
                )
            )
    return requirements


def _collect_go_env_requirements(content: str, relative_path: str) -> list[dict[str, Any]]:
    """Collect env names from Go source code."""
    requirements: list[dict[str, Any]] = []
    for line in content.splitlines():
        for match in re.finditer(r"os\.Getenv\(\s*\"([A-Z][A-Z0-9_]*)\"\s*\)", line):
            requirements.append(
                _build_env_requirement(
                    match.group(1),
                    required=False,
                    confidence="medium",
                    source=f"source_scan:{relative_path}",
                    reason=f"Referenced via os.Getenv without a strict startup guard in {relative_path}.",
                )
            )
        for match in re.finditer(r"os\.LookupEnv\(\s*\"([A-Z][A-Z0-9_]*)\"\s*\)", line):
            requirements.append(
                _build_env_requirement(
                    match.group(1),
                    required=False,
                    confidence="medium",
                    source=f"source_scan:{relative_path}",
                    reason=f"Referenced via os.LookupEnv in {relative_path}.",
                )
            )
    return requirements


def _collect_js_guard_required_envs(lines: list[str]) -> set[str]:
    """Detect env vars that are explicitly checked in a startup guard."""
    alias_to_env: dict[str, str] = {}
    assignment_patterns = [
        re.compile(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*process\.env(?:\?\.)?\.([A-Z][A-Z0-9_]*)"),
        re.compile(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*process\.env\s*\[\s*[\"']([A-Z][A-Z0-9_]*)[\"']\s*\]"),
    ]
    for line in lines:
        for pattern in assignment_patterns:
            for match in pattern.finditer(line):
                alias_to_env[match.group(1)] = match.group(2)

    required_envs: set[str] = set()
    for index, line in enumerate(lines):
        if "if" not in line or "!" not in line:
            continue
        match = re.search(r"if\s*\(([^)]*)\)", line)
        if not match:
            continue
        condition = match.group(1)
        negated_aliases = [alias for alias in re.findall(r"!\s*([A-Za-z_$][\w$]*)", condition) if alias in alias_to_env]
        if not negated_aliases:
            continue
        lookahead = "\n".join(lines[index : min(index + 5, len(lines))])
        if "return null" not in lookahead and "return;" not in lookahead and "throw" not in lookahead:
            continue
        if "||" in condition and "&&" not in condition:
            required_envs.update(alias_to_env[alias] for alias in negated_aliases)
    return required_envs


def _js_env_line_looks_toggle_optional(line: str, env_name: str, match_end: int) -> bool:
    """Return whether one JS env access looks like a toggle or non-critical option."""
    if _env_name_prefers_optional(env_name):
        return True
    tail = line[match_end:]
    return bool(
        re.search(r"\s*(?:===|==)\s*[\"']true[\"']", tail)
        or re.search(r"\s*(?:!==|!=)\s*[\"']false[\"']", tail)
        or re.search(r"\s*(?:===|==)\s*[\"']false[\"']", tail)
        or re.search(r"\s*(?:!==|!=)\s*[\"']true[\"']", tail)
    )


def _env_name_prefers_optional(env_name: str) -> bool:
    """Return whether one env name usually represents a toggle or tuning knob."""
    normalized = str(env_name).strip().upper()
    if not normalized:
        return False
    optional_suffixes = (
        "_ENABLED",
        "_DISABLED",
        "_TLS",
        "_STARTTLS",
        "_VERIFY_SSL",
        "_READ_ONLY",
        "_POOL_ENABLED",
    )
    optional_fragments = (
        "_AUTO_",
        "_HOOK_",
        "_ALERT_",
    )
    return normalized.endswith(optional_suffixes) or any(fragment in normalized for fragment in optional_fragments)


def _collect_env_requirements_from_runtime_error(message: str) -> list[dict[str, Any]]:
    """Promote env names mentioned in runtime failures back into the MCP record."""
    requirements: list[dict[str, Any]] = []
    for raw_line in str(message).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if not re.search(r"\b(env|environment)\b", line, flags=re.IGNORECASE):
            continue
        names = _normalize_env_names(re.findall(r"\b([A-Z][A-Z0-9_]*)\b", line))
        if not names:
            continue
        for name in names:
            requirements.append(
                _build_env_requirement(
                    name,
                    required=True,
                    confidence="high",
                    source="runtime_error",
                    reason=f"Runtime failure mentioned {name}.",
                )
            )
    return requirements


def _merge_runtime_error_env_requirements(
    record: dict[str, Any],
    current_env: dict[str, str],
    message: str,
) -> dict[str, Any]:
    """Merge env requirements inferred from runtime errors back into one MCP record."""
    runtime_requirements = _collect_env_requirements_from_runtime_error(message)
    if not runtime_requirements:
        return record
    env_requirements = _merge_env_requirements(record.get("env_requirements", []), runtime_requirements)
    required_env, optional_env = _split_env_requirements(env_requirements)
    return {
        **record,
        "env_requirements": env_requirements,
        "required_env": required_env,
        "optional_env": optional_env,
        "missing_env": _missing_env_vars(required_env, current_env),
    }


def _build_env_requirement(
    name: Any,
    *,
    required: bool,
    confidence: str,
    source: str,
    reason: str,
    default_value: Any = "",
    default_source: str = "",
) -> dict[str, Any]:
    """Create one normalized env requirement record."""
    normalized = _normalize_env_names([name])
    if not normalized:
        return {}
    level = str(confidence).strip().lower()
    if level not in {"low", "medium", "high"}:
        level = "medium"
    normalized_default = _normalize_env_default_candidate(normalized[0], default_value)
    payload = {
        "name": normalized[0],
        "required": bool(required),
        "confidence": level,
        "sources": [str(source).strip()] if str(source).strip() else [],
        "reason": str(reason).strip(),
    }
    if normalized_default:
        payload["default_value"] = normalized_default
        payload["default_source"] = str(default_source).strip() or str(source).strip()
    return payload


def _normalize_env_requirements(
    value: Any,
    *,
    fallback_required: Any = None,
    fallback_optional: Any = None,
) -> list[dict[str, Any]]:
    """Normalize legacy env lists and rich env requirement objects into one structure."""
    requirements = _merge_env_requirements(value)
    if fallback_required:
        requirements = _merge_env_requirements(
            requirements,
            [
                _build_env_requirement(
                    name,
                    required=True,
                    confidence="medium",
                    source="legacy_required_env",
                    reason="Persisted from legacy MCP metadata.",
                )
                for name in fallback_required
            ],
        )
    if fallback_optional:
        requirements = _merge_env_requirements(
            requirements,
            [
                _build_env_requirement(
                    name,
                    required=False,
                    confidence="medium",
                    source="legacy_optional_env",
                    reason="Persisted from legacy MCP metadata.",
                )
                for name in fallback_optional
            ],
        )
    return requirements


def _merge_env_requirements(*collections: Any) -> list[dict[str, Any]]:
    """Merge env requirement evidence while keeping the richest known record per env name."""
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for collection in collections:
        for item in _iter_env_requirement_items(collection):
            normalized = _coerce_env_requirement(item)
            if not normalized:
                continue
            name = normalized["name"]
            existing = merged.get(name)
            if existing is None:
                merged[name] = normalized
                order.append(name)
                continue
            required_before = bool(existing.get("required", False))
            existing["required"] = required_before or bool(normalized.get("required", False))
            for source in normalized.get("sources", []):
                if source not in existing["sources"]:
                    existing["sources"].append(source)
            if _env_confidence_rank(normalized.get("confidence", "")) > _env_confidence_rank(existing.get("confidence", "")):
                existing["confidence"] = normalized.get("confidence", "medium")
                if normalized.get("reason"):
                    existing["reason"] = normalized["reason"]
            elif normalized.get("reason") and not existing.get("reason"):
                existing["reason"] = normalized["reason"]
            elif normalized.get("required") and not required_before and normalized.get("reason"):
                existing["reason"] = normalized["reason"]
            normalized_default = str(normalized.get("default_value", "")).strip()
            existing_default = str(existing.get("default_value", "")).strip()
            if normalized_default:
                if (
                    not existing_default
                    or _env_confidence_rank(normalized.get("confidence", "")) > _env_confidence_rank(existing.get("confidence", ""))
                ):
                    existing["default_value"] = normalized_default
                    existing["default_source"] = str(normalized.get("default_source", "")).strip()
                elif not str(existing.get("default_source", "")).strip():
                    existing["default_source"] = str(normalized.get("default_source", "")).strip()
    return [merged[name] for name in order]


def _iter_env_requirement_items(value: Any) -> list[Any]:
    """Flatten one env requirement payload into individual items."""
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return list(value)
    if value is None or value == "":
        return []
    return [value]


def _coerce_env_requirement(value: Any) -> dict[str, Any]:
    """Coerce one legacy env hint into the rich env requirement shape."""
    if isinstance(value, dict):
        name = _normalize_env_names([value.get("name", "")])
        if not name:
            return {}
        confidence = str(value.get("confidence", "medium")).strip().lower()
        if confidence not in {"low", "medium", "high"}:
            confidence = "medium"
        sources = [str(item).strip() for item in value.get("sources", []) if str(item).strip()] if isinstance(value.get("sources"), list) else []
        source = str(value.get("source", "")).strip()
        if source and source not in sources:
            sources.append(source)
        return {
            "name": name[0],
            "required": bool(value.get("required", False)),
            "confidence": confidence,
            "sources": sources,
            "reason": str(value.get("reason", "")).strip(),
            "default_value": _normalize_env_default_candidate(name[0], value.get("default_value", "")),
            "default_source": str(value.get("default_source", "")).strip(),
        }
    if isinstance(value, str):
        normalized = _normalize_env_names([value])
        if not normalized:
            return {}
        return {
            "name": normalized[0],
            "required": False,
            "confidence": "medium",
            "sources": [],
            "reason": "",
        }
    return {}


def _split_env_requirements(env_requirements: Any) -> tuple[list[str], list[str]]:
    """Return legacy required/optional env name lists from rich env metadata."""
    required: list[str] = []
    optional: list[str] = []
    for item in _normalize_env_requirements(env_requirements):
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        if bool(item.get("required", False)):
            required.append(name)
        elif name not in optional:
            optional.append(name)
    return required, [name for name in optional if name not in required]


def _env_confidence_rank(value: Any) -> int:
    """Map env confidence labels into comparable weights."""
    levels = {"low": 1, "medium": 2, "high": 3}
    return levels.get(str(value).strip().lower(), 2)


def _example_runtime_is_actionable(example_config: dict[str, Any]) -> bool:
    """Return whether an example MCP config contains a runnable startup target."""
    transport = str(example_config.get("transport", "")).strip()
    if transport == "stdio":
        command = str(example_config.get("command", "")).strip()
        args = [str(item).strip() for item in example_config.get("args", []) if str(item).strip()]
        return bool(command) and not any(_looks_like_placeholder_runtime_value(arg) for arg in args)
    if transport in {"sse", "streamableHttp"}:
        url = str(example_config.get("url", "")).strip()
        return bool(url) and not _looks_like_placeholder_runtime_value(url)
    return False


def _looks_like_placeholder_runtime_value(value: str) -> bool:
    """Detect placeholder paths and URLs from documentation-only MCP examples."""
    raw = str(value).strip()
    if not raw:
        return False
    if raw.startswith("/path/to/"):
        return True
    if raw.startswith("<") and raw.endswith(">"):
        return True
    if "{path}" in raw.lower():
        return True
    return False


def _relative_runtime_path(value: str) -> str:
    """Normalize a repo-local runtime path so it can later expand against install_dir."""
    raw = str(value).strip()
    if not raw or raw.startswith("/path/to/") or Path(raw).is_absolute():
        return raw
    normalized = raw[2:] if raw.startswith("./") else raw.lstrip("/")
    return f"./{normalized}" if normalized else raw


def _derive_node_entry(checkout_dir: Path, package_json: dict[str, Any]) -> tuple[str, list[str]]:
    """Best-effort runtime command for Node-based MCP servers."""
    scripts = package_json.get("scripts") or {}
    bin_map = package_json.get("bin") or {}

    if isinstance(bin_map, dict) and bin_map:
        entry = next((str(item).strip() for item in bin_map.values() if str(item).strip()), "")
        if entry:
            return "node", [_relative_runtime_path(entry)]

    if "start" in scripts and (checkout_dir / "build" / "index.js").exists():
        return "node", ["./build/index.js"]
    if (checkout_dir / "build" / "index.js").exists():
        return "node", ["./build/index.js"]
    if (checkout_dir / "dist" / "index.js").exists():
        return "node", ["./dist/index.js"]
    for candidate in (
        "build/main.js",
        "dist/main.js",
        "lib/index.js",
        "src/index.js",
        "server.js",
        "mcp.js",
    ):
        path = checkout_dir / candidate
        if path.exists():
            return "node", [_relative_runtime_path(candidate)]

    return "", []


def _derive_python_file_entry(checkout_dir: Path) -> str:
    """Pick a direct Python entry file from a simple source checkout."""
    for candidate in ("src/main.py", "main.py", "__main__.py"):
        if (checkout_dir / candidate).exists():
            return candidate
    return ""


def _derive_python_entry(checkout_dir: Path, pyproject_data: dict[str, Any]) -> tuple[str, list[str]]:
    """Best-effort runtime command for Python-based MCP servers."""
    project = pyproject_data.get("project") if isinstance(pyproject_data, dict) else {}
    scripts = project.get("scripts") if isinstance(project, dict) else {}
    if isinstance(scripts, dict) and scripts:
        preferred = next(
            (
                str(name).strip()
                for name in scripts.keys()
                if str(name).strip() and "mcp" in str(name).strip().lower()
            ),
            "",
        )
        script_name = preferred or next((str(name).strip() for name in scripts.keys() if str(name).strip()), "")
        if script_name:
            return "uv", ["run", "--directory", "./", script_name]
    django_manage_entry = _derive_django_manage_entry(checkout_dir, pyproject_data)
    if django_manage_entry:
        return "uv", ["run", "--directory", "./", "python", f"./{django_manage_entry}", "stdio_server"]
    script_entry = _derive_python_file_entry(checkout_dir)
    if script_entry:
        return "uv", ["run", "--directory", "./", "python", f"./{script_entry}"]
    return "", []


def _derive_python_install_steps(pyproject_data: dict[str, Any], run_args: list[str]) -> list[dict[str, Any]]:
    """Pick the safest install steps for one Python MCP source checkout."""
    steps = [
        {
            "command": ["uv", "sync"],
            "display": "uv sync",
            "timeout": 900,
        }
    ]
    extra_deps = _derive_python_group_dependency_specs(pyproject_data, run_args)
    if extra_deps:
        steps.append(
            {
                "command": ["uv", "pip", "install", "--python", ".venv/bin/python", *extra_deps],
                "display": "uv pip install --python .venv/bin/python " + " ".join(extra_deps),
                "timeout": 900,
            }
        )
    return steps


def _derive_python_group_dependency_specs(pyproject_data: dict[str, Any], run_args: list[str]) -> list[str]:
    """Collect extra dependency specs for runnable example-based Python MCPs."""
    if not any(str(arg).endswith("manage.py") for arg in run_args):
        return []

    specs: list[str] = []
    tool = pyproject_data.get("tool") if isinstance(pyproject_data, dict) else {}
    if isinstance(tool, dict):
        poetry = tool.get("poetry")
        if isinstance(poetry, dict):
            groups = poetry.get("group")
            if isinstance(groups, dict):
                for group in groups.values():
                    if not isinstance(group, dict):
                        continue
                    dependencies = group.get("dependencies")
                    if isinstance(dependencies, dict):
                        for name in dependencies.keys():
                            value = str(name).strip()
                            if value and value not in specs:
                                specs.append(value)

        uv_tool = tool.get("uv")
        if isinstance(uv_tool, dict):
            dev_dependencies = uv_tool.get("dev-dependencies")
            if isinstance(dev_dependencies, list):
                for item in dev_dependencies:
                    value = str(item).strip()
                    if value and value not in specs:
                        specs.append(value)

    dependency_groups = pyproject_data.get("dependency-groups") if isinstance(pyproject_data, dict) else {}
    if isinstance(dependency_groups, dict):
        for group_specs in dependency_groups.values():
            if not isinstance(group_specs, list):
                continue
            for item in group_specs:
                value = str(item).strip()
                if value and value not in specs:
                    specs.append(value)
    return specs


def _derive_django_manage_entry(checkout_dir: Path, pyproject_data: dict[str, Any]) -> str:
    """Pick a Django manage.py entry when the repo exposes a stdio MCP management command."""
    if not _pyproject_mentions_django(pyproject_data):
        return ""
    if not any(checkout_dir.rglob("management/commands/stdio_server.py")):
        return ""

    candidates: list[tuple[int, int, str]] = []
    for manage_path in checkout_dir.rglob("manage.py"):
        relative = manage_path.relative_to(checkout_dir)
        if any(part in {"site-packages", ".venv", "venv", "node_modules"} for part in relative.parts):
            continue
        score = 0
        rel_lower = str(relative).lower()
        if len(relative.parts) == 1:
            score += 6
        if "examples/" in rel_lower or rel_lower.startswith("examples/"):
            score += 5
        if "/test/" in rel_lower or rel_lower.startswith("test/"):
            score -= 3
        if "/tests/" in rel_lower or rel_lower.startswith("tests/"):
            score -= 3
        if "example" in rel_lower:
            score += 2
        candidates.append((score, -len(relative.parts), str(relative)))

    if not candidates:
        return ""
    candidates.sort(reverse=True)
    return candidates[0][2]


def _pyproject_mentions_django(pyproject_data: dict[str, Any]) -> bool:
    """Return whether a pyproject declares Django in modern or Poetry metadata."""
    if not isinstance(pyproject_data, dict):
        return False

    project = pyproject_data.get("project")
    if isinstance(project, dict):
        dependencies = project.get("dependencies")
        if isinstance(dependencies, list):
            for item in dependencies:
                if "django" in str(item).lower():
                    return True

    tool = pyproject_data.get("tool")
    if isinstance(tool, dict):
        poetry = tool.get("poetry")
        if isinstance(poetry, dict):
            dependencies = poetry.get("dependencies")
            if isinstance(dependencies, dict):
                for name in dependencies.keys():
                    if "django" in str(name).lower():
                        return True
    return False


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
    env_requirements: Any = None,
) -> dict[str, str]:
    """Pre-fill obvious MCP env values from the current nanobot config."""
    return {
        name: item["value"]
        for name, item in _guess_env_default_hints(
            config=config,
            server_name=server_name,
            required_env=required_env,
            optional_env=optional_env,
            env_requirements=env_requirements,
            workspace=workspace,
        ).items()
    }


def _guess_env_default_hints(
    *,
    config,
    server_name: str,
    required_env: list[str],
    optional_env: list[str],
    workspace: Path,
    env_requirements: Any = None,
) -> dict[str, dict[str, str]]:
    """Return visible default hints for MCP env fields, including their source."""
    requirements = _normalize_env_requirements(
        env_requirements,
        fallback_required=required_env,
        fallback_optional=optional_env,
    )
    defaults: dict[str, dict[str, str]] = {}
    mappings = {
        "OPENAI_API_KEY": (config.providers.openai.api_key, "nanobot_config:providers.openai.api_key"),
        "ANTHROPIC_API_KEY": (config.providers.anthropic.api_key, "nanobot_config:providers.anthropic.api_key"),
        "MOONSHOT_API_KEY": (config.providers.moonshot.api_key, "nanobot_config:providers.moonshot.api_key"),
        "OPENROUTER_API_KEY": (config.providers.openrouter.api_key, "nanobot_config:providers.openrouter.api_key"),
        "BRAVE_API_KEY": (config.tools.web.search.api_key, "nanobot_config:tools.web.search.api_key"),
    }

    for item in requirements:
        env_name = str(item.get("name", "")).strip()
        if not env_name:
            continue
        mapped_value, mapped_source = mappings.get(env_name, ("", ""))
        if str(mapped_value).strip():
            defaults[env_name] = {"value": str(mapped_value).strip(), "source": mapped_source}
            continue
        default_value = _normalize_env_default_candidate(env_name, item.get("default_value", ""))
        if default_value:
            defaults[env_name] = {
                "value": default_value,
                "source": str(item.get("default_source", "")).strip() or "repository_default",
            }

    path_env_names = [
        str(item.get("name", "")).strip()
        for item in requirements
        if _should_prefill_workspace_path(str(item.get("name", "")).strip())
    ]
    if path_env_names:
        save_dir = workspace / "mcp-output" / server_name
        save_dir.mkdir(parents=True, exist_ok=True)
        for env_name in path_env_names:
            defaults.setdefault(
                env_name,
                {"value": str(save_dir), "source": "gui_heuristic:workspace_output_path"},
            )

    port_values: dict[str, str] = {}
    for item in requirements:
        env_name = str(item.get("name", "")).strip()
        if not env_name or not _should_prefill_local_port(env_name):
            continue
        value = defaults.get(env_name, {}).get("value", "").strip()
        if not value:
            value = _suggest_local_port(server_name, env_name)
            defaults[env_name] = {"value": value, "source": "gui_heuristic:local_port"}
        port_values[env_name] = value

    host_values: dict[str, str] = {}
    for item in requirements:
        env_name = str(item.get("name", "")).strip()
        if not env_name or not _should_prefill_local_host(env_name):
            continue
        value = defaults.get(env_name, {}).get("value", "").strip()
        if not value:
            value = "127.0.0.1"
            defaults[env_name] = {"value": value, "source": "gui_heuristic:local_host"}
        host_values[env_name] = value

    resolved_host = next((value for value in host_values.values() if value), "127.0.0.1")
    resolved_port = next((value for value in port_values.values() if value), "")
    for item in requirements:
        env_name = str(item.get("name", "")).strip()
        if not env_name or not _should_prefill_local_url(env_name):
            continue
        if defaults.get(env_name, {}).get("value", "").strip():
            continue
        port = resolved_port or _suggest_local_port(server_name, "PORT")
        defaults[env_name] = {
            "value": f"http://{resolved_host}:{port}",
            "source": "gui_heuristic:local_url",
        }
    return defaults


def _should_prefill_workspace_path(env_name: str) -> bool:
    """Return whether one env name likely expects a writable filesystem path."""
    normalized = str(env_name).strip().upper()
    if not normalized:
        return False
    if normalized in {"PATH", "PYTHONPATH", "NODE_PATH", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"}:
        return False
    path_suffixes = (
        "BASE_PATH",
        "OUTPUT_PATH",
        "SAVE_PATH",
        "DATA_PATH",
        "CACHE_PATH",
        "OUTPUT_DIR",
        "SAVE_DIR",
        "DATA_DIR",
        "CACHE_DIR",
        "TEMP_DIR",
        "TEMP_PATH",
    )
    return any(normalized.endswith(suffix) for suffix in path_suffixes)


def _should_prefill_local_port(env_name: str) -> bool:
    """Return whether one env name likely expects a local MCP bind port."""
    normalized = str(env_name).strip().upper()
    if not normalized:
        return False
    if normalized == "PORT":
        return True
    if not normalized.endswith("_PORT"):
        return False
    return _env_name_has_local_bind_hint(normalized)


def _should_prefill_local_host(env_name: str) -> bool:
    """Return whether one env name likely expects a local MCP bind host."""
    normalized = str(env_name).strip().upper()
    if not normalized:
        return False
    if normalized in {"HOST", "HOSTNAME"}:
        return True
    if not (normalized.endswith("_HOST") or normalized.endswith("_HOSTNAME")):
        return False
    return _env_name_has_local_bind_hint(normalized)


def _should_prefill_local_url(env_name: str) -> bool:
    """Return whether one env name likely expects a local MCP endpoint URL."""
    normalized = str(env_name).strip().upper()
    if not normalized:
        return False
    direct_names = {
        "MCP_URL",
        "MCP_SERVER_URL",
        "SERVER_URL",
        "SERVICE_URL",
        "LOCAL_URL",
        "LOCAL_BASE_URL",
        "LOCAL_ENDPOINT_URL",
    }
    if normalized in direct_names:
        return True
    if not normalized.endswith("_URL"):
        return False
    return _env_name_has_local_bind_hint(normalized)


def _env_name_has_local_bind_hint(normalized: str) -> bool:
    """Return whether one env name looks like local server bind config, not external service config."""
    positive_tokens = {"APP", "SERVER", "MCP", "HTTP", "HTTPS", "SERVICE", "LISTEN", "LOCAL", "WEB", "WS"}
    negative_tokens = {
        "SMTP",
        "IMAP",
        "POSTGRES",
        "POSTGRESQL",
        "MYSQL",
        "MONGO",
        "REDIS",
        "DATABASE",
        "DB",
        "API",
        "FTP",
        "SSH",
        "OAUTH",
        "FIGMA",
    }
    parts = [part for part in normalized.split("_") if part]
    if any(part in negative_tokens for part in parts):
        return False
    return any(part in positive_tokens for part in parts)


def _suggest_local_port(server_name: str, env_name: str) -> str:
    """Return one stable local port suggestion for an MCP server env."""
    digest = hashlib.sha256(f"{server_name}:{env_name}".encode("utf-8")).hexdigest()
    return str(3300 + (int(digest[:6], 16) % 3000))


def _normalize_env_default_candidate(env_name: Any, raw_value: Any) -> str:
    """Normalize one repository-provided env default while skipping placeholders and secrets."""
    normalized_name = str(env_name).strip().upper()
    if _env_name_is_secret_like(normalized_name):
        return ""
    candidate = str(raw_value or "").strip()
    if not candidate:
        return ""
    candidate = re.split(r"\s+#", candidate, maxsplit=1)[0].strip()
    if len(candidate) >= 2 and candidate[0] == candidate[-1] and candidate[0] in {"'", '"'}:
        candidate = candidate[1:-1].strip()
    if not candidate:
        return ""
    lower = candidate.lower()
    if (
        "<" in candidate
        or ">" in candidate
        or "${" in candidate
        or lower.startswith("your_")
        or lower.startswith("your-")
        or "example" in lower
        or "placeholder" in lower
        or "changeme" in lower
        or "replace_me" in lower
        or "replace-with" in lower
    ):
        return ""
    return candidate


def _normalize_runtime_default_candidate(env_name: Any, raw_value: Any) -> str:
    """Normalize one runtime default literal from source code."""
    candidate = str(raw_value or "").strip()
    if not candidate:
        return ""
    if candidate in {"None", "null", "undefined"}:
        return ""
    return _normalize_env_default_candidate(env_name, candidate)


def _extract_js_env_default(tail: str, env_name: str) -> str:
    """Extract one simple JS default literal from a process.env expression tail."""
    match = re.match(r"\s*(?:\?\?|\|\|)\s*(\"[^\"]*\"|'[^']*'|\d+|true|false)\b", tail)
    if not match:
        return ""
    return _normalize_runtime_default_candidate(env_name, match.group(1))


def _env_name_is_secret_like(env_name: str) -> bool:
    """Return whether an env name likely carries secrets that should never be auto-filled from repo examples."""
    normalized = str(env_name).strip().upper()
    if not normalized:
        return False
    secret_suffixes = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_PASS", "_CLIENT_SECRET")
    return normalized.endswith(secret_suffixes)


def _merge_env_requirement_default_hints(
    env_requirements: Any,
    hints: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    """Attach guessed default hints to rich env requirements without losing stronger repository evidence."""
    merged = _normalize_env_requirements(env_requirements)
    for item in merged:
        env_name = str(item.get("name", "")).strip()
        hint = hints.get(env_name, {})
        if not env_name or not hint:
            continue
        if not str(item.get("default_value", "")).strip():
            item["default_value"] = str(hint.get("value", "")).strip()
        if not str(item.get("default_source", "")).strip():
            item["default_source"] = str(hint.get("source", "")).strip()
    return merged


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
    if value.startswith(".venv/"):
        return str(install_dir / value)
    if value.startswith("build/") or value.startswith("dist/"):
        return str(install_dir / value)
    if "/" in value and not Path(value).is_absolute():
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


def _normalize_repo_url(value: str) -> str:
    """Normalize repository URLs so duplicate detection is stable across variants."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith("git@"):
        host_part, path = raw.split(":", 1)
        host = host_part.split("@", 1)[1].lower()
        path = path.removesuffix(".git").strip("/")
        return f"https://{host}/{path}"
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        return raw.rstrip("/").removesuffix(".git").lower()
    path = parsed.path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if parsed.netloc.lower() in {"github.com", "www.github.com"}:
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 2:
            return f"https://github.com/{parts[0]}/{parts[1]}".lower()
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}".lower()


async def _list_stdio_tools(cfg: MCPServerConfig) -> list[str]:
    """List tools from a stdio MCP server."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = os.environ.copy()
    env.update({key: value for key, value in (cfg.env or {}).items() if value})
    params = StdioServerParameters(command=cfg.command, args=cfg.args, env=env)
    tool_names: list[str] | None = None
    try:
        async with AsyncExitStack() as stack:
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await asyncio.wait_for(session.initialize(), timeout=15)
            tools = await asyncio.wait_for(session.list_tools(), timeout=15)
            tool_names = [tool.name for tool in tools.tools]
    except BaseException as exc:
        if tool_names is not None and _is_ignorable_stdio_shutdown_error(exc):
            return tool_names
        raise
    return tool_names or []


def _is_ignorable_stdio_shutdown_error(exc: BaseException) -> bool:
    """Treat known stdio teardown races as non-fatal after tool discovery succeeded."""
    nested = getattr(exc, "exceptions", None)
    if isinstance(nested, (list, tuple)):
        children = [item for item in nested if isinstance(item, BaseException)]
        return bool(children) and all(_is_ignorable_stdio_shutdown_error(item) for item in children)
    return exc.__class__.__name__ == "BrokenResourceError"


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
