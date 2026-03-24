"""Compatibility helpers for running the WebGUI against upstream nanobot."""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any


def get_tools_enabled(config: Any) -> bool:
    """Return whether tools are enabled, defaulting to True on upstream configs."""
    tools = getattr(config, "tools", None)
    return bool(getattr(tools, "enabled", True))


def set_tools_enabled(config: Any, enabled: bool) -> None:
    """Persist the tools-enabled flag when the upstream config supports it."""
    tools = getattr(config, "tools", None)
    if tools is None or not hasattr(tools, "enabled"):
        return
    setattr(tools, "enabled", bool(enabled))


def supports_tools_enabled(config: Any) -> bool:
    """Return whether the loaded config model exposes a tools-enabled toggle."""
    tools = getattr(config, "tools", None)
    return bool(tools is not None and hasattr(tools, "enabled"))


def get_agent_default(config: Any, field_name: str, default: Any = None) -> Any:
    """Read one agent default field across newer and older config shapes."""
    agents = getattr(config, "agents", None)
    defaults = getattr(agents, "defaults", None)
    if defaults is None and isinstance(agents, dict):
        defaults = agents.get("defaults")
    if defaults is None:
        return default
    if isinstance(defaults, dict):
        if field_name in defaults:
            return defaults[field_name]
        alias = _camel_case(field_name)
        return defaults.get(alias, default)
    return getattr(defaults, field_name, default)


def set_agent_default(config: Any, field_name: str, value: Any) -> None:
    """Write one agent default field only when the active config schema supports it."""
    agents = getattr(config, "agents", None)
    defaults = getattr(agents, "defaults", None)
    if defaults is None and isinstance(agents, dict):
        defaults = agents.get("defaults")
    if defaults is None:
        return
    if isinstance(defaults, dict):
        alias = _camel_case(field_name)
        if field_name in defaults:
            key = field_name
        elif alias in defaults:
            key = alias
        else:
            key = alias
        defaults[key] = value
        return
    if hasattr(defaults, field_name):
        setattr(defaults, field_name, value)


def get_channel_field(config: Any, channel_name: str, field_name: str, default: Any = None) -> Any:
    """Read a channel field from either an upstream dict config or a legacy object config."""
    channel_cfg = getattr(getattr(config, "channels", None), channel_name, None)
    if channel_cfg is None:
        return default
    if isinstance(channel_cfg, dict):
        if field_name in channel_cfg:
            return channel_cfg[field_name]
        alias = _camel_case(field_name)
        return channel_cfg.get(alias, default)
    return getattr(channel_cfg, field_name, default)


def set_channel_field(config: Any, channel_name: str, field_name: str, value: Any) -> None:
    """Write a channel field while preserving upstream camelCase keys when present."""
    channels = getattr(config, "channels", None)
    if channels is None:
        return

    channel_cfg = getattr(channels, channel_name, None)
    if channel_cfg is None:
        channel_cfg = {}
        setattr(channels, channel_name, channel_cfg)

    if isinstance(channel_cfg, dict):
        alias = _camel_case(field_name)
        if field_name in channel_cfg:
            key = field_name
        elif alias in channel_cfg:
            key = alias
        else:
            key = alias
        channel_cfg[key] = value
        return

    setattr(channel_cfg, field_name, value)


def is_channel_enabled(config: Any, channel_name: str) -> bool:
    """Return whether one channel is enabled across supported config shapes."""
    return bool(get_channel_field(config, channel_name, "enabled", False))


def get_agent_usage(agent: Any) -> dict[str, Any]:
    """Return the most recent token-usage summary when available."""
    usage = getattr(agent, "last_usage", {})
    return dict(usage or {})


def build_agent_loop_kwargs(base_kwargs: dict[str, Any]) -> dict[str, Any]:
    """Filter optional AgentLoop kwargs so upstream main stays compatible."""
    from nanobot.agent.loop import AgentLoop

    signature = inspect.signature(AgentLoop)
    allowed = set(signature.parameters)
    return {key: value for key, value in base_kwargs.items() if key in allowed}


def enrich_session_summaries(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Backfill session preview fields when upstream SessionManager does not expose them."""
    enriched: list[dict[str, Any]] = []
    for item in sessions:
        session = dict(item)
        path_value = str(session.get("path", "")).strip()
        if path_value and _needs_preview_fields(session):
            preview = _read_session_preview(Path(path_value))
            session.update(preview)
        session.setdefault("session_type", _session_type(str(session.get("key", ""))))
        session.setdefault("message_count", 0)
        session.setdefault("last_user", "")
        session.setdefault("last_assistant", "")
        enriched.append(session)
    return enriched


def _needs_preview_fields(session: dict[str, Any]) -> bool:
    return any(key not in session for key in ("session_type", "message_count", "last_user", "last_assistant"))


def _read_session_preview(path: Path) -> dict[str, Any]:
    preview_messages: list[dict[str, Any]] = []
    if not path.exists():
        return {
            "message_count": 0,
            "last_user": "",
            "last_assistant": "",
        }

    try:
        with open(path, encoding="utf-8") as handle:
            first_line = handle.readline().strip()
            if not first_line:
                return {}
            metadata = json.loads(first_line)
            if metadata.get("_type") != "metadata":
                return {}
            for line in handle:
                raw = line.strip()
                if not raw:
                    continue
                payload = json.loads(raw)
                if isinstance(payload, dict):
                    preview_messages.append(payload)
    except Exception:
        return {
            "message_count": 0,
            "last_user": "",
            "last_assistant": "",
        }

    last_user = next(
        (_preview_content(message.get("content", "")) for message in reversed(preview_messages) if message.get("role") == "user"),
        "",
    )
    last_assistant = next(
        (_preview_content(message.get("content", "")) for message in reversed(preview_messages) if message.get("role") == "assistant"),
        "",
    )
    return {
        "message_count": len(preview_messages),
        "last_user": last_user,
        "last_assistant": last_assistant,
    }


def _session_type(key: str) -> str:
    if key.startswith("web:mcp-test:"):
        return "MCP test"
    if key.startswith("web:"):
        return "Web chat"
    if key.startswith("cli:"):
        return "CLI"
    return "Other"


def _camel_case(value: str) -> str:
    parts = str(value).split("_")
    if not parts:
        return ""
    return parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:])


def _preview_content(content: Any, limit: int = 180) -> str:
    if isinstance(content, list):
        text_parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_parts.append(str(item.get("text", "")).strip())
        preview = " ".join(part for part in text_parts if part)
    else:
        preview = str(content or "").strip()

    preview = " ".join(preview.split())
    if len(preview) > limit:
        return preview[: limit - 1] + "..."
    return preview
