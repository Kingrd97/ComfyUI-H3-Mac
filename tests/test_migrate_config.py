from __future__ import annotations

import json
from pathlib import Path

from scripts.migrate_config import OLD_SHIPPED_DEFAULTS, migrate


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "config.example.json"


def write_json(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_exact_legacy_default_is_backed_up_and_migrated(tmp_path: Path):
    config_path = tmp_path / "config.json"
    write_json(config_path, OLD_SHIPPED_DEFAULTS)

    assert migrate(config_path, EXAMPLE) == "migrated-default"

    migrated = json.loads(config_path.read_text(encoding="utf-8"))
    backup = json.loads(
        (tmp_path / "config.json.v1-backup").read_text(encoding="utf-8")
    )
    assert migrated["config_schema_version"] == 2
    assert migrated["auto_active_behavior"] == "adaptive"
    assert migrated["auto_poll_seconds"] == 0.5
    assert migrated["auto_jank_pause_seconds"] == 2
    assert backup == OLD_SHIPPED_DEFAULTS


def test_missing_config_is_created_from_current_example(tmp_path: Path):
    config_path = tmp_path / "config.json"

    assert migrate(config_path, EXAMPLE) == "created"
    assert json.loads(config_path.read_text(encoding="utf-8")) == json.loads(
        EXAMPLE.read_text(encoding="utf-8")
    )


def test_custom_legacy_policy_is_preserved(tmp_path: Path):
    config_path = tmp_path / "config.json"
    custom = dict(OLD_SHIPPED_DEFAULTS)
    custom["auto_max_external_cpu_percent"] = 80
    write_json(config_path, custom)

    assert migrate(config_path, EXAMPLE) == "migrated-custom"

    migrated = json.loads(config_path.read_text(encoding="utf-8"))
    assert migrated["config_schema_version"] == 2
    assert migrated["auto_active_behavior"] == "background"
    assert migrated["auto_poll_seconds"] == 2
    assert migrated["auto_max_external_cpu_percent"] == 80
    assert migrated["auto_jank_pause_seconds"] == 2


def test_schema_two_migration_is_idempotent(tmp_path: Path):
    config_path = tmp_path / "config.json"
    current = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    current["auto_active_behavior"] = "pause"
    write_json(config_path, current)

    assert migrate(config_path, EXAMPLE) == "unchanged"
    assert json.loads(config_path.read_text(encoding="utf-8")) == current
    assert not (tmp_path / "config.json.v1-backup").exists()


def test_existing_backup_is_not_overwritten(tmp_path: Path):
    config_path = tmp_path / "config.json"
    backup_path = tmp_path / "config.json.v1-backup"
    write_json(config_path, OLD_SHIPPED_DEFAULTS)
    backup_path.write_text("keep-me", encoding="utf-8")

    assert migrate(config_path, EXAMPLE) == "migrated-default"
    assert backup_path.read_text(encoding="utf-8") == "keep-me"


def test_install_and_start_both_apply_config_migration():
    for script_name in ("install.sh", "start.sh"):
        script = (ROOT / "scripts" / script_name).read_text(encoding="utf-8")
        assert "scripts/migrate_config.py" in script
