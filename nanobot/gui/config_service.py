"""Config helpers used by the nanobot web GUI."""

from __future__ import annotations

import re
import json
from datetime import datetime
from importlib.resources import files as package_files
from pathlib import Path
from typing import Any

from nanobot.config.loader import load_config, save_config, set_config_path
from nanobot.config.schema import Config
from nanobot.utils.helpers import sync_workspace_templates


class GUIConfigService:
    """Manage the GUI instance's config file and workspace."""

    def __init__(self, config_path: Path, workspace_override: str | None = None) -> None:
        self.config_path = config_path
        self.workspace_override = workspace_override

    @property
    def runtime_dir(self) -> Path:
        """Return the instance data directory derived from the chosen config path."""
        return self.config_path.parent

    @property
    def state_path(self) -> Path:
        """Return the GUI state file path."""
        return self.runtime_dir / "gui-state.json"

    @property
    def media_dir(self) -> Path:
        """Return the GUI media directory."""
        return self.runtime_dir / "media"

    @property
    def avatars_dir(self) -> Path:
        """Return the avatar upload directory."""
        return self.media_dir / "avatars"

    @property
    def branding_dir(self) -> Path:
        """Return the directory used for bundled branding assets."""
        return self.media_dir / "branding"

    @property
    def branding_banner_path(self) -> Path:
        """Return the public banner image path used by the GUI shell."""
        return self.branding_dir / "nanobot-webgui-banner.png"

    @property
    def mcp_installs_dir(self) -> Path:
        """Return the directory used for GUI-managed MCP checkouts."""
        return self.default_workspace / "mcp-installs"

    @property
    def uploads_dir(self) -> Path:
        """Return the directory used for chat-uploaded files."""
        return self.default_workspace / "uploads"

    @property
    def default_workspace(self) -> Path:
        """Return the effective workspace path for this GUI instance."""
        if self.workspace_override:
            return Path(self.workspace_override).expanduser()
        return Path.home() / ".nanobot" / "workspace"

    def ensure_instance(self) -> Config:
        """Ensure the instance has a config file, workspace, and default templates."""
        set_config_path(self.config_path)
        config = load_config(self.config_path if self.config_path.exists() else None)

        if self.workspace_override:
            config.agents.defaults.workspace = str(self.default_workspace)

        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.default_workspace.mkdir(parents=True, exist_ok=True)
        self.avatars_dir.mkdir(parents=True, exist_ok=True)
        self.branding_dir.mkdir(parents=True, exist_ok=True)
        self.mcp_installs_dir.mkdir(parents=True, exist_ok=True)
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        self._sync_branding_assets()
        sync_workspace_templates(self.default_workspace)
        save_config(config, self.config_path)
        self._ensure_state_file()
        return config

    def load(self) -> Config:
        """Load the active instance configuration."""
        set_config_path(self.config_path)
        config = load_config(self.config_path if self.config_path.exists() else None)
        if self.workspace_override:
            config.agents.defaults.workspace = str(self.default_workspace)
        return config

    def save(self, config: Config) -> Config:
        """Persist the config and keep the workspace templates in sync."""
        if self.workspace_override:
            config.agents.defaults.workspace = str(self.default_workspace)

        self.default_workspace.mkdir(parents=True, exist_ok=True)
        self.avatars_dir.mkdir(parents=True, exist_ok=True)
        self.branding_dir.mkdir(parents=True, exist_ok=True)
        self.mcp_installs_dir.mkdir(parents=True, exist_ok=True)
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        self._sync_branding_assets()
        sync_workspace_templates(self.default_workspace)
        set_config_path(self.config_path)
        save_config(config, self.config_path)
        return config

    def load_state(self) -> dict[str, Any]:
        """Load the GUI state JSON file."""
        self._ensure_state_file()
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def save_state(self, state: dict[str, Any]) -> None:
        """Persist the GUI state JSON file."""
        merged = self._normalize_state(state)
        self.state_path.write_text(json.dumps(merged, indent=2), encoding="utf-8")

    def is_setup_complete(self) -> bool:
        """Return True once the onboarding wizard has been completed."""
        state = self.load_state()
        return bool(state.get("setup_complete"))

    def set_setup_complete(self, value: bool) -> None:
        """Persist the onboarding completion flag."""
        state = self.load_state()
        state["setup_complete"] = bool(value)
        self.save_state(state)

    def is_safe_mode(self) -> bool:
        """Return whether the beginner-friendly safe mode is enabled."""
        state = self.load_state()
        return bool(state.get("safe_mode", True))

    def set_safe_mode(self, value: bool) -> None:
        """Persist the safe mode toggle."""
        state = self.load_state()
        state["safe_mode"] = bool(value)
        self.save_state(state)

    def is_unrestricted_agent_shell_enabled(self) -> bool:
        """Return whether dangerous unrestricted MCP repair mode is enabled."""
        state = self.load_state()
        return bool(state.get("unrestricted_agent_shell_enabled", False))

    def set_unrestricted_agent_shell_enabled(self, value: bool) -> None:
        """Persist the dangerous unrestricted MCP repair mode toggle."""
        state = self.load_state()
        state["unrestricted_agent_shell_enabled"] = bool(value)
        self.save_state(state)

    def get_agent_health(self) -> dict[str, Any]:
        """Return the last stored GUI agent health result."""
        state = self.load_state()
        health = state.get("agent_health")
        return health if isinstance(health, dict) else {}

    def set_agent_health(self, health: dict[str, Any]) -> None:
        """Persist the last GUI agent health result."""
        state = self.load_state()
        state["agent_health"] = health
        self.save_state(state)

    def get_mcp_registry(self) -> dict[str, dict[str, Any]]:
        """Return GUI-managed MCP metadata indexed by server name."""
        state = self.load_state()
        registry = state.get("mcp_registry")
        if not isinstance(registry, dict):
            return {}
        return {str(name): data for name, data in registry.items() if isinstance(data, dict)}

    def get_mcp_record(self, server_name: str) -> dict[str, Any]:
        """Return one MCP metadata record if it exists."""
        return dict(self.get_mcp_registry().get(server_name, {}))

    def is_mcp_enabled(self, server_name: str) -> bool:
        """Return whether one MCP server is enabled for normal chat runtime use."""
        return bool(self.get_mcp_record(server_name).get("enabled"))

    def set_mcp_record(self, server_name: str, record: dict[str, Any]) -> None:
        """Store GUI metadata for one MCP server."""
        state = self.load_state()
        registry = state.get("mcp_registry")
        if not isinstance(registry, dict):
            registry = {}
        registry[str(server_name)] = record
        state["mcp_registry"] = registry
        self.save_state(state)

    def set_mcp_enabled(self, server_name: str, enabled: bool) -> dict[str, Any]:
        """Toggle whether one MCP server participates in the default agent runtime."""
        record = self.get_mcp_record(server_name)
        record["enabled"] = bool(enabled)
        self.set_mcp_record(server_name, record)
        return record

    def remove_mcp_record(self, server_name: str) -> None:
        """Delete GUI metadata for one MCP server."""
        state = self.load_state()
        registry = state.get("mcp_registry")
        if isinstance(registry, dict):
            registry.pop(str(server_name), None)
            state["mcp_registry"] = registry
        self.save_state(state)

    def enabled_mcp_servers(self, servers: dict[str, Any]) -> dict[str, Any]:
        """Filter configured MCP servers down to the enabled set."""
        enabled: dict[str, Any] = {}
        for name, server in servers.items():
            if self.is_mcp_enabled(str(name)):
                enabled[str(name)] = server
        return enabled

    def get_last_successful_chat(self) -> dict[str, Any]:
        """Return the last successful GUI chat turn."""
        state = self.load_state()
        value = state.get("last_successful_chat")
        return value if isinstance(value, dict) else {}

    def set_last_successful_chat(self, payload: dict[str, Any]) -> None:
        """Persist the last successful GUI chat turn."""
        state = self.load_state()
        state["last_successful_chat"] = payload
        self.save_state(state)

    def get_last_error(self) -> dict[str, Any]:
        """Return the last user-facing error stored by the GUI."""
        state = self.load_state()
        value = state.get("last_error")
        return value if isinstance(value, dict) else {}

    def set_last_error(self, payload: dict[str, Any]) -> None:
        """Persist the latest user-facing error."""
        state = self.load_state()
        state["last_error"] = payload
        self.save_state(state)

    def clear_last_error(self) -> None:
        """Clear the stored GUI error once the runtime is healthy again."""
        state = self.load_state()
        state["last_error"] = {}
        self.save_state(state)

    def get_last_restart_at(self) -> str:
        """Return the last recorded restart timestamp."""
        state = self.load_state()
        value = state.get("last_restart_at")
        return str(value) if value else ""

    def set_last_restart_at(self, value: str) -> None:
        """Persist the last restart request timestamp."""
        state = self.load_state()
        state["last_restart_at"] = value
        self.save_state(state)

    def get_update_status(self) -> dict[str, Any]:
        """Return the cached GUI update-check metadata."""
        state = self.load_state()
        value = state.get("update_status")
        return value if isinstance(value, dict) else {}

    def set_update_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Persist the cached GUI update-check metadata."""
        state = self.load_state()
        clean = {
            "enabled": bool(payload.get("enabled", False)),
            "current_version": str(payload.get("current_version", "")),
            "latest_version": str(payload.get("latest_version", "")),
            "tag_name": str(payload.get("tag_name", "")),
            "available": bool(payload.get("available", False)),
            "checked_at": str(payload.get("checked_at", "")),
            "release_url": str(payload.get("release_url", "")),
            "release_notes_url": str(payload.get("release_notes_url", "")),
            "release_name": str(payload.get("release_name", "")),
            "published_at": str(payload.get("published_at", "")),
            "source": str(payload.get("source", "")),
            "repo": str(payload.get("repo", "")),
            "error": str(payload.get("error", "")),
            "updating": bool(payload.get("updating", False)),
            "last_update_request_at": str(payload.get("last_update_request_at", "")),
            "last_update_error": str(payload.get("last_update_error", "")),
        }
        state["update_status"] = clean
        self.save_state(state)
        return clean

    def get_community_preferences(self) -> dict[str, bool]:
        """Return the current community integration preferences."""
        state = self.load_state()
        value = state.get("community_preferences")
        if not isinstance(value, dict):
            value = {}
        return {
            "share_anonymous_metrics": bool(value.get("share_anonymous_metrics", False)),
            "receive_recommendations": bool(value.get("receive_recommendations", True)),
            "show_marketplace_stats": bool(value.get("show_marketplace_stats", True)),
            "allow_public_mcp_submissions": bool(value.get("allow_public_mcp_submissions", False)),
        }

    def set_community_preferences(
        self,
        *,
        share_anonymous_metrics: bool,
        receive_recommendations: bool,
        show_marketplace_stats: bool,
        allow_public_mcp_submissions: bool,
    ) -> dict[str, bool]:
        """Persist the current community integration preferences."""
        state = self.load_state()
        payload = {
            "share_anonymous_metrics": bool(share_anonymous_metrics),
            "receive_recommendations": bool(receive_recommendations),
            "show_marketplace_stats": bool(show_marketplace_stats),
            "allow_public_mcp_submissions": bool(allow_public_mcp_submissions),
        }
        state["community_preferences"] = payload
        self.save_state(state)
        return payload

    def get_active_memory_doc(self) -> str:
        """Return the most recently opened memory document key."""
        state = self.load_state()
        value = state.get("active_memory_doc")
        return str(value) if value else "memory"

    def set_active_memory_doc(self, key: str) -> None:
        """Persist the most recently opened memory document key."""
        state = self.load_state()
        state["active_memory_doc"] = key
        self.save_state(state)

    def get_last_mcp_test(self) -> dict[str, Any]:
        """Return the last MCP test result shown on the dashboard."""
        state = self.load_state()
        value = state.get("last_mcp_test")
        return value if isinstance(value, dict) else {}

    def set_last_mcp_test(self, payload: dict[str, Any]) -> None:
        """Persist the latest MCP test result."""
        state = self.load_state()
        state["last_mcp_test"] = payload
        self.save_state(state)

    def get_usage_events(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Return recorded chat and health-check usage events, newest first."""
        state = self.load_state()
        raw_events = state.get("usage_events")
        if not isinstance(raw_events, list):
            return []

        events = [dict(item) for item in raw_events if isinstance(item, dict)]
        events = sorted(events, key=lambda item: str(item.get("timestamp", "")), reverse=True)
        if limit is not None:
            return events[:limit]
        return events

    def record_usage_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Append one usage event to GUI state and keep only the newest items."""
        state = self.load_state()
        events = state.get("usage_events")
        if not isinstance(events, list):
            events = []

        clean = {
            "timestamp": str(payload.get("timestamp", "")),
            "source": str(payload.get("source", "")),
            "provider": str(payload.get("provider", "")),
            "model": str(payload.get("model", "")),
            "prompt_tokens": int(payload.get("prompt_tokens", 0) or 0),
            "completion_tokens": int(payload.get("completion_tokens", 0) or 0),
            "total_tokens": int(payload.get("total_tokens", 0) or 0),
            "estimated_cost_usd": payload.get("estimated_cost_usd"),
            "note": str(payload.get("note", "")),
        }
        events.append(clean)
        state["usage_events"] = events[-300:]
        self.save_state(state)
        return clean

    def get_usage_summary(self) -> dict[str, Any]:
        """Return a compact usage summary for the usage page."""
        events = self.get_usage_events()
        recent_events = events[:25]
        last_24h = self._events_within_hours(events, hours=24)

        recent_models: list[str] = []
        for event in events:
            label = " / ".join(part for part in [event.get("provider", ""), event.get("model", "")] if part)
            if label and label not in recent_models:
                recent_models.append(label)

        return {
            "event_count": len(events),
            "totals_all_time": self._sum_usage(events),
            "totals_24h": self._sum_usage(last_24h),
            "recent_models": recent_models[:6],
            "last_event": events[0] if events else {},
            "recent_events": recent_events,
        }

    def recent_uploads(self, limit: int = 8) -> list[dict[str, Any]]:
        """Return the newest chat-uploaded files from the workspace."""
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        items = []
        for path in self.uploads_dir.iterdir():
            if not path.is_file():
                continue
            stat = path.stat()
            items.append(
                {
                    "name": path.name,
                    "path": str(path),
                    "relative_path": str(path.relative_to(self.default_workspace)),
                    "size_bytes": stat.st_size,
                    "size_label": self._format_size(stat.st_size),
                    "modified_at": self._format_timestamp(stat.st_mtime),
                }
            )
        items.sort(key=lambda item: item["modified_at"], reverse=True)
        return items[:limit]

    def markdown_documents(self) -> list[dict[str, str]]:
        """Return the editable workspace markdown files exposed in the GUI."""
        workspace = self.default_workspace
        return [
            {
                "key": "memory",
                "label": "Long-Term Memory",
                "group": "Memory",
                "description": "Long-term memory facts used by the agent.",
                "path": str(workspace / "memory" / "MEMORY.md"),
            },
            {
                "key": "history",
                "label": "Project Context / History",
                "group": "Project Context",
                "description": "Searchable running history and project context log.",
                "path": str(workspace / "memory" / "HISTORY.md"),
            },
            {
                "key": "agents",
                "label": "Instructions",
                "group": "Instructions",
                "description": "Primary behavior instructions loaded into the system prompt.",
                "path": str(workspace / "AGENTS.md"),
            },
            {
                "key": "heartbeat",
                "label": "Heartbeat Tasks",
                "group": "Automation",
                "description": "Recurring heartbeat guidance for the agent.",
                "path": str(workspace / "HEARTBEAT.md"),
            },
            {
                "key": "soul",
                "label": "Persona",
                "group": "Instructions",
                "description": "High-level persona and guiding behavior.",
                "path": str(workspace / "SOUL.md"),
            },
            {
                "key": "tools",
                "label": "Tool Notes",
                "group": "Instructions",
                "description": "Tool usage conventions and limits.",
                "path": str(workspace / "TOOLS.md"),
            },
            {
                "key": "user",
                "label": "User Profile",
                "group": "Project Context",
                "description": "User-specific preferences and working style.",
                "path": str(workspace / "USER.md"),
            },
        ]

    def get_markdown_document(self, key: str) -> dict[str, str]:
        """Return metadata for one editable markdown file."""
        for document in self.markdown_documents():
            if document["key"] == key:
                return document
        return self.markdown_documents()[0]

    def read_markdown_document(self, key: str) -> dict[str, str]:
        """Load one editable markdown file from the workspace."""
        self.default_workspace.mkdir(parents=True, exist_ok=True)
        sync_workspace_templates(self.default_workspace, silent=True)
        document = self.get_markdown_document(key)
        path = Path(document["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("", encoding="utf-8")
        stat = path.stat()
        return {
            **document,
            "content": path.read_text(encoding="utf-8"),
            "modified_at": self._format_timestamp(stat.st_mtime),
            "size_bytes": stat.st_size,
            "size_label": self._format_size(stat.st_size),
            "template_available": bool(self.get_markdown_template(key)),
        }

    def save_markdown_document(self, key: str, content: str) -> dict[str, str]:
        """Persist one editable markdown file."""
        document = self.get_markdown_document(key)
        path = Path(document["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        stat = path.stat()
        return {
            **document,
            "content": content,
            "modified_at": self._format_timestamp(stat.st_mtime),
            "size_bytes": stat.st_size,
            "size_label": self._format_size(stat.st_size),
            "template_available": bool(self.get_markdown_template(key)),
        }

    def get_markdown_template(self, key: str) -> str:
        """Return the bundled template text for one markdown document when available."""
        template_map = {
            "memory": ("templates", "memory", "MEMORY.md"),
            "history": None,
            "agents": ("templates", "AGENTS.md"),
            "heartbeat": ("templates", "HEARTBEAT.md"),
            "soul": ("templates", "SOUL.md"),
            "tools": ("templates", "TOOLS.md"),
            "user": ("templates", "USER.md"),
        }
        parts = template_map.get(key)
        if not parts:
            return ""
        try:
            resource = package_files("nanobot")
            for part in parts:
                resource = resource / part
            return resource.read_text(encoding="utf-8")
        except Exception:
            return ""

    def reset_markdown_document(self, key: str) -> dict[str, str]:
        """Reset one markdown file back to the bundled template or a blank file."""
        content = self.get_markdown_template(key)
        return self.save_markdown_document(key, content)

    def get_response_style(self) -> str:
        """Return the selected response-length preference from USER.md."""
        user_doc = self.read_markdown_document("user")["content"]
        mapping = {
            "brief": "Brief and concise",
            "detailed": "Detailed explanations",
            "adaptive": "Adaptive based on question",
        }
        for key, label in mapping.items():
            if f"- [x] {label}" in user_doc:
                return key
        return "adaptive"

    def set_response_style(self, value: str) -> None:
        """Update the response-length preference checkboxes inside USER.md."""
        mapping = {
            "brief": "Brief and concise",
            "detailed": "Detailed explanations",
            "adaptive": "Adaptive based on question",
        }
        selected = mapping.get(value, mapping["adaptive"])
        document = self.read_markdown_document("user")
        content = document["content"]
        for label in mapping.values():
            content = re.sub(
                rf"- \[[ xX]\] {re.escape(label)}",
                f"- [{'x' if label == selected else ' '}] {label}",
                content,
            )
        self.save_markdown_document("user", content)

    def _ensure_state_file(self) -> None:
        """Create the GUI state file when it does not exist yet."""
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        if self.state_path.exists():
            current = json.loads(self.state_path.read_text(encoding="utf-8"))
            normalized = self._normalize_state(current)
            if normalized != current:
                self.save_state(normalized)
            return
        self.save_state({"setup_complete": False})

    def _sync_branding_assets(self) -> None:
        """Copy packaged branding files into the public media directory."""
        try:
            assets_root = package_files("nanobot.gui").joinpath("assets", "branding")
        except ModuleNotFoundError:
            return

        for asset_name in ("nanobot-webgui-banner.png",):
            asset = assets_root.joinpath(asset_name)
            if not asset.is_file():
                continue
            destination = self.branding_dir / asset_name
            destination.write_bytes(asset.read_bytes())

    @staticmethod
    def _normalize_state(state: dict[str, Any]) -> dict[str, Any]:
        """Ensure the persisted GUI state always has the expected top-level keys."""
        normalized = dict(state)
        normalized.setdefault("setup_complete", False)
        normalized.setdefault("safe_mode", True)
        normalized.setdefault("unrestricted_agent_shell_enabled", False)
        if not isinstance(normalized.get("agent_health"), dict):
            normalized["agent_health"] = {}
        if not isinstance(normalized.get("mcp_registry"), dict):
            normalized["mcp_registry"] = {}
        if not isinstance(normalized.get("last_successful_chat"), dict):
            normalized["last_successful_chat"] = {}
        if not isinstance(normalized.get("last_error"), dict):
            normalized["last_error"] = {}
        if not isinstance(normalized.get("last_mcp_test"), dict):
            normalized["last_mcp_test"] = {}
        if not isinstance(normalized.get("usage_events"), list):
            normalized["usage_events"] = []
        if not isinstance(normalized.get("last_restart_at"), str):
            normalized["last_restart_at"] = ""
        if not isinstance(normalized.get("active_memory_doc"), str):
            normalized["active_memory_doc"] = "memory"
        if not isinstance(normalized.get("update_status"), dict):
            normalized["update_status"] = {}
        if not isinstance(normalized.get("community_preferences"), dict):
            normalized["community_preferences"] = {
                "share_anonymous_metrics": False,
                "receive_recommendations": True,
                "show_marketplace_stats": True,
                "allow_public_mcp_submissions": False,
            }
        else:
            normalized["community_preferences"].setdefault("allow_public_mcp_submissions", False)
        return normalized

    @staticmethod
    def _format_timestamp(value: float) -> str:
        """Format one POSIX timestamp for the GUI."""
        return datetime.fromtimestamp(value).isoformat(timespec="seconds")

    @staticmethod
    def _events_within_hours(events: list[dict[str, Any]], *, hours: int) -> list[dict[str, Any]]:
        """Filter usage events down to a moving time window."""
        cutoff = datetime.now().timestamp() - (hours * 3600)
        selected = []
        for event in events:
            try:
                stamp = datetime.fromisoformat(str(event.get("timestamp", ""))).timestamp()
            except ValueError:
                continue
            if stamp >= cutoff:
                selected.append(event)
        return selected

    @staticmethod
    def _sum_usage(events: list[dict[str, Any]]) -> dict[str, Any]:
        """Aggregate tokens and known cost across usage events."""
        totals = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "estimated_cost_usd": 0.0,
            "estimated_cost_known": False,
        }
        for event in events:
            totals["prompt_tokens"] += int(event.get("prompt_tokens", 0) or 0)
            totals["completion_tokens"] += int(event.get("completion_tokens", 0) or 0)
            totals["total_tokens"] += int(event.get("total_tokens", 0) or 0)
            raw_cost = event.get("estimated_cost_usd")
            if raw_cost is None:
                continue
            try:
                totals["estimated_cost_usd"] += float(raw_cost)
                totals["estimated_cost_known"] = True
            except (TypeError, ValueError):
                continue
        return totals

    @staticmethod
    def _format_size(size_bytes: int) -> str:
        """Format file sizes for the GUI."""
        units = ["B", "KB", "MB", "GB"]
        size = float(size_bytes)
        for unit in units:
            if size < 1024 or unit == units[-1]:
                if unit == "B":
                    return f"{int(size)} {unit}"
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{int(size_bytes)} B"
