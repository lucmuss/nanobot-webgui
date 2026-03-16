"""User-facing error explanations for the nanobot GUI."""

from __future__ import annotations

from typing import Any


def explain_error(raw_error: str, *, context: str = "general", server_name: str | None = None) -> dict[str, str]:
    """Convert a raw technical error into a short user-facing explanation."""
    raw = (raw_error or "").strip()
    lower = raw.lower()

    base = {
        "title": "Action failed",
        "raw": raw or "Unknown error",
        "explanation": "The action could not be completed.",
        "next_action": "Check the related settings and inspect the logs for more detail.",
        "action_label": "Open Logs",
        "action_url": "/logs",
    }

    if "missing required environment variables" in lower:
        target = f"/mcp/{server_name}" if server_name else "/mcp"
        base.update(
            {
                "title": "MCP configuration is incomplete",
                "explanation": "The MCP server is installed, but one or more required environment variables are empty.",
                "next_action": "Open the MCP settings, fill in the missing variables, then run the test again.",
                "action_label": "Open MCP Settings",
                "action_url": target,
            }
        )
        return base

    if (
        "missing authentication header" in lower
        or "invalid api key" in lower
        or "authentication" in lower
        or "no api key is configured" in lower
        or "unauthorized" in lower
    ):
        base.update(
            {
                "title": "Provider authentication failed",
                "explanation": "The configured provider did not receive a valid API key or authentication header.",
                "next_action": "Open Provider settings and verify the API key, API base, and custom headers.",
                "action_label": "Open Provider",
                "action_url": "/setup/provider",
            }
        )
        return base

    if "timed out" in lower or "timeout" in lower:
        base.update(
            {
                "title": "The request timed out",
                "explanation": "Nanobot waited too long for the provider or MCP server to respond.",
                "next_action": "Retry the action. If it keeps happening, review the runtime logs or increase the timeout for this MCP.",
            }
        )
        if context == "mcp" and server_name:
            base["action_label"] = "Open MCP Settings"
            base["action_url"] = f"/mcp/{server_name}"
        return base

    if "failed to connect" in lower or "connection" in lower:
        base.update(
            {
                "title": "Connection failed",
                "explanation": "Nanobot could not reach the configured service endpoint.",
                "next_action": "Check whether the endpoint is online and whether the configured URL, command, or network settings are correct.",
            }
        )
        return base

    return base
