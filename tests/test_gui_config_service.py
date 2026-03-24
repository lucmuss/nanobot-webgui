from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nanobot_webgui.config_service import GUIConfigService


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def service(tmp_path: Path) -> GUIConfigService:
    config_path = tmp_path / "instance" / "config.json"
    workspace_path = tmp_path / "workspace"
    svc = GUIConfigService(config_path, str(workspace_path))
    svc.ensure_instance()
    return svc


# ---------------------------------------------------------------------------
# ensure_instance / branding
# ---------------------------------------------------------------------------


def test_ensure_instance_syncs_branding_assets(tmp_path: Path):
    config_path = tmp_path / "instance" / "config.json"
    workspace_path = tmp_path / "workspace"
    svc = GUIConfigService(config_path, str(workspace_path))
    svc.ensure_instance()
    assert svc.branding_banner_path.exists()
    assert svc.branding_banner_path.read_bytes()


def test_ensure_instance_is_idempotent(service: GUIConfigService):
    # second call should not raise
    service.ensure_instance()


# ---------------------------------------------------------------------------
# load_state / save_state
# ---------------------------------------------------------------------------


def test_load_state_returns_dict(service: GUIConfigService):
    state = service.load_state()
    assert isinstance(state, dict)


def test_save_state_persists(service: GUIConfigService):
    state = service.load_state()
    state["custom_key"] = "hello"
    service.save_state(state)
    reloaded = service.load_state()
    assert reloaded["custom_key"] == "hello"


# ---------------------------------------------------------------------------
# setup_complete
# ---------------------------------------------------------------------------


def test_setup_complete_false_initially(service: GUIConfigService):
    assert service.is_setup_complete() is False


def test_setup_complete_persists(service: GUIConfigService):
    service.set_setup_complete(True)
    assert service.is_setup_complete() is True


# ---------------------------------------------------------------------------
# safe_mode
# ---------------------------------------------------------------------------


def test_safe_mode_default_true(service: GUIConfigService):
    assert service.is_safe_mode() is True


def test_safe_mode_can_be_disabled(service: GUIConfigService):
    service.set_safe_mode(False)
    assert service.is_safe_mode() is False


def test_safe_mode_toggle_back(service: GUIConfigService):
    service.set_safe_mode(False)
    service.set_safe_mode(True)
    assert service.is_safe_mode() is True


# ---------------------------------------------------------------------------
# unrestricted_agent_shell
# ---------------------------------------------------------------------------


def test_unrestricted_shell_default_false(service: GUIConfigService):
    assert service.is_unrestricted_agent_shell_enabled() is False


def test_unrestricted_shell_can_be_enabled(service: GUIConfigService):
    service.set_unrestricted_agent_shell_enabled(True)
    assert service.is_unrestricted_agent_shell_enabled() is True


# ---------------------------------------------------------------------------
# agent_health
# ---------------------------------------------------------------------------


def test_agent_health_default_empty(service: GUIConfigService):
    assert service.get_agent_health() == {}


def test_agent_health_persists(service: GUIConfigService):
    service.set_agent_health({"status": "ok", "latency": 1.2})
    assert service.get_agent_health()["status"] == "ok"


def test_agent_health_overwrites(service: GUIConfigService):
    service.set_agent_health({"status": "ok"})
    service.set_agent_health({"status": "error"})
    assert service.get_agent_health()["status"] == "error"


# ---------------------------------------------------------------------------
# MCP registry
# ---------------------------------------------------------------------------


def test_mcp_registry_empty_initially(service: GUIConfigService):
    assert service.get_mcp_registry() == {}


def test_mcp_record_set_and_get(service: GUIConfigService):
    service.set_mcp_record("my-server", {"enabled": True, "name": "My Server"})
    record = service.get_mcp_record("my-server")
    assert record["enabled"] is True
    assert record["name"] == "My Server"


def test_mcp_record_nonexistent_returns_empty(service: GUIConfigService):
    assert service.get_mcp_record("ghost") == {}


def test_mcp_enabled_true(service: GUIConfigService):
    service.set_mcp_record("my-server", {"enabled": True})
    assert service.is_mcp_enabled("my-server") is True


