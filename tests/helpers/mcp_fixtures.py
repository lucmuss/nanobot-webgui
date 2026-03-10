"""Shared MCP fixture metadata for GUI integration and E2E tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "fixtures" / "mcp"


def load_mcp_fixture(name: str) -> dict[str, Any]:
    """Load one MCP fixture by directory name."""
    fixture_path = FIXTURE_ROOT / name / "fixture.json"
    if not fixture_path.exists():
        raise FileNotFoundError(f"Unknown MCP fixture: {name}")
    return json.loads(fixture_path.read_text(encoding="utf-8"))


def build_mcp_fixture_analysis(
    *,
    fixture_name: str,
    repo: dict[str, str],
) -> dict[str, Any]:
    """Build one analyze/install payload from a local fixture definition."""
    fixture = load_mcp_fixture(fixture_name)
    transport = str(fixture.get("transport", "stdio")).strip() or "stdio"
    install_mode = str(fixture.get("install_mode", "fixture")).strip() or "fixture"
    required_runtimes = []
    if str(fixture.get("run_command", "")).strip() == "npx":
        required_runtimes = ["node", "npx"]
    elif str(fixture.get("run_command", "")).strip() in {"python", "python3"}:
        required_runtimes = ["python"]
    return {
        "server_name": str(fixture.get("server_name", fixture_name)).strip(),
        "title": f"{repo['owner']}/{repo['repo']}",
        "summary": str(fixture.get("summary", "")).strip(),
        "repo_url": repo["repo_url"],
        "clone_url": repo["clone_url"],
        "install_slug": f"{repo['owner']}__{repo['repo']}".lower(),
        "install_mode": install_mode,
        "transport": transport,
        "run_command": str(fixture.get("run_command", "")).strip(),
        "run_args": [str(item) for item in fixture.get("run_args", [])],
        "run_url": str(fixture.get("run_url", "")).strip(),
        "install_steps": [
            {"display": str(item), "command": [], "timeout": 0}
            for item in fixture.get("install_steps", [])
        ],
        "required_env": [str(item) for item in fixture.get("required_env", [])],
        "optional_env": [str(item) for item in fixture.get("optional_env", [])],
        "healthcheck": str(fixture.get("healthcheck", "Fixture MCP handshake")).strip(),
        "evidence": [str(item) for item in fixture.get("evidence", [f"fixture:{fixture_name}"])],
        "tool_names": [str(item) for item in fixture.get("tool_names", [])],
        "probe_error": str(fixture.get("probe_error", "")).strip(),
        "repo_type": str(fixture.get("repo_type", "fixture")).strip() or "fixture",
        "analysis_mode": "deterministic",
        "analysis_confidence": float(fixture.get("analysis_confidence", 0.95) or 0.95),
        "required_runtimes": required_runtimes,
        "runtime_status": [{"name": item, "available": True, "executable": item} for item in required_runtimes],
        "missing_runtimes": [],
        "can_install": True,
        "next_action": "Install the MCP, verify the runtime test, then enable it for chat.",
    }
