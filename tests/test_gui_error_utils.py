"""Tests for nanobot_webgui.error_utils — explain_error."""

from __future__ import annotations

import pytest

from nanobot_webgui.error_utils import explain_error


# ---------------------------------------------------------------------------
# Default / unknown error
# ---------------------------------------------------------------------------


def test_explain_error_unknown_returns_base():
    result = explain_error("some random error")
    assert result["title"] == "Action failed"
    assert result["action_url"] == "/logs"
    assert "some random error" in result["raw"]


def test_explain_error_empty_string():
    result = explain_error("")
    assert result["raw"] == "Unknown error"


def test_explain_error_none_like():
    result = explain_error("   ")
    assert result["raw"] == "Unknown error"


# ---------------------------------------------------------------------------
# MCP configuration incomplete
# ---------------------------------------------------------------------------


def test_explain_error_missing_env_vars():
    result = explain_error("missing required environment variables: API_KEY")
    assert result["title"] == "MCP configuration is incomplete"
    assert result["action_url"] == "/mcp"


def test_explain_error_missing_env_vars_with_server_name():
    result = explain_error("missing required environment variables", server_name="my-server")
    assert result["action_url"] == "/mcp/my-server"
    assert result["action_label"] == "Open MCP Settings"


def test_explain_error_missing_env_vars_case_insensitive():
    result = explain_error("MISSING REQUIRED ENVIRONMENT VARIABLES")
    assert result["title"] == "MCP configuration is incomplete"


# ---------------------------------------------------------------------------
# Authentication errors
# ---------------------------------------------------------------------------


def test_explain_error_invalid_api_key():
    result = explain_error("invalid api key provided")
    assert result["title"] == "Provider authentication failed"
    assert result["action_url"] == "/setup/provider"


def test_explain_error_unauthorized():
    result = explain_error("401 Unauthorized")
    assert result["title"] == "Provider authentication failed"


def test_explain_error_missing_authentication_header():
    result = explain_error("Missing authentication header in request")
    assert result["title"] == "Provider authentication failed"


def test_explain_error_no_api_key_configured():
    result = explain_error("No API key is configured for this provider")
    assert result["title"] == "Provider authentication failed"


def test_explain_error_authentication_keyword():
    result = explain_error("Authentication failed")
    assert result["title"] == "Provider authentication failed"


# ---------------------------------------------------------------------------
# Timeout errors
# ---------------------------------------------------------------------------


def test_explain_error_timed_out():
    result = explain_error("The request timed out after 30s")
    assert result["title"] == "The request timed out"


def test_explain_error_timeout_keyword():
    result = explain_error("Connection timeout exceeded")
    assert result["title"] == "The request timed out"


def test_explain_error_timeout_general_context_keeps_logs_url():
    result = explain_error("timed out", context="general")
    assert result["action_url"] == "/logs"


def test_explain_error_timeout_mcp_context_with_server():
    result = explain_error("timed out", context="mcp", server_name="my-server")
    assert result["action_url"] == "/mcp/my-server"
    assert result["action_label"] == "Open MCP Settings"


def test_explain_error_timeout_mcp_context_no_server():
    result = explain_error("timed out", context="mcp")
    assert result["action_url"] == "/logs"


# ---------------------------------------------------------------------------
# Connection errors
# ---------------------------------------------------------------------------


def test_explain_error_failed_to_connect():
    result = explain_error("Failed to connect to endpoint")
    assert result["title"] == "Connection failed"


def test_explain_error_connection_keyword():
    result = explain_error("Connection refused")
    assert result["title"] == "Connection failed"


def test_explain_error_connection_error_case_insensitive():
    result = explain_error("CONNECTION REFUSED")
    assert result["title"] == "Connection failed"


# ---------------------------------------------------------------------------
# result structure completeness
# ---------------------------------------------------------------------------


def test_explain_error_always_has_required_keys():
    for error_msg in [
        "",
        "some error",
        "timed out",
        "invalid api key",
        "missing required environment variables",
        "failed to connect",
    ]:
        result = explain_error(error_msg)
        for key in ("title", "raw", "explanation", "next_action", "action_label", "action_url"):
            assert key in result, f"Key '{key}' missing for error: {error_msg!r}"
