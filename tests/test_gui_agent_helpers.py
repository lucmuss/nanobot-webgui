"""Tests for nanobot_webgui.agent_service module-level helper functions."""

from __future__ import annotations

import builtins
import sys
from types import ModuleType, SimpleNamespace

import pytest

from nanobot_webgui import agent_service
from nanobot_webgui.agent_service import (
    _display_content,
    _extract_json_object,
    _fallback_mcp_tags,
    _make_provider,
    _merge_marketplace_tags,
    _normalize_marketplace_tags,
)


# ---------------------------------------------------------------------------
# _display_content
# ---------------------------------------------------------------------------


def test_display_content_plain_string():
    assert _display_content("hello world") == "hello world"


def test_display_content_empty_string():
    assert _display_content("") == ""


def test_display_content_list_of_text():
    content = [{"type": "text", "text": "foo"}, {"type": "text", "text": "bar"}]
    assert _display_content(content) == "foo\nbar"


def test_display_content_list_with_image():
    content = [{"type": "image_url", "url": "http://x"}, {"type": "text", "text": "caption"}]
    result = _display_content(content)
    assert "[image]" in result
    assert "caption" in result


def test_display_content_list_empty():
    assert _display_content([]) == ""


def test_display_content_strips_community_context_block():
    content = "intro [nanobot_community_context]hidden stuff[/nanobot_community_context] outro"
    result = _display_content(content)
    assert "hidden stuff" not in result
    assert "intro" in result
    assert "outro" in result


def test_display_content_strips_community_block_in_list():
    content = [{"type": "text", "text": "before [nanobot_community_context]hide[/nanobot_community_context] after"}]
    result = _display_content(content)
    assert "hide" not in result
    assert "before" in result
    assert "after" in result


def test_display_content_non_list_non_string():
    result = _display_content(42)
    assert result == "42"


def test_display_content_list_skips_empty_text():
    content = [{"type": "text", "text": "  "}, {"type": "text", "text": "valid"}]
    result = _display_content(content)
    assert result.strip() == "valid"


# ---------------------------------------------------------------------------
# _extract_json_object
# ---------------------------------------------------------------------------


def test_extract_json_plain():
    result = _extract_json_object('{"key": "value"}')
    assert result == {"key": "value"}


def test_extract_json_fenced_markdown():
    raw = '```json\n{"install_mode": "npm"}\n```'
    result = _extract_json_object(raw)
    assert result["install_mode"] == "npm"


def test_extract_json_fenced_no_language():
    raw = '```\n{"key": 1}\n```'
    result = _extract_json_object(raw)
    assert result["key"] == 1


def test_extract_json_embedded_in_text():
    raw = 'Here is my plan:\n{"step": "install"}\nDone.'
    result = _extract_json_object(raw)
    assert result["step"] == "install"


def test_extract_json_empty_raises():
    with pytest.raises(ValueError, match="empty response"):
        _extract_json_object("")


def test_extract_json_whitespace_only_raises():
    with pytest.raises(ValueError, match="empty response"):
        _extract_json_object("   ")


def test_extract_json_invalid_raises():
    with pytest.raises(ValueError, match="invalid JSON"):
        _extract_json_object("{not valid json}")


def test_extract_json_array_raises():
    with pytest.raises(ValueError, match="JSON object"):
        _extract_json_object("[1, 2, 3]")


def test_extract_json_nested_object():
    raw = '{"outer": {"inner": true}}'
    result = _extract_json_object(raw)
    assert result["outer"]["inner"] is True


# ---------------------------------------------------------------------------
# _normalize_marketplace_tags
# ---------------------------------------------------------------------------


def test_normalize_tags_string_comma_separated():
    result = _normalize_marketplace_tags("python, ai, mcp")
    assert "python" in result
    assert "ai" in result
    assert "mcp" in result


def test_normalize_tags_list_of_strings():
    result = _normalize_marketplace_tags(["Python", "AI", "MCP"])
    assert "python" in result
    assert "ai" in result


def test_normalize_tags_lowercases():
    result = _normalize_marketplace_tags(["MyCoolTag"])
    assert result == ["mycooltag"]


def test_normalize_tags_deduplicates():
    result = _normalize_marketplace_tags(["python", "python", "ai"])
    assert result.count("python") == 1


def test_normalize_tags_strips_special_chars():
    result = _normalize_marketplace_tags(["hello world!"])
    assert result == ["hello-world-"]


def test_normalize_tags_truncates_at_10():
    tags = [str(i) for i in range(20)]
    result = _normalize_marketplace_tags(tags)
    assert len(result) == 10


def test_normalize_tags_truncates_tag_at_32_chars():
    long_tag = "a" * 50
    result = _normalize_marketplace_tags([long_tag])
    assert len(result[0]) <= 32


def test_normalize_tags_unknown_type_returns_empty():
    result = _normalize_marketplace_tags(42)
    assert result == []


