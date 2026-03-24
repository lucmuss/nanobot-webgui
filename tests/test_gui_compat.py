"""Tests for nanobot_webgui.compat helper functions."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from nanobot_webgui.compat import (
    _camel_case,
    _preview_content,
    _session_type,
    build_agent_loop_kwargs,
    enrich_session_summaries,
    get_agent_default,
    get_channel_field,
    get_tools_enabled,
    is_channel_enabled,
    set_agent_default,
    set_channel_field,
    set_tools_enabled,
    supports_tools_enabled,
)


# ---------------------------------------------------------------------------
# _camel_case
# ---------------------------------------------------------------------------


def test_camel_case_single_word():
    assert _camel_case("model") == "model"


def test_camel_case_two_words():
    assert _camel_case("max_tokens") == "maxTokens"


def test_camel_case_three_words():
    assert _camel_case("max_tool_iterations") == "maxToolIterations"


def test_camel_case_empty_string():
    assert _camel_case("") == ""


def test_camel_case_already_camel():
    # single "word" with no underscores is returned as-is
    assert _camel_case("maxTokens") == "maxTokens"


# ---------------------------------------------------------------------------
# _preview_content
# ---------------------------------------------------------------------------


def test_preview_content_plain_string():
    assert _preview_content("hello world") == "hello world"


def test_preview_content_list_of_text_items():
    content = [{"type": "text", "text": "hello"}, {"type": "text", "text": "world"}]
    assert _preview_content(content) == "hello world"


def test_preview_content_list_with_non_text_items():
    content = [{"type": "image_url", "url": "http://x"}, {"type": "text", "text": "caption"}]
    assert _preview_content(content) == "caption"


def test_preview_content_truncates_at_limit():
    long_text = "a" * 200
    result = _preview_content(long_text, limit=180)
    # function returns preview[:limit-1] + "..." so result is limit+2 chars
    assert result.endswith("...")
    assert len(result) < len(long_text)


def test_preview_content_exact_limit_not_truncated():
    text = "a" * 180
    result = _preview_content(text, limit=180)
    assert not result.endswith("...")


def test_preview_content_empty_string():
    assert _preview_content("") == ""


def test_preview_content_none():
    assert _preview_content(None) == ""


def test_preview_content_collapses_whitespace():
    assert _preview_content("  foo   bar  ") == "foo bar"


# ---------------------------------------------------------------------------
# _session_type
# ---------------------------------------------------------------------------


def test_session_type_mcp_test():
    assert _session_type("web:mcp-test:echo") == "MCP test"


def test_session_type_web_chat():
    assert _session_type("web:admin-1") == "Web chat"


def test_session_type_cli():
    assert _session_type("cli:run-1") == "CLI"


def test_session_type_other():
    assert _session_type("telegram:123") == "Other"


def test_session_type_empty():
    assert _session_type("") == "Other"


# ---------------------------------------------------------------------------
# get_tools_enabled / set_tools_enabled / supports_tools_enabled
# ---------------------------------------------------------------------------


def _make_config_with_tools(enabled: bool) -> Any:
    tools = SimpleNamespace(enabled=enabled)
    return SimpleNamespace(tools=tools)


def test_get_tools_enabled_true():
    assert get_tools_enabled(_make_config_with_tools(True)) is True


def test_get_tools_enabled_false():
    assert get_tools_enabled(_make_config_with_tools(False)) is False


def test_get_tools_enabled_defaults_true_when_no_tools_attr():
    config = SimpleNamespace()
    assert get_tools_enabled(config) is True


def test_set_tools_enabled_sets_value():
    config = _make_config_with_tools(True)
    set_tools_enabled(config, False)
    assert config.tools.enabled is False


def test_set_tools_enabled_noop_when_no_tools():
    config = SimpleNamespace()
    set_tools_enabled(config, False)  # should not raise


def test_set_tools_enabled_noop_when_no_enabled_attr():
    config = SimpleNamespace(tools=SimpleNamespace())
    set_tools_enabled(config, False)  # should not raise


def test_supports_tools_enabled_true():
    assert supports_tools_enabled(_make_config_with_tools(True)) is True


def test_supports_tools_enabled_false_no_tools():
    assert supports_tools_enabled(SimpleNamespace()) is False


def test_supports_tools_enabled_false_no_enabled():
    config = SimpleNamespace(tools=SimpleNamespace())
    assert supports_tools_enabled(config) is False


# ---------------------------------------------------------------------------
# get_agent_default / set_agent_default
# ---------------------------------------------------------------------------


def _make_object_config(**kwargs) -> Any:
    defaults = SimpleNamespace(**kwargs)
    agents = SimpleNamespace(defaults=defaults)
    return SimpleNamespace(agents=agents)


def _make_dict_config(**kwargs) -> Any:
    """Config where agents.defaults is a plain dict."""
    agents = SimpleNamespace(defaults=dict(kwargs))
    return SimpleNamespace(agents=agents)


def test_get_agent_default_object_shape():
    config = _make_object_config(max_tokens=1024)
    assert get_agent_default(config, "max_tokens") == 1024


def test_get_agent_default_object_shape_missing_returns_default():
    config = _make_object_config(model="gpt-4")
    assert get_agent_default(config, "memory_window", 100) == 100


def test_get_agent_default_dict_shape_snake_case():
    config = _make_dict_config(max_tokens=512)
    assert get_agent_default(config, "max_tokens") == 512


def test_get_agent_default_dict_shape_camel_case_alias():
    config = _make_dict_config(maxTokens=2048)
    assert get_agent_default(config, "max_tokens") == 2048


def test_get_agent_default_no_agents_returns_default():
    config = SimpleNamespace()
    assert get_agent_default(config, "max_tokens", 99) == 99


def test_set_agent_default_object_shape():
    config = _make_object_config(max_tokens=100)
    set_agent_default(config, "max_tokens", 500)
    assert config.agents.defaults.max_tokens == 500


def test_set_agent_default_object_shape_noop_for_missing_field():
    config = _make_object_config(model="gpt-4")
    set_agent_default(config, "memory_window", 50)
    assert not hasattr(config.agents.defaults, "memory_window")


def test_set_agent_default_dict_shape_snake_case_key():
    config = _make_dict_config(max_tokens=100)
    set_agent_default(config, "max_tokens", 999)
    assert config.agents.defaults["max_tokens"] == 999


def test_set_agent_default_dict_shape_camel_case_key_preserved():
    config = _make_dict_config(maxTokens=100)
    set_agent_default(config, "max_tokens", 999)
    # should update under the camelCase key that already exists
    assert config.agents.defaults["maxTokens"] == 999
    assert "max_tokens" not in config.agents.defaults


def test_set_agent_default_dict_shape_new_key_uses_camel():
    config = _make_dict_config()
    set_agent_default(config, "memory_window", 50)
    assert config.agents.defaults["memoryWindow"] == 50


def test_set_agent_default_no_agents_is_noop():
    config = SimpleNamespace()
    set_agent_default(config, "max_tokens", 100)  # should not raise


# ---------------------------------------------------------------------------
# get_channel_field / set_channel_field / is_channel_enabled
# ---------------------------------------------------------------------------


def _make_channel_config(channel_name: str, **kwargs) -> Any:
    channel_obj = SimpleNamespace(**kwargs)
    channels = SimpleNamespace(**{channel_name: channel_obj})
    return SimpleNamespace(channels=channels)


def _make_dict_channel_config(channel_name: str, **kwargs) -> Any:
    channels = SimpleNamespace(**{channel_name: dict(kwargs)})
    return SimpleNamespace(channels=channels)


def test_get_channel_field_object_shape():
    config = _make_channel_config("telegram", enabled=True, token="abc")
    assert get_channel_field(config, "telegram", "token") == "abc"


def test_get_channel_field_dict_shape():
    config = _make_dict_channel_config("telegram", enabled=True, token="abc")
    assert get_channel_field(config, "telegram", "token") == "abc"


def test_get_channel_field_dict_camel_alias():
    config = _make_dict_channel_config("telegram", botToken="tok123")
    assert get_channel_field(config, "telegram", "bot_token") == "tok123"


def test_get_channel_field_missing_channel_returns_default():
    config = SimpleNamespace(channels=SimpleNamespace())
    assert get_channel_field(config, "slack", "enabled", False) is False


def test_set_channel_field_object_shape():
    config = _make_channel_config("telegram", enabled=False)
    set_channel_field(config, "telegram", "enabled", True)
    assert config.channels.telegram.enabled is True


def test_set_channel_field_dict_shape():
    config = _make_dict_channel_config("telegram", enabled=False)
    set_channel_field(config, "telegram", "enabled", True)
    assert config.channels.telegram["enabled"] is True


def test_set_channel_field_dict_preserves_camel_key():
    config = _make_dict_channel_config("telegram", botToken="old")
    set_channel_field(config, "telegram", "bot_token", "new")
    assert config.channels.telegram["botToken"] == "new"
    assert "bot_token" not in config.channels.telegram


def test_set_channel_field_creates_new_channel():
    channels = SimpleNamespace()
    config = SimpleNamespace(channels=channels)
    set_channel_field(config, "telegram", "enabled", True)
    assert config.channels.telegram["enabled"] is True


def test_set_channel_field_noop_when_no_channels():
    config = SimpleNamespace()
    set_channel_field(config, "telegram", "enabled", True)  # should not raise


def test_is_channel_enabled_true():
    config = _make_channel_config("telegram", enabled=True)
    assert is_channel_enabled(config, "telegram") is True


def test_is_channel_enabled_false():
    config = _make_channel_config("telegram", enabled=False)
    assert is_channel_enabled(config, "telegram") is False


def test_is_channel_enabled_missing_channel():
    config = SimpleNamespace(channels=SimpleNamespace())
    assert is_channel_enabled(config, "slack") is False


# ---------------------------------------------------------------------------
# build_agent_loop_kwargs
# ---------------------------------------------------------------------------


def test_build_agent_loop_kwargs_filters_unknown():
    kwargs = {"workspace": "/tmp", "__unknown_key__": "value"}
    result = build_agent_loop_kwargs(kwargs)
    assert "workspace" in result
    assert "__unknown_key__" not in result


def test_build_agent_loop_kwargs_empty():
    result = build_agent_loop_kwargs({})
    assert result == {}


# ---------------------------------------------------------------------------
# enrich_session_summaries
# ---------------------------------------------------------------------------


def test_enrich_session_summaries_adds_defaults():
    sessions = [{"key": "web:admin-1"}]
    result = enrich_session_summaries(sessions)
    assert result[0]["session_type"] == "Web chat"
    assert result[0]["message_count"] == 0
    assert result[0]["last_user"] == ""
    assert result[0]["last_assistant"] == ""


def test_enrich_session_summaries_preserves_existing_fields():
    sessions = [{"key": "cli:run-1", "session_type": "CLI", "message_count": 5, "last_user": "hi", "last_assistant": "hello"}]
    result = enrich_session_summaries(sessions)
    assert result[0]["message_count"] == 5
    assert result[0]["last_user"] == "hi"


def test_enrich_session_summaries_reads_jsonl(tmp_path: Path):
    session_file = tmp_path / "session.jsonl"
    session_file.write_text(
        json.dumps({"_type": "metadata", "key": "web:admin-1"}) + "\n"
        + json.dumps({"role": "user", "content": "hello"}) + "\n"
        + json.dumps({"role": "assistant", "content": "hi there"}) + "\n",
        encoding="utf-8",
    )
    sessions = [{"key": "web:admin-1", "path": str(session_file)}]
    result = enrich_session_summaries(sessions)
    assert result[0]["message_count"] == 2
    assert result[0]["last_user"] == "hello"
    assert result[0]["last_assistant"] == "hi there"


def test_enrich_session_summaries_handles_missing_file():
    sessions = [{"key": "web:admin-1", "path": "/nonexistent/path/session.jsonl"}]
    result = enrich_session_summaries(sessions)
    assert result[0]["message_count"] == 0


def test_enrich_session_summaries_handles_corrupt_jsonl(tmp_path: Path):
    session_file = tmp_path / "bad.jsonl"
    session_file.write_text("not json at all\n", encoding="utf-8")
    sessions = [{"key": "web:admin-1", "path": str(session_file)}]
    result = enrich_session_summaries(sessions)
    assert result[0]["message_count"] == 0


def test_enrich_session_summaries_empty_list():
    assert enrich_session_summaries([]) == []


def test_enrich_session_summaries_mcp_test_type():
    sessions = [{"key": "web:mcp-test:my-server"}]
    result = enrich_session_summaries(sessions)
    assert result[0]["session_type"] == "MCP test"