def test_mcp_enabled_false(service: GUIConfigService):
    service.set_mcp_record("my-server", {"enabled": False})
    assert service.is_mcp_enabled("my-server") is False


def test_mcp_enabled_missing_server(service: GUIConfigService):
    assert service.is_mcp_enabled("ghost") is False


def test_set_mcp_enabled_preserves_other_fields(service: GUIConfigService):
    service.set_mcp_record("my-server", {"enabled": True, "name": "My Server"})
    service.set_mcp_enabled("my-server", False)
    record = service.get_mcp_record("my-server")
    assert record["enabled"] is False
    assert record["name"] == "My Server"


def test_remove_mcp_record(service: GUIConfigService):
    service.set_mcp_record("my-server", {"enabled": True})
    service.remove_mcp_record("my-server")
    assert service.get_mcp_record("my-server") == {}


def test_remove_mcp_record_noop_for_missing(service: GUIConfigService):
    service.remove_mcp_record("ghost")  # should not raise


def test_enabled_mcp_servers_filters_correctly(service: GUIConfigService):
    service.set_mcp_record("enabled-server", {"enabled": True})
    service.set_mcp_record("disabled-server", {"enabled": False})
    servers = {"enabled-server": {}, "disabled-server": {}, "untracked": {}}
    result = service.enabled_mcp_servers(servers)
    assert "enabled-server" in result
    assert "disabled-server" not in result
    assert "untracked" not in result


# ---------------------------------------------------------------------------
# last_successful_chat / last_error
# ---------------------------------------------------------------------------


def test_last_successful_chat_default_empty(service: GUIConfigService):
    assert service.get_last_successful_chat() == {}


def test_last_successful_chat_persists(service: GUIConfigService):
    service.set_last_successful_chat({"message": "ok"})
    assert service.get_last_successful_chat()["message"] == "ok"


def test_last_error_default_empty(service: GUIConfigService):
    assert service.get_last_error() == {}


def test_last_error_persists_and_clears(service: GUIConfigService):
    service.set_last_error({"error": "boom"})
    assert service.get_last_error()["error"] == "boom"
    service.clear_last_error()
    assert service.get_last_error() == {}


# ---------------------------------------------------------------------------
# update_status
# ---------------------------------------------------------------------------


def test_update_status_default_empty(service: GUIConfigService):
    assert service.get_update_status() == {}


def test_update_status_persists_normalized(service: GUIConfigService):
    result = service.set_update_status({
        "enabled": True,
        "current_version": "0.3.11",
        "latest_version": "0.3.12",
        "available": True,
    })
    assert result["enabled"] is True
    assert result["current_version"] == "0.3.11"
    assert result["available"] is True
    assert service.get_update_status()["latest_version"] == "0.3.12"


def test_update_status_defaults_missing_fields(service: GUIConfigService):
    result = service.set_update_status({})
    assert result["enabled"] is False
    assert result["available"] is False
    assert result["error"] == ""


# ---------------------------------------------------------------------------
# community_preferences
# ---------------------------------------------------------------------------


def test_community_preferences_defaults(service: GUIConfigService):
    prefs = service.get_community_preferences()
    assert prefs["share_anonymous_metrics"] is False
    assert prefs["receive_recommendations"] is True
    assert prefs["show_marketplace_stats"] is True
    assert prefs["allow_public_mcp_submissions"] is False


def test_community_preferences_persists(service: GUIConfigService):
    service.set_community_preferences(
        share_anonymous_metrics=True,
        receive_recommendations=False,
        show_marketplace_stats=False,
        allow_public_mcp_submissions=True,
    )
    prefs = service.get_community_preferences()
    assert prefs["share_anonymous_metrics"] is True
    assert prefs["allow_public_mcp_submissions"] is True


# ---------------------------------------------------------------------------
# active_memory_doc
# ---------------------------------------------------------------------------


def test_active_memory_doc_defaults_to_memory(service: GUIConfigService):
    assert service.get_active_memory_doc() == "memory"


