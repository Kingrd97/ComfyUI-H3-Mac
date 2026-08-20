from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class BridgeConfig:
    project_root: Path
    h3_binary: Path
    model_root: Path
    expected_model_revision: str = ""
    # Intended only for hermetic tests with synthetic weights. Production
    # load_config() never enables this escape hatch.
    allow_unmanaged_model: bool = False
    default_task: str = "Ref2VA"
    output_subdir: str = "h3-jobs"
    auto_ssd_streaming_ram_gib: int = 64
    auto_idle_seconds: float = 300.0
    auto_poll_seconds: float = 0.5
    auto_metrics_poll_seconds: float = 2.0
    auto_health_poll_seconds: float = 10.0
    auto_status_interval_seconds: float = 15.0
    auto_max_external_cpu_percent: float = 120.0
    auto_active_behavior: str = "adaptive"
    auto_require_ac_power: bool = True
    auto_jank_interaction_seconds: float = 5.0
    auto_jank_pause_seconds: float = 2.0
    auto_jank_recover_seconds: float = 15.0
    auto_jank_probe_seconds: float = 20.0
    auto_jank_cpu_percent: float = 300.0
    auto_jank_window_server_percent: float = 80.0
    auto_jank_window_server_recover_percent: float = 50.0
    auto_jank_gpu_percent: float = 92.0
    auto_jank_gpu_recover_percent: float = 70.0
    auto_memory_pause_percent: float = 8.0
    auto_memory_recover_percent: float = 15.0
    auto_swap_growth_pause_mib_per_minute: float = 512.0
    auto_pageout_pause_mib_per_minute: float = 256.0
    keep_failed_output: bool = True

    def model_dir(self, task: str | None = None) -> Path:
        # h3.c expects the snapshot root. FL2VA is the required base and
        # Ref2VA is an optional sibling selected when ordered refs are present.
        return self.model_root


