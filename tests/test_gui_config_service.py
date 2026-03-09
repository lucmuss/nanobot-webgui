from pathlib import Path

from nanobot.gui.config_service import GUIConfigService


def test_ensure_instance_syncs_branding_assets(tmp_path: Path):
    config_path = tmp_path / "instance" / "config.json"
    workspace_path = tmp_path / "workspace"
    service = GUIConfigService(config_path, str(workspace_path))

    service.ensure_instance()

    assert service.branding_banner_path.exists()
    assert service.branding_banner_path.read_bytes()
