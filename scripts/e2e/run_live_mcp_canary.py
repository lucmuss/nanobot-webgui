#!/usr/bin/env python3
"""Run a live MCP canary against a small set of official repositories."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nanobot.gui.config_service import GUIConfigService
from nanobot.gui.mcp_service import GUIMCPService


@dataclass(frozen=True, slots=True)
class CanaryCase:
    name: str
    source: str


CASES = [
    CanaryCase("chrome-devtools", "https://github.com/ChromeDevTools/chrome-devtools-mcp"),
    CanaryCase("context7", "https://github.com/upstash/context7"),
    CanaryCase("playwright", "https://github.com/microsoft/playwright-mcp"),
    CanaryCase("firecrawl", "https://github.com/firecrawl/firecrawl-mcp-server"),
    CanaryCase("github", "https://github.com/github/github-mcp-server"),
]

CASE_INDEX = {case.name: case for case in CASES}


def _summarize(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "server_name": record.get("server_name", ""),
        "status": record.get("status", ""),
        "status_label": record.get("status_label", ""),
        "last_error": record.get("last_error", ""),
        "tool_names": record.get("tool_names", []),
        "install_dir": record.get("install_dir", ""),
    }


def _select_cases(case_names: list[str] | None) -> list[CanaryCase]:
    if not case_names:
        return CASES

    selected: list[CanaryCase] = []
    for name in case_names:
        case = CASE_INDEX.get(name)
        if case is None:
            known = ", ".join(sorted(CASE_INDEX))
            raise ValueError(f"Unknown canary case '{name}'. Known cases: {known}")
        selected.append(case)
    return selected


def _print_summary(results: list[dict[str, Any]]) -> None:
    for item in results:
        name = str(item.get("name", "unknown"))
        ok = bool(item.get("ok"))
        marker = "PASS" if ok else "FAIL"
        status = ""
        if isinstance(item.get("result"), dict):
            status = str(item["result"].get("status_label", "") or item["result"].get("status", "")).strip()
        if not status and item.get("error"):
            status = str(item["error"])
        print(f"[{marker}] {name}: {status}")


def _is_expected_firecrawl(record: dict[str, Any], *, has_key: bool) -> bool:
    status = str(record.get("status", "")).strip()
    last_error = str(record.get("last_error", "")).strip()
    if has_key:
        return status == "active"
    return status in {"active", "needs_configuration"} or "FIRECRAWL_API_KEY" in last_error


def _is_expected_github(record: dict[str, Any], *, has_pat: bool) -> bool:
    status = str(record.get("status", "")).strip()
    last_error = str(record.get("last_error", "")).strip()
    if has_pat:
        return status == "active"
    return status == "active" or "401 Unauthorized" in last_error


async def _run_canary(
    *,
    output_path: Path,
    case_names: list[str] | None = None,
    firecrawl_api_key: str = "",
    github_pat: str = "",
) -> int:
    runtime_root = output_path.parent.parent / "live-canary-runtime"
    shutil.rmtree(runtime_root, ignore_errors=True)
    config_service = GUIConfigService(runtime_root / "runtime" / "config.json", str(runtime_root / "workspace"))
    config_service.ensure_instance()
    mcp_service = GUIMCPService(config_service, logging.getLogger("live-mcp-canary"))

    results: list[dict[str, Any]] = []

    for case in _select_cases(case_names):
        item: dict[str, Any] = {
            "name": case.name,
            "source": case.source,
            "ok": False,
        }
        try:
            analysis = await mcp_service.analyze_repository(case.source)
            item["analysis"] = {
                "server_name": analysis.get("server_name", ""),
                "install_mode": analysis.get("install_mode", ""),
                "transport": analysis.get("transport", ""),
                "run_command": analysis.get("run_command", ""),
                "run_args": analysis.get("run_args", []),
                "run_url": analysis.get("run_url", ""),
            }
            record = await mcp_service.install_repository(case.source)

            if case.name == "firecrawl" and firecrawl_api_key and record.get("status") != "active":
                config = config_service.load()
                config.tools.mcp_servers[analysis["server_name"]].env["FIRECRAWL_API_KEY"] = firecrawl_api_key
                config_service.save(config)
                record = await mcp_service.test_server(analysis["server_name"])

            if case.name == "github" and github_pat and record.get("status") != "active":
                config = config_service.load()
                headers = dict(config.tools.mcp_servers[analysis["server_name"]].headers or {})
                headers["Authorization"] = f"Bearer {github_pat}"
                config.tools.mcp_servers[analysis["server_name"]].headers = headers
                config_service.save(config)
                record = await mcp_service.test_server(analysis["server_name"])

            item["result"] = _summarize(record)

            if case.name == "firecrawl":
                item["ok"] = _is_expected_firecrawl(record, has_key=bool(firecrawl_api_key))
            elif case.name == "github":
                item["ok"] = _is_expected_github(record, has_pat=bool(github_pat))
            else:
                item["ok"] = record.get("status") == "active"
        except Exception as exc:  # pragma: no cover - failure reporting path
            item["error"] = f"{type(exc).__name__}: {exc}"
            item["ok"] = False
        results.append(item)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "selected_cases": [item["name"] for item in results],
                "results": results,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    _print_summary(results)
    print(json.dumps(results, indent=2))
    return 0 if all(result["ok"] for result in results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the live MCP repository canary.")
    parser.add_argument(
        "--case",
        action="append",
        dest="cases",
        help="Restrict the run to one or more named cases. Repeat this option to select multiple cases.",
    )
    parser.add_argument(
        "--list-cases",
        action="store_true",
        help="Print the available official MCP smoke-test cases and exit.",
    )
    parser.add_argument(
        "--output",
        default="test-results/live-mcp-canary.json",
        help="Where to write the JSON summary.",
    )
    parser.add_argument(
        "--firecrawl-api-key",
        default=os.getenv("FIRECRAWL_API_KEY", ""),
        help="Optional Firecrawl API key for authenticated retests.",
    )
    parser.add_argument(
        "--github-pat",
        default=os.getenv("GITHUB_MCP_PAT", ""),
        help="Optional GitHub PAT for authenticated remote GitHub MCP retests.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.list_cases:
        for case in CASES:
            print(f"{case.name}: {case.source}")
        return 0

    try:
        return asyncio.run(
            _run_canary(
                output_path=Path(args.output),
                case_names=args.cases,
                firecrawl_api_key=args.firecrawl_api_key,
                github_pat=args.github_pat,
            )
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":
    sys.exit(main())