def _resolve(project_root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (project_root / path).resolve()


def _version_value(project_root: Path, name: str) -> str:
    versions_path = project_root / "versions.env"
    try:
        lines = versions_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError(f"Missing pinned-version file: {versions_path}") from exc
    prefix = f"{name}="
    for line in lines:
        if line.startswith(prefix):
            value = line[len(prefix) :].strip()
            if value:
                return value
    raise RuntimeError(f"Missing {name} in {versions_path}")


def load_config(config_path: Path | None = None) -> BridgeConfig:
    project_root = PROJECT_ROOT
    selected = config_path or (project_root / "config.json")
    fallback = project_root / "config.example.json"
    source = selected if selected.exists() else fallback
    if not source.exists():
        raise FileNotFoundError(f"Missing configuration: {source}")

    raw: dict[str, Any] = json.loads(source.read_text(encoding="utf-8"))
    output_subdir = str(raw.get("output_subdir", "h3-jobs"))
    if (
        not output_subdir
        or output_subdir in {".", ".."}
        or Path(output_subdir).name != output_subdir
        or "/" in output_subdir
        or "\\" in output_subdir
    ):
        raise ValueError("output_subdir must be one safe path component")
    active_behavior = str(raw.get("auto_active_behavior", "adaptive"))
    if active_behavior not in {"adaptive", "pause", "background"}:
        raise ValueError(
            "auto_active_behavior must be 'adaptive', 'pause', or 'background'"
        )
    auto_poll_seconds = float(raw.get("auto_poll_seconds", 0.5))
    auto_metrics_poll_seconds = float(raw.get("auto_metrics_poll_seconds", 2.0))
    auto_health_poll_seconds = float(raw.get("auto_health_poll_seconds", 10.0))
    auto_status_interval_seconds = float(
        raw.get("auto_status_interval_seconds", 15.0)
    )
    auto_idle_seconds = float(raw.get("auto_idle_seconds", 300.0))
    auto_max_external_cpu_percent = float(
        raw.get("auto_max_external_cpu_percent", 120.0)
    )
    auto_jank_interaction_seconds = float(
        raw.get("auto_jank_interaction_seconds", 5.0)
    )
    auto_jank_pause_seconds = float(raw.get("auto_jank_pause_seconds", 2.0))
    auto_jank_recover_seconds = float(raw.get("auto_jank_recover_seconds", 15.0))
    auto_jank_probe_seconds = float(raw.get("auto_jank_probe_seconds", 20.0))
    auto_jank_cpu_percent = float(raw.get("auto_jank_cpu_percent", 300.0))
    auto_jank_window_server_percent = float(
        raw.get("auto_jank_window_server_percent", 80.0)
    )
    auto_jank_window_server_recover_percent = float(
        raw.get("auto_jank_window_server_recover_percent", 50.0)
    )
    auto_jank_gpu_percent = float(raw.get("auto_jank_gpu_percent", 92.0))
    auto_jank_gpu_recover_percent = float(
        raw.get("auto_jank_gpu_recover_percent", 70.0)
    )
    auto_memory_pause_percent = float(raw.get("auto_memory_pause_percent", 8.0))
    auto_memory_recover_percent = float(
        raw.get("auto_memory_recover_percent", 15.0)
    )
    auto_swap_growth_pause_mib_per_minute = float(
        raw.get("auto_swap_growth_pause_mib_per_minute", 512.0)
    )
    auto_pageout_pause_mib_per_minute = float(
        raw.get("auto_pageout_pause_mib_per_minute", 256.0)
    )
    numeric_values = (
        auto_poll_seconds,
        auto_metrics_poll_seconds,
        auto_health_poll_seconds,
        auto_status_interval_seconds,
        auto_idle_seconds,
        auto_max_external_cpu_percent,
        auto_jank_interaction_seconds,
        auto_jank_pause_seconds,
        auto_jank_recover_seconds,
        auto_jank_probe_seconds,
        auto_jank_cpu_percent,
        auto_jank_window_server_percent,
        auto_jank_window_server_recover_percent,
        auto_jank_gpu_percent,
        auto_jank_gpu_recover_percent,
        auto_memory_pause_percent,
        auto_memory_recover_percent,
        auto_swap_growth_pause_mib_per_minute,
        auto_pageout_pause_mib_per_minute,
    )
    if not all(math.isfinite(value) for value in numeric_values):
        raise ValueError("auto scheduling values must be finite numbers")
    if min(
        auto_poll_seconds,
        auto_metrics_poll_seconds,
        auto_health_poll_seconds,
        auto_status_interval_seconds,
    ) <= 0:
        raise ValueError("auto polling intervals must be greater than zero")
    if min(
        auto_idle_seconds,
        auto_max_external_cpu_percent,
        auto_jank_interaction_seconds,
    ) < 0:
        raise ValueError(
            "auto idle, CPU, and interaction thresholds cannot be negative"
        )
    if min(
        auto_jank_pause_seconds,
        auto_jank_recover_seconds,
        auto_jank_probe_seconds,
    ) < 0:
        raise ValueError("auto jank timing values cannot be negative")
    if auto_jank_cpu_percent <= auto_max_external_cpu_percent:
        raise ValueError(
            "auto_jank_cpu_percent must exceed auto_max_external_cpu_percent"
        )
    if not (
        0.0
        <= auto_jank_window_server_recover_percent
        < auto_jank_window_server_percent
    ):
        raise ValueError(
            "WindowServer recovery threshold must be below its pause threshold"
        )
    if not 0.0 <= auto_jank_window_server_percent <= 1000.0:
        raise ValueError("WindowServer pause threshold must be between 0 and 1000")
    if not 0.0 <= auto_jank_gpu_recover_percent < auto_jank_gpu_percent <= 100.0:
        raise ValueError("GPU recovery threshold must be below its pause threshold")
    if not 0.0 <= auto_memory_pause_percent < auto_memory_recover_percent <= 100.0:
        raise ValueError(
            "Memory recovery threshold must exceed its pause threshold"
        )
    if min(
        auto_swap_growth_pause_mib_per_minute,
        auto_pageout_pause_mib_per_minute,
    ) <= 0:
        raise ValueError("Swap and pageout growth thresholds must be positive")
    return BridgeConfig(
        project_root=project_root,
        h3_binary=_resolve(project_root, raw["h3_binary"]),
        model_root=_resolve(project_root, raw["model_root"]),
        expected_model_revision=_version_value(project_root, "H3_MODEL_REF"),
        default_task=str(raw.get("default_task", "Ref2VA")),
        output_subdir=output_subdir,
        auto_ssd_streaming_ram_gib=int(raw.get("auto_ssd_streaming_ram_gib", 64)),
        auto_idle_seconds=auto_idle_seconds,
        auto_poll_seconds=auto_poll_seconds,
        auto_metrics_poll_seconds=auto_metrics_poll_seconds,
        auto_health_poll_seconds=auto_health_poll_seconds,
        auto_status_interval_seconds=auto_status_interval_seconds,
        auto_max_external_cpu_percent=auto_max_external_cpu_percent,
        auto_active_behavior=active_behavior,
        auto_require_ac_power=bool(raw.get("auto_require_ac_power", True)),
        auto_jank_interaction_seconds=auto_jank_interaction_seconds,
        auto_jank_pause_seconds=auto_jank_pause_seconds,
        auto_jank_recover_seconds=auto_jank_recover_seconds,
        auto_jank_probe_seconds=auto_jank_probe_seconds,
        auto_jank_cpu_percent=auto_jank_cpu_percent,
        auto_jank_window_server_percent=auto_jank_window_server_percent,
        auto_jank_window_server_recover_percent=(
            auto_jank_window_server_recover_percent
        ),
        auto_jank_gpu_percent=auto_jank_gpu_percent,
        auto_jank_gpu_recover_percent=auto_jank_gpu_recover_percent,
        auto_memory_pause_percent=auto_memory_pause_percent,
        auto_memory_recover_percent=auto_memory_recover_percent,
        auto_swap_growth_pause_mib_per_minute=(
            auto_swap_growth_pause_mib_per_minute
        ),
        auto_pageout_pause_mib_per_minute=(
            auto_pageout_pause_mib_per_minute
        ),
        keep_failed_output=bool(raw.get("keep_failed_output", True)),
    )
