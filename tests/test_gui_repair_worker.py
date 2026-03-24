"""Tests for nanobot_webgui.repair_worker."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from nanobot_webgui.repair_worker import (
    REPAIR_RECIPE_DETAILS,
    _detect_package_manager,
    _python_pip_install_prefix,
    run_repair_recipe,
    supported_repair_recipes,
)


# ---------------------------------------------------------------------------
# supported_repair_recipes
# ---------------------------------------------------------------------------


def test_supported_recipes_node():
    assert supported_repair_recipes(["node"]) == ["install_node"]


def test_supported_recipes_npm():
    assert supported_repair_recipes(["npm"]) == ["install_node"]


def test_supported_recipes_npx():
    assert supported_repair_recipes(["npx"]) == ["install_node"]


def test_supported_recipes_uv():
    assert supported_repair_recipes(["uv"]) == ["install_uv"]


def test_supported_recipes_uvx():
    assert supported_repair_recipes(["uvx"]) == ["install_uv"]


def test_supported_recipes_python():
    assert supported_repair_recipes(["python"]) == ["install_python_build_tools"]


def test_supported_recipes_pip():
    assert supported_repair_recipes(["pip"]) == ["install_python_build_tools"]


def test_supported_recipes_docker():
    assert supported_repair_recipes(["docker"]) == ["install_docker_cli"]


def test_supported_recipes_multiple_runtimes():
    result = supported_repair_recipes(["node", "uv"])
    assert "install_node" in result
    assert "install_uv" in result


def test_supported_recipes_empty_list():
    assert supported_repair_recipes([]) == []


def test_supported_recipes_unknown_runtime():
    assert supported_repair_recipes(["ruby", "go"]) == []


def test_supported_recipes_case_insensitive():
    assert supported_repair_recipes(["NODE", "NPM"]) == ["install_node"]


def test_supported_recipes_deduplicates_node():
    result = supported_repair_recipes(["node", "npm", "npx"])
    assert result.count("install_node") == 1


# ---------------------------------------------------------------------------
# run_repair_recipe — guard rails
# ---------------------------------------------------------------------------


def test_run_repair_recipe_unknown_raises():
    with pytest.raises(ValueError, match="Unsupported repair recipe"):
        run_repair_recipe("delete_everything")


def test_run_repair_recipe_empty_raises():
    with pytest.raises(ValueError, match="Unsupported repair recipe"):
        run_repair_recipe("")


def test_run_repair_recipe_unrestricted_requires_flag():
    with pytest.raises(ValueError, match="not enabled"):
        run_repair_recipe("unrestricted_agent_shell", allow_unrestricted=False)


def test_run_repair_recipe_unrestricted_empty_command():
    with pytest.raises(ValueError, match="shell command"):
        run_repair_recipe("unrestricted_agent_shell", allow_unrestricted=True, shell_command="")


def test_run_repair_recipe_unrestricted_whitespace_command():
    with pytest.raises(ValueError, match="shell command"):
        run_repair_recipe("unrestricted_agent_shell", allow_unrestricted=True, shell_command="   ")


def test_run_repair_recipe_unrestricted_executes(monkeypatch):
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)

        class FakeResult:
            returncode = 0
            stdout = "done"
            stderr = ""

        return FakeResult()

    monkeypatch.setattr("subprocess.run", fake_run)
    result = run_repair_recipe(
        "unrestricted_agent_shell",
        allow_unrestricted=True,
        shell_command="echo hello",
    )
    assert result["ok"] is True
    assert calls[0] == ["/bin/bash", "-lc", "echo hello"]


# ---------------------------------------------------------------------------
# run_repair_recipe — bounded recipes via mocked subprocess
# ---------------------------------------------------------------------------


def _fake_run_success(command, **_kwargs):
    class Result:
        returncode = 0
        stdout = "ok"
        stderr = ""

    return Result()


def _fake_run_failure(command, **_kwargs):
    class Result:
        returncode = 1
        stdout = ""
        stderr = "failed"

    return Result()


def test_run_repair_recipe_install_uv_success(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name if name in ("python3",) else None)
    monkeypatch.setattr("subprocess.run", _fake_run_success)
    result = run_repair_recipe("install_uv")
    assert result["ok"] is True
    assert result["recipe"] == "install_uv"
    assert result["error"] == ""


def test_run_repair_recipe_install_uv_no_python_raises(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _name: None)
    with pytest.raises(ValueError, match="Python is required"):
        run_repair_recipe("install_uv")


def test_run_repair_recipe_install_node_apt(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/apt-get" if name == "apt-get" else None)
    monkeypatch.setattr("subprocess.run", _fake_run_success)
    result = run_repair_recipe("install_node")
    assert result["ok"] is True


def test_run_repair_recipe_install_node_apk(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/sbin/apk" if name == "apk" else None)
    monkeypatch.setattr("subprocess.run", _fake_run_success)
    result = run_repair_recipe("install_node")
    assert result["ok"] is True


def test_run_repair_recipe_install_node_no_package_manager(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _name: None)
    with pytest.raises(ValueError, match="No supported package manager"):
        run_repair_recipe("install_node")


def test_run_repair_recipe_failure_returns_ok_false(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/apt-get" if name == "apt-get" else None)
    monkeypatch.setattr("subprocess.run", _fake_run_failure)
    result = run_repair_recipe("install_node")
    assert result["ok"] is False
    assert result["error"] != ""


def test_run_repair_recipe_result_contains_log(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/apt-get" if name == "apt-get" else None)
    monkeypatch.setattr("subprocess.run", _fake_run_success)
    result = run_repair_recipe("install_node")
    assert "log" in result
    assert "apt-get" in result["log"]


# ---------------------------------------------------------------------------
# _detect_package_manager
# ---------------------------------------------------------------------------


def test_detect_package_manager_apt(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/apt-get" if name == "apt-get" else None)
    assert _detect_package_manager() == "apt"


def test_detect_package_manager_apk(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/sbin/apk" if name == "apk" else None)
    assert _detect_package_manager() == "apk"


def test_detect_package_manager_none(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _name: None)
    assert _detect_package_manager() == ""


# ---------------------------------------------------------------------------
# _python_pip_install_prefix
# ---------------------------------------------------------------------------


def test_python_pip_prefix_python3(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/python3" if name == "python3" else None)
    assert _python_pip_install_prefix() == ["python3", "-m", "pip"]


def test_python_pip_prefix_fallback_python(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/python" if name == "python" else None)
    assert _python_pip_install_prefix() == ["python", "-m", "pip"]


def test_python_pip_prefix_no_python_raises(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _name: None)
    with pytest.raises(ValueError, match="Python is not available"):
        _python_pip_install_prefix()


# ---------------------------------------------------------------------------
# REPAIR_RECIPE_DETAILS integrity
# ---------------------------------------------------------------------------


def test_repair_recipe_details_all_have_label():
    for name, details in REPAIR_RECIPE_DETAILS.items():
        assert "label" in details, f"{name} missing label"
        assert "description" in details, f"{name} missing description"
        assert "repairs" in details, f"{name} missing repairs"
