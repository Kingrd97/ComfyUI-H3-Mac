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
    return BridgeConfig(
        project_root=project_root,
        h3_binary=_resolve(project_root, raw["h3_binary"]),
        model_root=_resolve(project_root, raw["model_root"]),
        default_task=str(raw.get("default_task", "Ref2VA")),
        output_subdir=str(raw.get("output_subdir", "h3-jobs")),
        auto_ssd_streaming_ram_gib=int(raw.get("auto_ssd_streaming_ram_gib", 64)),
        keep_failed_output=bool(raw.get("keep_failed_output", True)),
    )