def test_normalize_tags_none_returns_empty():
    result = _normalize_marketplace_tags(None)
    assert result == []


def test_normalize_tags_empty_string():
    result = _normalize_marketplace_tags("")
    assert result == []


def test_normalize_tags_semicolon_separated():
    result = _normalize_marketplace_tags("a;b;c")
    assert len(result) == 3


def test_normalize_tags_newline_separated():
    result = _normalize_marketplace_tags("a\nb\nc")
    assert len(result) == 3


# ---------------------------------------------------------------------------
# _merge_marketplace_tags
# ---------------------------------------------------------------------------


def test_merge_tags_combines_groups():
    result = _merge_marketplace_tags(["a", "b"], ["c", "d"])
    assert result == ["a", "b", "c", "d"]


def test_merge_tags_deduplicates_across_groups():
    result = _merge_marketplace_tags(["a", "b"], ["b", "c"])
    assert result.count("b") == 1


def test_merge_tags_caps_at_10():
    g1 = [str(i) for i in range(6)]
    g2 = [str(i + 6) for i in range(6)]
    result = _merge_marketplace_tags(g1, g2)
    assert len(result) == 10


def test_merge_tags_empty_groups():
    result = _merge_marketplace_tags([], [])
    assert result == []


def test_merge_tags_preserves_order():
    result = _merge_marketplace_tags(["first", "second"], ["third"])
    assert result == ["first", "second", "third"]


def test_merge_tags_skips_empty_strings():
    result = _merge_marketplace_tags(["", "valid", ""])
    assert result == ["valid"]


# ---------------------------------------------------------------------------
# _fallback_mcp_tags
# ---------------------------------------------------------------------------


def test_fallback_mcp_tags_returns_list():
    result = _fallback_mcp_tags(
        server_name="my-server",
        description="A useful MCP server for Python",
        tool_names=["search", "fetch"],
        category="utility",
        existing_tags=[],
    )
    assert isinstance(result, list)
    assert len(result) <= 10


def test_fallback_mcp_tags_uses_existing_tags():
    result = _fallback_mcp_tags(
        server_name="my-server",
        description="",
        tool_names=[],
        category="",
        existing_tags=["python", "ai"],
    )
    assert "python" in result
    assert "ai" in result


def test_fallback_mcp_tags_includes_category():
    result = _fallback_mcp_tags(
        server_name="my-server",
        description="",
        tool_names=[],
        category="database",
        existing_tags=[],
    )
    assert "database" in result


def test_fallback_mcp_tags_derives_from_server_name():
    result = _fallback_mcp_tags(
        server_name="my-cool-server",
        description="",
        tool_names=[],
        category="",
        existing_tags=[],
    )
    # server name parts should be among the tags
    name_parts = {"my", "cool", "server"}
    assert name_parts & set(result), f"Expected name parts in {result}"


def test_fallback_mcp_tags_empty_inputs():
    result = _fallback_mcp_tags(
        server_name="",
        description="",
        tool_names=[],
        category="",
        existing_tags=[],
    )
    assert isinstance(result, list)


def test_make_provider_skips_custom_import_for_non_custom_provider(monkeypatch: pytest.MonkeyPatch):
    class FakeLiteLLMProvider:
        def __init__(
            self,
            *,
            api_key,
            api_base,
            default_model,
            extra_headers,
            provider_name,
        ) -> None:
            self.api_key = api_key
            self.api_base = api_base
            self.default_model = default_model
            self.extra_headers = extra_headers
            self.provider_name = provider_name

    monkeypatch.setitem(
        sys.modules,
        "nanobot.providers.litellm_provider",
        ModuleType("nanobot.providers.litellm_provider"),
    )
    sys.modules["nanobot.providers.litellm_provider"].LiteLLMProvider = FakeLiteLLMProvider

    monkeypatch.setitem(
        sys.modules,
        "nanobot.providers.registry",
        ModuleType("nanobot.providers.registry"),
    )
    sys.modules["nanobot.providers.registry"].find_by_name = lambda _name: SimpleNamespace(
        is_oauth=False,
        is_local=False,
    )

    original_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "nanobot.providers.custom_provider":
            raise ModuleNotFoundError("No module named 'nanobot.providers.custom_provider'")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    config = SimpleNamespace(
        agents=SimpleNamespace(
            defaults=SimpleNamespace(
                model="openrouter/openai/gpt-4.1-mini",
            )
        ),
        get_provider_name=lambda _model: "openrouter",
        get_provider=lambda _model: SimpleNamespace(
            api_key="test-key",
            extra_headers={"X-Test": "1"},
        ),
        get_api_base=lambda _model: "https://openrouter.ai/api/v1",
    )

    provider = _make_provider(config)

    assert isinstance(provider, FakeLiteLLMProvider)
    assert provider.provider_name == "openrouter"
    assert provider.api_key == "test-key"
