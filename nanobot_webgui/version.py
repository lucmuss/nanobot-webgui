"""Resolve the packaged nanobot-webgui version consistently."""

from __future__ import annotations

from importlib import metadata
from pathlib import Path
import tomllib


def _read_installed_version() -> str:
    """Return the installed distribution version when package metadata is available."""
    for package_name in ("nanobot-webgui", "nanobot_webgui"):
        try:
            return metadata.version(package_name)
        except metadata.PackageNotFoundError:
            continue
    return ""


def _read_pyproject_version() -> str:
    """Return the local project version for source-checkout runs."""
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    if not pyproject_path.exists():
        return ""
    try:
        with pyproject_path.open("rb") as handle:
            payload = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return ""
    project = payload.get("project")
    if not isinstance(project, dict):
        return ""
    return str(project.get("version", "")).strip()


def get_gui_version() -> str:
    """Return the current nanobot-webgui version string."""
    return _read_installed_version() or _read_pyproject_version() or "0.0.0"


GUI_VERSION = get_gui_version()
