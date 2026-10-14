#!/usr/bin/env python3
"""Migrate legacy Docker configs into a schema-tolerant preference overlay."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import yaml
from newsletter_mcp.config import deep_merge, preference_overlay


DATA_CONFIG = Path("/data/config.yaml")
LOCAL_CONFIG = Path("/data/config.local.yaml")
SETTINGS_FILE = Path(os.getenv("NEWSLETTER_SETTINGS_FILE", "/data/settings.yaml"))
MIGRATION_ARCHIVE = Path("/data/migrations")
OFFICIAL_X_MIGRATION = MIGRATION_ARCHIVE / "official-x-app-bearer-v1"


def read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return value if isinstance(value, dict) else {}


def atomic_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".migrating")
    temporary.write_text(yaml.safe_dump(value, sort_keys=False, allow_unicode=True), encoding="utf-8")
    os.replace(temporary, path)


def backup(path: Path) -> Path:
    candidate = path.with_suffix(path.suffix + ".pre-migration")
    index = 1
    while candidate.exists():
        candidate = path.with_suffix(path.suffix + f".pre-migration-{index}")
        index += 1
    shutil.copy2(path, candidate)
    return candidate


def archive(path: Path) -> Path:
    MIGRATION_ARCHIVE.mkdir(parents=True, exist_ok=True)
    candidate = MIGRATION_ARCHIVE / (path.name + ".legacy")
    index = 1
    while candidate.exists():
        candidate = MIGRATION_ARCHIVE / (path.name + f".legacy-{index}")
        index += 1
    shutil.move(path, candidate)
    return candidate


def official_x_preferences(value: dict) -> tuple[dict, bool]:
    """Enable the replacement official X connection once, then honor later UI toggles."""
    if OFFICIAL_X_MIGRATION.exists():
        return value, False
    migrated = deep_merge(value, {"mcps": {"twitter": {"enabled": True}}})
    return migrated, migrated != value


def finish_official_x_migration() -> None:
    OFFICIAL_X_MIGRATION.parent.mkdir(parents=True, exist_ok=True)
    temporary = OFFICIAL_X_MIGRATION.with_name("." + OFFICIAL_X_MIGRATION.name + ".migrating")
    temporary.write_text("official hosted X MCP with app-only Bearer authentication\n", encoding="utf-8")
    os.replace(temporary, OFFICIAL_X_MIGRATION)


def main() -> None:
    if SETTINGS_FILE.exists():
        existing = read_yaml(SETTINGS_FILE)
        sanitized = preference_overlay(existing)
        sanitized, x_changed = official_x_preferences(sanitized)
        if sanitized != existing:
            saved = backup(SETTINGS_FILE)
            atomic_write(SETTINGS_FILE, sanitized)
            print(f"Sanitized {SETTINGS_FILE}; backup: {saved}", flush=True)
        if not OFFICIAL_X_MIGRATION.exists():
            finish_official_x_migration()
            if x_changed:
                print("Enabled the replacement official X MCP connection; later dashboard toggles are preserved.", flush=True)
        return

    # Older images copied their entire base configuration into the volume.
    # Fold both legacy files once, extract only user-controlled preferences,
    # and leave the originals as backups. Future starts ignore them completely.
    legacy = deep_merge(read_yaml(DATA_CONFIG), read_yaml(LOCAL_CONFIG))
    preferences, _ = official_x_preferences(preference_overlay(legacy))
    atomic_write(SETTINGS_FILE, preferences)
    finish_official_x_migration()
    archived = []
    for path in (DATA_CONFIG, LOCAL_CONFIG):
        if path.exists():
            archived.append(str(archive(path)))
    print(f"Initialized {SETTINGS_FILE} from legacy preferences; archive: {', '.join(archived) or 'none'}", flush=True)


if __name__ == "__main__":
    main()