def test_active_memory_doc_persists(service: GUIConfigService):
    service.set_active_memory_doc("agents")
    assert service.get_active_memory_doc() == "agents"


# ---------------------------------------------------------------------------
# usage events
# ---------------------------------------------------------------------------


def test_usage_events_empty_initially(service: GUIConfigService):
    assert service.get_usage_events() == []


def test_record_usage_event_appends(service: GUIConfigService):
    now = datetime.now(timezone.utc).isoformat()
    service.record_usage_event({"timestamp": now, "source": "chat", "prompt_tokens": 10, "completion_tokens": 5})
    events = service.get_usage_events()
    assert len(events) == 1
    assert events[0]["source"] == "chat"


def test_record_usage_event_normalizes_fields(service: GUIConfigService):
    now = datetime.now(timezone.utc).isoformat()
    event = service.record_usage_event({"timestamp": now, "source": "chat"})
    assert "prompt_tokens" in event
    assert "completion_tokens" in event
    assert event["prompt_tokens"] == 0


def test_record_usage_event_caps_at_300(service: GUIConfigService):
    now = datetime.now(timezone.utc)
    for i in range(305):
        ts = (now - timedelta(minutes=i)).isoformat()
        service.record_usage_event({"timestamp": ts, "source": "chat"})
    events = service.get_usage_events()
    assert len(events) == 300


def test_get_usage_events_sorted_newest_first(service: GUIConfigService):
    now = datetime.now(timezone.utc)
    service.record_usage_event({"timestamp": (now - timedelta(hours=2)).isoformat(), "source": "old"})
    service.record_usage_event({"timestamp": (now - timedelta(minutes=5)).isoformat(), "source": "new"})
    events = service.get_usage_events()
    assert events[0]["source"] == "new"


def test_get_usage_events_limit(service: GUIConfigService):
    now = datetime.now(timezone.utc)
    for i in range(10):
        service.record_usage_event({"timestamp": (now - timedelta(minutes=i)).isoformat(), "source": "chat"})
    assert len(service.get_usage_events(limit=3)) == 3


# ---------------------------------------------------------------------------
# usage summary helpers
# ---------------------------------------------------------------------------


def test_usage_summary_24h_filter(service: GUIConfigService):
    now = datetime.now(timezone.utc)
    service.record_usage_event({
        "timestamp": (now - timedelta(hours=2)).isoformat(),
        "source": "chat",
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
    })
    service.record_usage_event({
        "timestamp": (now - timedelta(hours=48)).isoformat(),
        "source": "chat",
        "prompt_tokens": 200,
        "completion_tokens": 100,
        "total_tokens": 300,
    })
    summary = service.get_usage_summary()
    assert summary["totals_24h"]["total_tokens"] == 150
    assert summary["totals_all_time"]["total_tokens"] == 450


def test_usage_summary_sources_24h(service: GUIConfigService):
    now = datetime.now(timezone.utc)
    service.record_usage_event({"timestamp": (now - timedelta(hours=1)).isoformat(), "source": "chat", "total_tokens": 100})
    service.record_usage_event({"timestamp": (now - timedelta(hours=2)).isoformat(), "source": "telegram", "total_tokens": 50})
    summary = service.get_usage_summary()
    sources = {s["source"]: s for s in summary["sources_24h"]}
    assert "chat" in sources
    assert "telegram" in sources
    assert sources["chat"]["total_tokens"] == 100


def test_usage_summary_recent_models_deduplicated(service: GUIConfigService):
    now = datetime.now(timezone.utc)
    for i in range(3):
        service.record_usage_event({
            "timestamp": (now - timedelta(minutes=i)).isoformat(),
            "provider": "openrouter",
            "model": "gpt-4",
            "source": "chat",
        })
    summary = service.get_usage_summary()
    models = summary["recent_models"]
    assert models.count("openrouter / gpt-4") == 1


def test_usage_summary_empty(service: GUIConfigService):
    summary = service.get_usage_summary()
    assert summary["event_count"] == 0
    assert summary["totals_24h"]["total_tokens"] == 0
    assert summary["sources_24h"] == []
    assert summary["last_event"] == {}
