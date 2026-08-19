#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any


CURRENT_SCHEMA = 2
OLD_SHIPPED_DEFAULTS: dict[str, Any] = {
    "h3_binary": "runtime/h3.c/h3",
    "model_root": "runtime/models/MiniMax-H3",
    "default_task": "Ref2VA",
    "output_subdir": "h3-jobs",
    "auto_ssd_streaming_ram_gib": 64,
    "auto_idle_seconds": 300,
    "auto_poll_seconds": 2,
    "auto_max_external_cpu_percent": 120,
    "auto_active_behavior": "background",
    "auto_require_ac_power": True,
    "keep_failed_output": True,
}


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Configuration must contain a JSON object: {path}")
    return value


def atomic_write(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def migrate(config_path: Path, example_path: Path) -> str:
    defaults = read_object(example_path)
    if not config_path.exists():
        atomic_write(config_path, defaults)
        return "created"

    current = read_object(config_path)
    try:
        schema = int(current.get("config_schema_version", 1))
    except (TypeError, ValueError):
        schema = 1
    if schema >= CURRENT_SCHEMA:
        return "unchanged"

    backup = config_path.with_name(f"{config_path.name}.v1-backup")
    if not backup.exists():
        shutil.copy2(config_path, backup)

    was_old_shipped_default = current == OLD_SHIPPED_DEFAULTS
    for key, value in defaults.items():
        current.setdefault(key, value)
    current["config_schema_version"] = CURRENT_SCHEMA
    if was_old_shipped_default:
        current["auto_active_behavior"] = "adaptive"
        current["auto_poll_seconds"] = defaults["auto_poll_seconds"]
    atomic_write(config_path, current)
    return "migrated-default" if was_old_shipped_default else "migrated-custom"


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate ComfyUI-H3-Mac config safely.")
    parser.add_argument("config", type=Path)
    parser.add_argument("example", type=Path)
    args = parser.parse_args()
    result = migrate(args.config, args.example)
    print(f"config: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
