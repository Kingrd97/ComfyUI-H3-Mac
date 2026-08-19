from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class BridgeConfig:
    project_root: Path
    h3_binary: Path
    model_root: Path
    default_task: str = "Ref2VA"
    output_subdir: str = "h3-jobs"
    auto_ssd_streaming_ram_gib: int = 64
    auto_idle_seconds: float = 60.0
    auto_poll_seconds: float = 2.0
    auto_max_external_cpu_percent: float = 120.0
    auto_active_behavior: str = "pause"
    auto_require_ac_power: bool = True
    keep_failed_output: bool = True

    def model_dir(self, task: str | None = None) -> Path:
        # h3.c expects the snapshot root. FL2VA is the required base and
        # Ref2VA is an optional sibling selected when ordered refs are present.
        return self.model_root


def _resolve(project_root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (project_root / path).resolve()


def load_config(config_path: Path | None = None) -> BridgeConfig:
    project_root = PROJECT_ROOT
    selected = config_path or (project_root / "config.json")
    fallback = project_root / "config.example.json"
    source = selected if selected.exists() else fallback
    if not source.exists():
        raise FileNotFoundError(f"Missing configuration: {source}")

    raw: dict[str, Any] = json.loads(source.read_text(encoding="utf-8"))
    active_behavior = str(raw.get("auto_active_behavior", "pause"))
    if active_behavior not in {"pause", "background"}:
        raise ValueError("auto_active_behavior must be 'pause' or 'background'")
    return BridgeConfig(
        project_root=project_root,
        h3_binary=_resolve(project_root, raw["h3_binary"]),
        model_root=_resolve(project_root, raw["model_root"]),
        default_task=str(raw.get("default_task", "Ref2VA")),
        output_subdir=str(raw.get("output_subdir", "h3-jobs")),
        auto_ssd_streaming_ram_gib=int(raw.get("auto_ssd_streaming_ram_gib", 64)),
        auto_idle_seconds=float(raw.get("auto_idle_seconds", 60.0)),
        auto_poll_seconds=float(raw.get("auto_poll_seconds", 2.0)),
        auto_max_external_cpu_percent=float(
            raw.get("auto_max_external_cpu_percent", 120.0)
        ),
        auto_active_behavior=active_behavior,
        auto_require_ac_power=bool(raw.get("auto_require_ac_power", True)),
        keep_failed_output=bool(raw.get("keep_failed_output", True)),
    )
