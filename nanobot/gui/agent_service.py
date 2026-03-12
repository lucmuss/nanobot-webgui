"""Shared agent runtime used by the nanobot web GUI."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nanobot.agent.loop import AgentLoop
from nanobot.bus.queue import MessageBus
from nanobot.gui.auth import AdminUser
from nanobot.gui.config_service import GUIConfigService
from nanobot.session.manager import SessionManager


class GUIAgentService:
    """Create and reuse one direct agent runtime for the web GUI."""

    def __init__(self, config_service: GUIConfigService, logger: logging.Logger) -> None:
        self.config_service = config_service
        self.logger = logger
        self._agent: AgentLoop | None = None
        self._signature: tuple[int, int] | None = None
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        """Drop the cached runtime so the next chat rebuilds it from config."""
        self._agent = None
        self._signature = None

    async def send_message(self, admin: AdminUser, content: str) -> dict[str, Any]:
        """Send one chat message through the direct agent loop."""
        config = self.config_service.load()
        agent = await self._get_agent()
        self.logger.info("chat_send admin=%s session=%s", admin.username, self._session_key(admin))
        response = await agent.process_direct(
            content,
            session_key=self._session_key(admin),
            channel="web",
            chat_id=self._chat_id(admin),
        )
        self._ensure_assistant_message(agent.sessions, admin, response)
        return {
            "content": response,
            "usage": dict(agent.last_usage or {}),
            "provider": config.get_provider_name() or config.agents.defaults.provider,
            "model": config.agents.defaults.model,
        }

    async def get_chat_history(self, admin: AdminUser) -> list[dict[str, Any]]:
        """Return the filtered chat history for one admin."""
        session_manager = self._get_session_manager()
        session = session_manager.get_or_create(self._session_key(admin))
        history: list[dict[str, Any]] = []
        for message in session.messages:
            role = str(message.get("role", "")).strip()
            if role not in {"user", "assistant"}:
                continue
            content = _display_content(message.get("content", ""))
            if not content:
                continue
            history.append(
                {
                    "role": role,
                    "content": content,
                    "timestamp": message.get("timestamp", ""),
                }
            )
        return history

    async def clear_chat(self, admin: AdminUser) -> None:
        """Clear the stored direct chat session for one admin."""
        session_manager = self._get_session_manager()
        session = session_manager.get_or_create(self._session_key(admin))
        session.clear()
        session_manager.save(session)
        self.logger.info("chat_cleared admin=%s session=%s", admin.username, self._session_key(admin))

    async def list_sessions(self) -> list[dict[str, Any]]:
        """List saved chat sessions in the workspace."""
        return self._get_session_manager().list_sessions()

    async def load_session_into_chat(self, admin: AdminUser, session_key: str) -> None:
        """Load a saved session into the main web chat session."""
        normalized_key = str(session_key or "").strip()
        if not normalized_key:
            raise ValueError("Choose a saved session first.")

        session_manager = self._get_session_manager()
        available = {item.get("key", "") for item in session_manager.list_sessions()}
        if normalized_key not in available:
            raise ValueError("That saved session could not be found.")

        source = session_manager.get_or_create(normalized_key)
        target = session_manager.get_or_create(self._session_key(admin))
        target.messages = [dict(message) for message in source.messages]
        target.metadata = dict(source.metadata)
        target.last_consolidated = int(source.last_consolidated or 0)
        target.updated_at = datetime.now()
        session_manager.save(target)
        self.logger.info(
            "chat_session_loaded admin=%s source=%s target=%s",
            admin.username,
            normalized_key,
            self._session_key(admin),
        )

    async def read_session_jsonl(self, session_key: str) -> str:
        """Return the raw JSONL contents for one saved session."""
        normalized_key = str(session_key or "").strip()
        if not normalized_key:
            raise ValueError("Choose a saved session first.")

        session_manager = self._get_session_manager()
        available = {item.get("key", "") for item in session_manager.list_sessions()}
        if normalized_key not in available:
            raise ValueError("That saved session could not be found.")

        path = session_manager._get_session_path(normalized_key)
        if not path.exists():
            raise ValueError("The saved session file is not available on disk.")
        return path.read_text(encoding="utf-8", errors="replace")

    async def get_recent_tool_activity(self, admin: AdminUser, limit: int = 10) -> list[dict[str, Any]]:
        """Return the most recent tool calls from the current direct chat session."""
        session_manager = self._get_session_manager()
        session = session_manager.get_or_create(self._session_key(admin))
        activity: list[dict[str, Any]] = []
        for message in reversed(session.messages):
            tool_calls = message.get("tool_calls")
            if not isinstance(tool_calls, list):
                continue
            timestamp = str(message.get("timestamp", "")).strip()
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
                name = str(function.get("name", "")).strip()
                if not name:
                    continue
                activity.append({"name": name, "timestamp": timestamp})
                if len(activity) >= limit:
                    return activity
        return activity

    async def get_mcp_test_history(self, admin: AdminUser, server_name: str) -> list[dict[str, Any]]:
        """Return the stored MCP test chat history for one server."""
        session_manager = self._get_session_manager()
        session = session_manager.get_or_create(self._mcp_test_session_key(admin, server_name))
        history: list[dict[str, Any]] = []
        for message in session.messages:
            role = str(message.get("role", "")).strip()
            if role not in {"user", "assistant"}:
                continue
            content = _display_content(message.get("content", ""))
            if not content:
                continue
            history.append(
                {
                    "role": role,
                    "content": content,
                    "timestamp": message.get("timestamp", ""),
                }
            )
        return history

    async def send_mcp_test_message(self, admin: AdminUser, server_name: str, content: str) -> dict[str, Any]:
        """Send a message through a one-off runtime that only loads the selected MCP server."""
        config = self.config_service.load()
        target_server = config.tools.mcp_servers.get(server_name)
        if target_server is None:
            raise ValueError(f"MCP server '{server_name}' is not registered.")

        workspace = config.workspace_path
        workspace.mkdir(parents=True, exist_ok=True)
        provider = _make_provider(config)
        bus = MessageBus()
        session_manager = SessionManager(workspace)
        agent = self._build_agent(
            config=config,
            provider=provider,
            bus=bus,
            session_manager=session_manager,
            mcp_servers={server_name: target_server},
        )
        try:
            response = await agent.process_direct(
                content,
                session_key=self._mcp_test_session_key(admin, server_name),
                channel="web",
                chat_id=self._mcp_test_chat_id(admin, server_name),
            )
            self._ensure_assistant_message(session_manager, admin, response, server_name=server_name)
            return {
                "content": response,
                "usage": dict(agent.last_usage or {}),
                "provider": config.get_provider_name() or config.agents.defaults.provider,
                "model": config.agents.defaults.model,
            }
        finally:
            await agent.close_mcp()

    async def clear_mcp_test(self, admin: AdminUser, server_name: str) -> None:
        """Clear one MCP test chat session."""
        session_manager = self._get_session_manager()
        session = session_manager.get_or_create(self._mcp_test_session_key(admin, server_name))
        session.clear()
        session_manager.save(session)
        self.logger.info(
            "mcp_test_chat_cleared admin=%s server=%s session=%s",
            admin.username,
            server_name,
            self._mcp_test_session_key(admin, server_name),
        )

    async def check_runtime(self) -> dict[str, Any]:
        """Run a small provider round-trip and return a health summary."""
        config = self.config_service.load()
        provider_name = config.get_provider_name() or config.agents.defaults.provider
        model = config.agents.defaults.model
        started = time.perf_counter()

        try:
            provider = _make_provider(config)
            response = await provider.chat(
                messages=[
                    {
                        "role": "user",
                        "content": "Reply with exactly OK.",
                    }
                ],
                model=model,
                max_tokens=16,
                temperature=max(config.agents.defaults.temperature, 0.1),
                reasoning_effort=config.agents.defaults.reasoning_effort,
            )
        except Exception as exc:
            result = {
                "ok": False,
                "provider": provider_name,
                "model": model,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "error": str(exc).strip() or f"{type(exc).__name__}",
                "checked_at": _utc_now(),
            }
            self.logger.warning(
                "agent_health_failed provider=%s model=%s error=%s",
                provider_name,
                model,
                result["error"],
            )
            return result

        content = (response.content or "").strip()
        result = {
            "ok": True,
            "provider": provider_name,
            "model": model,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "checked_at": _utc_now(),
            "usage": dict(response.usage or {}),
            "preview": content[:120],
        }
        self.logger.info(
            "agent_health_ok provider=%s model=%s latency_ms=%s",
            provider_name,
            model,
            result["latency_ms"],
        )
        return result

    async def plan_mcp_install(self, repository_bundle: dict[str, Any]) -> dict[str, Any]:
        """Ask the configured model for a bounded MCP install plan in JSON form."""
        config = self.config_service.load()
        provider = _make_provider(config)
        prompt = _build_mcp_install_planner_prompt(repository_bundle)
        response = await provider.chat(
            messages=[
                {"role": "system", "content": _MCP_INSTALL_PLANNER_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            model=config.agents.defaults.model,
            max_tokens=1600,
            temperature=0.1,
            reasoning_effort=config.agents.defaults.reasoning_effort,
        )
        payload = _extract_json_object((response.content or "").strip())
        if not isinstance(payload, dict):
            raise ValueError("The AI install planner did not return a JSON object.")
        return payload

    async def plan_mcp_repair(self, repair_bundle: dict[str, Any]) -> dict[str, Any]:
        """Ask the configured model for a bounded MCP repair plan in JSON form."""
        config = self.config_service.load()
        provider = _make_provider(config)
        prompt = _build_mcp_repair_planner_prompt(repair_bundle)
        response = await provider.chat(
            messages=[
                {"role": "system", "content": _MCP_REPAIR_PLANNER_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            model=config.agents.defaults.model,
            max_tokens=1000,
            temperature=0.1,
            reasoning_effort=config.agents.defaults.reasoning_effort,
        )
        payload = _extract_json_object((response.content or "").strip())
        if not isinstance(payload, dict):
            raise ValueError("The AI repair planner did not return a JSON object.")
        return payload

    async def _get_agent(self) -> AgentLoop:
        """Return a cached agent runtime, rebuilding it when config changes."""
        signature = self._config_signature()
        if self._agent is not None and self._signature == signature:
            return self._agent

        async with self._lock:
            signature = self._config_signature()
            if self._agent is not None and self._signature == signature:
                return self._agent

            config = self.config_service.load()
            workspace = config.workspace_path
            workspace.mkdir(parents=True, exist_ok=True)
            provider = _make_provider(config)
            bus = MessageBus()
            session_manager = SessionManager(workspace)
            agent = self._build_agent(
                config=config,
                provider=provider,
                bus=bus,
                session_manager=session_manager,
                mcp_servers=self.config_service.enabled_mcp_servers(config.tools.mcp_servers),
            )
            self._agent = agent
            self._signature = signature
            self.logger.info("agent_runtime_ready model=%s provider=%s", agent.model, config.agents.defaults.provider)
            return agent

    def _get_session_manager(self) -> SessionManager:
        """Return a session manager for the active workspace."""
        config = self.config_service.load()
        workspace = config.workspace_path
        workspace.mkdir(parents=True, exist_ok=True)
        return SessionManager(workspace)

    def _ensure_assistant_message(
        self,
        session_manager: SessionManager,
        admin: AdminUser,
        response: str,
        *,
        server_name: str | None = None,
    ) -> None:
        """Persist assistant error responses that the core may skip on failure."""
        clean_response = response.strip()
        if not clean_response:
            return

        session_key = (
            self._mcp_test_session_key(admin, server_name)
            if server_name is not None
            else self._session_key(admin)
        )
        session = session_manager.get_or_create(session_key)
        if session.messages:
            last_message = session.messages[-1]
            if last_message.get("role") == "assistant" and _display_content(last_message.get("content", "")) == clean_response:
                return

        session.add_message("assistant", clean_response)
        session_manager.save(session)

    @staticmethod
    def _build_agent(
        *,
        config,
        provider,
        bus: MessageBus,
        session_manager: SessionManager,
        mcp_servers: dict[str, Any],
    ) -> AgentLoop:
        """Construct an AgentLoop for the GUI with the provided MCP set."""
        return AgentLoop(
            bus=bus,
            provider=provider,
            workspace=config.workspace_path,
            model=config.agents.defaults.model,
            temperature=config.agents.defaults.temperature,
            max_tokens=config.agents.defaults.max_tokens,
            max_iterations=config.agents.defaults.max_tool_iterations,
            memory_window=config.agents.defaults.memory_window,
            reasoning_effort=config.agents.defaults.reasoning_effort,
            brave_api_key=config.tools.web.search.api_key or None,
            web_proxy=config.tools.web.proxy or None,
            exec_config=config.tools.exec,
            tools_enabled=config.tools.enabled,
            restrict_to_workspace=config.tools.restrict_to_workspace,
            session_manager=session_manager,
            mcp_servers=mcp_servers,
            channels_config=config.channels,
        )

    def _config_signature(self) -> tuple[int, int]:
        """Return a cheap change signature for the active config file."""
        if not self.config_service.config_path.exists():
            return (0, 0)
        stat = self.config_service.config_path.stat()
        return (stat.st_mtime_ns, stat.st_size)

    @staticmethod
    def _session_key(admin: AdminUser) -> str:
        """Return the direct session key for one admin."""
        return f"web:admin-{admin.id}"

    @staticmethod
    def _chat_id(admin: AdminUser) -> str:
        """Return the chat identifier used by the direct agent runtime."""
        return f"admin-{admin.id}"

    @staticmethod
    def _mcp_test_session_key(admin: AdminUser, server_name: str | None) -> str:
        """Return the dedicated MCP test session key."""
        target = server_name or "unknown"
        return f"web:mcp-test:{target}:admin-{admin.id}"

    @staticmethod
    def _mcp_test_chat_id(admin: AdminUser, server_name: str | None) -> str:
        """Return the dedicated MCP test chat identifier."""
        target = server_name or "unknown"
        return f"mcp-test-{target}-admin-{admin.id}"


def _make_provider(config):
    """Create the provider matching the active config."""
    from nanobot.providers.azure_openai_provider import AzureOpenAIProvider
    from nanobot.providers.custom_provider import CustomProvider
    from nanobot.providers.litellm_provider import LiteLLMProvider
    from nanobot.providers.openai_codex_provider import OpenAICodexProvider
    from nanobot.providers.registry import find_by_name

    model = config.agents.defaults.model
    provider_name = config.get_provider_name(model)
    provider_config = config.get_provider(model)

    if provider_name == "openai_codex" or model.startswith("openai-codex/"):
        return OpenAICodexProvider(default_model=model)

    if provider_name == "custom":
        return CustomProvider(
            api_key=provider_config.api_key if provider_config else "no-key",
            api_base=config.get_api_base(model) or "http://localhost:8000/v1",
            default_model=model,
        )

    if provider_name == "azure_openai":
        if not provider_config or not provider_config.api_key or not provider_config.api_base:
            raise ValueError("Azure OpenAI requires both api_key and api_base.")
        return AzureOpenAIProvider(
            api_key=provider_config.api_key,
            api_base=provider_config.api_base,
            default_model=model,
        )

    provider_spec = find_by_name(provider_name) if provider_name else None
    if (
        not model.startswith("bedrock/")
        and not (provider_config and provider_config.api_key)
        and not (provider_spec and provider_spec.is_oauth)
    ):
        raise ValueError("No API key is configured for the selected provider.")

    return LiteLLMProvider(
        api_key=provider_config.api_key if provider_config else None,
        api_base=config.get_api_base(model),
        default_model=model,
        extra_headers=provider_config.extra_headers if provider_config else None,
        provider_name=provider_name,
    )


def _display_content(content: Any) -> str:
    """Convert stored session payloads into plain text for the chat UI."""
    def _strip_hidden_context(text: str) -> str:
        return re.sub(r"\[nanobot_community_context\][\s\S]*?\[/nanobot_community_context\]\s*", "", text).strip()

    if isinstance(content, str):
        return _strip_hidden_context(content)
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    text = str(item.get("text", "")).strip()
                    if text:
                        parts.append(_strip_hidden_context(text))
                elif item.get("type") == "image_url":
                    parts.append("[image]")
        return "\n".join(parts).strip()
    return _strip_hidden_context(str(content))


def _utc_now() -> str:
    """Return a compact UTC timestamp string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_MCP_INSTALL_PLANNER_SYSTEM_PROMPT = """You are a nanobot MCP install planner.

Return exactly one JSON object and no prose.

Your job:
- inspect the provided repository bundle
- infer a safe MCP install plan
- never invent package names, file paths, or env variables that are not supported by the evidence

Allowed values:
- install_mode: source, npm, workspace_package, remote, oci
- repo_type: npm, python, docker, remote, monorepo, server_json, unknown
- transport: stdio, sse, streamableHttp
- runtime names: node, npm, npx, python, uv, pip, docker, uvx
- run_command: npx, node, python, python3, uv, uvx, docker, or empty string for remote-only

Allowed install commands only:
- ["npm", "ci"]
- ["npm", "install"]
- ["npm", "run", "build"]
- ["uv", "pip", "install", "-e", "."]
- ["uv", "sync"]
- ["pip", "install", "-e", "."]
- ["python", "-m", "pip", "install", "-e", "."]
- ["python3", "-m", "pip", "install", "-e", "."]

JSON schema:
{
  "repo_type": "...",
  "install_mode": "...",
  "transport": "...",
  "runtime": ["..."],
  "run_command": "...",
  "run_args": ["..."],
  "run_url": "",
  "install_steps": [
    {"display": "npm ci", "command": ["npm", "ci"], "timeout": 900}
  ],
  "required_env": ["OPENAI_API_KEY"],
  "optional_env": [],
  "server_name": "example-mcp",
  "summary": "short summary",
  "evidence": ["package.json name=..."],
  "confidence": 0.0
}

If uncertain:
- keep confidence low
- prefer empty arrays over guesses
- do not emit commands outside the allowlist
"""


