import importlib.util
from pathlib import Path

import yaml


def load_migration_module():
    path = Path(__file__).parents[1] / "scripts" / "migrate_docker_config.py"
    spec = importlib.util.spec_from_file_location("docker_config_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_official_x_migration_replaces_old_disabled_default_once(tmp_path):
    migration = load_migration_module()
    migration.DATA_CONFIG = tmp_path / "legacy-config.yaml"
    migration.LOCAL_CONFIG = tmp_path / "legacy-local.yaml"
    migration.SETTINGS_FILE = tmp_path / "settings.yaml"
    migration.MIGRATION_ARCHIVE = tmp_path / "migrations"
    migration.OFFICIAL_X_MIGRATION = migration.MIGRATION_ARCHIVE / "official-x-app-bearer-v1"
    migration.SETTINGS_FILE.write_text(
        yaml.safe_dump({"mcps": {"twitter": {"enabled": False}}}), encoding="utf-8")

    migration.main()
    first = yaml.safe_load(migration.SETTINGS_FILE.read_text(encoding="utf-8"))
    assert first["mcps"]["twitter"]["enabled"] is True
    assert migration.OFFICIAL_X_MIGRATION.exists()

    # A later explicit user opt-out must not be overwritten on every restart.
    first["mcps"]["twitter"]["enabled"] = False
    migration.SETTINGS_FILE.write_text(yaml.safe_dump(first), encoding="utf-8")
    migration.main()
    second = yaml.safe_load(migration.SETTINGS_FILE.read_text(encoding="utf-8"))
    assert second["mcps"]["twitter"]["enabled"] is False
