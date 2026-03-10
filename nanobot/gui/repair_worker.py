"""Bounded MCP repair recipes and worker execution helpers."""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any


REPAIR_RECIPE_DETAILS: dict[str, dict[str, Any]] = {
    "install_node": {
        "label": "Install Node runtime",
        "description": "Install node, npm, and npx support for npm-based MCP servers.",
        "repairs": ["node", "npm", "npx"],
    },
    "install_uv": {
        "label": "Install uv runtime",
        "description": "Install uv and uvx for Python MCP projects that expect the Astral toolchain.",
        "repairs": ["uv", "uvx"],
    },
    "install_python_build_tools": {
        "label": "Install Python build tools",
        "description": "Install python, pip, and common build tooling for Python MCP servers.",
        "repairs": ["python", "pip"],
    },
    "install_docker_cli": {
        "label": "Install Docker CLI",
        "description": "Install the Docker CLI for OCI-based or docker-run MCP runtimes.",
        "repairs": ["docker"],
    },
    "unrestricted_agent_shell": {
        "label": "Unrestricted agent shell",
        "description": "Dangerous mode. Run the AI-proposed shell command exactly as returned.",
        "repairs": [],
        "dangerous": True,
    },
}


def supported_repair_recipes(missing_runtimes: list[str]) -> list[str]:
    """Return the deterministic repair recipes that can fix the missing runtimes."""
    ordered: list[str] = []
    normalized = [str(item).strip().lower() for item in missing_runtimes if str(item).strip()]
    if any(item in {"node", "npm", "npx"} for item in normalized):
        ordered.append("install_node")
    if any(item in {"uv", "uvx"} for item in normalized):
        ordered.append("install_uv")
    if any(item in {"python", "pip"} for item in normalized):
        ordered.append("install_python_build_tools")
    if any(item == "docker" for item in normalized):
        ordered.append("install_docker_cli")
    return ordered


def run_repair_recipe(
    recipe: str,
    *,
    allow_unrestricted: bool = False,
    shell_command: str = "",
    timeout: int = 1200,
) -> dict[str, Any]:
    """Execute one supported repair recipe and return a structured result."""
    normalized_recipe = str(recipe or "").strip()
    if normalized_recipe not in REPAIR_RECIPE_DETAILS:
        raise ValueError(f"Unsupported repair recipe: {normalized_recipe}")

    if normalized_recipe == "unrestricted_agent_shell":
        if not allow_unrestricted:
            raise ValueError("Unrestricted Agent + Shell mode is not enabled.")
        command_text = str(shell_command or "").strip()
        if not command_text:
            raise ValueError("The unrestricted repair plan did not provide a shell command.")
        return _run_recipe_commands(
            normalized_recipe,
            [["/bin/bash", "-lc", command_text]],
            timeout=timeout,
        )

    return _run_recipe_commands(normalized_recipe, _recipe_commands(normalized_recipe), timeout=timeout)


def _recipe_commands(recipe: str) -> list[list[str]]:
    """Return the command sequence for one bounded recipe."""
    package_manager = _detect_package_manager()
    commands: list[list[str]] = []

    if recipe == "install_node":
        if package_manager == "apt":
            commands.extend(
                [
                    ["apt-get", "update"],
                    ["apt-get", "install", "-y", "--no-install-recommends", "nodejs", "npm"],
                ]
            )
        elif package_manager == "apk":
            commands.append(["apk", "add", "--no-cache", "nodejs", "npm"])
        else:
            raise ValueError("No supported package manager was found for install_node.")
    elif recipe == "install_uv":
        if shutil.which("python3") is None and shutil.which("python") is None:
            raise ValueError("Python is required before uv can be installed.")
        pip_command = _python_pip_install_prefix()
        commands.append([*pip_command, "install", "--upgrade", "uv"])
    elif recipe == "install_python_build_tools":
        if package_manager == "apt":
            commands.extend(
                [
                    ["apt-get", "update"],
                    ["apt-get", "install", "-y", "--no-install-recommends", "python3", "python3-pip", "build-essential"],
                ]
            )
        elif package_manager == "apk":
            commands.append(["apk", "add", "--no-cache", "python3", "py3-pip", "build-base"])
        else:
            raise ValueError("No supported package manager was found for install_python_build_tools.")
    elif recipe == "install_docker_cli":
        if package_manager == "apt":
            commands.extend(
                [
                    ["apt-get", "update"],
                    ["apt-get", "install", "-y", "--no-install-recommends", "docker.io"],
                ]
            )
        elif package_manager == "apk":
            commands.append(["apk", "add", "--no-cache", "docker-cli"])
        else:
            raise ValueError("No supported package manager was found for install_docker_cli.")
    else:
        raise ValueError(f"Unsupported repair recipe: {recipe}")

    return commands


def _run_recipe_commands(recipe: str, commands: list[list[str]], *, timeout: int) -> dict[str, Any]:
    """Run the command list for one recipe and capture compact logs."""
    logs: list[str] = []
    for command in commands:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=os.environ.copy(),
        )
        if result.stdout.strip():
            logs.append(f"$ {' '.join(command)}\n{result.stdout.strip()}")
        elif result.stderr.strip():
            logs.append(f"$ {' '.join(command)}\n{result.stderr.strip()}")
        else:
            logs.append(f"$ {' '.join(command)}\n(no output)")
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or f"Recipe exited with {result.returncode}."
            return {
                "ok": False,
                "recipe": recipe,
                "log": "\n\n".join(logs)[-6000:],
                "error": message,
            }
    return {
        "ok": True,
        "recipe": recipe,
        "log": "\n\n".join(logs)[-6000:],
        "error": "",
    }


def _detect_package_manager() -> str:
    """Detect the package manager available in the current runtime."""
    if shutil.which("apt-get"):
        return "apt"
    if shutil.which("apk"):
        return "apk"
    return ""


def _python_pip_install_prefix() -> list[str]:
    """Return the safest Python + pip prefix available in the runtime."""
    if shutil.which("python3"):
        return ["python3", "-m", "pip"]
    if shutil.which("python"):
        return ["python", "-m", "pip"]
    raise ValueError("Python is not available in the current runtime.")