_MCP_REPAIR_PLANNER_SYSTEM_PROMPT = """You are a nanobot MCP repair planner.

Return exactly one JSON object and no prose.

Your job:
- inspect the provided MCP runtime evidence
- decide which bounded repair recipe is the safest next step
- only recommend unrestricted_agent_shell when the input explicitly says unrestricted mode is enabled

Allowed recommended_recipe values:
- ""
- install_node
- install_uv
- install_python_build_tools
- install_docker_cli
- unrestricted_agent_shell

JSON schema:
{
  "missing_runtime": "node",
  "recommended_recipe": "install_node",
  "required_env": ["OPENAI_API_KEY"],
  "next_step": "Apply the repair, then retest the MCP.",
  "confidence": 0.0,
  "shell_command": ""
}

Rules:
- keep shell_command empty unless recommended_recipe is unrestricted_agent_shell
- do not emit prose outside the JSON object
- do not suggest any recipe that is not in the allowlist
"""


def _build_mcp_install_planner_prompt(repository_bundle: dict[str, Any]) -> str:
    """Render one bounded repository bundle for the AI install planner."""
    return (
        "Plan a safe MCP installation from this repository bundle.\n"
        "Only use the bundled evidence and return JSON only.\n\n"
        + json.dumps(repository_bundle, indent=2, ensure_ascii=False)
    )


def _build_mcp_repair_planner_prompt(repair_bundle: dict[str, Any]) -> str:
    """Render one bounded MCP repair bundle for the AI repair planner."""
    return (
        "Plan the safest next MCP repair step from this runtime evidence.\n"
        "Only use the bundled evidence and return JSON only.\n\n"
        + json.dumps(repair_bundle, indent=2, ensure_ascii=False)
    )


def _extract_json_object(raw: str) -> dict[str, Any]:
    """Extract one JSON object from plain text or a fenced markdown block."""
    text = raw.strip()
    if not text:
        raise ValueError("The AI install planner returned an empty response.")

    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    elif not text.startswith("{"):
        match = re.search(r"(\{.*\})", text, flags=re.DOTALL)
        if match:
            text = match.group(1).strip()

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("The AI install planner returned invalid JSON.") from exc

    if not isinstance(payload, dict):
        raise ValueError("The AI install planner must return a JSON object.")
    return payload
