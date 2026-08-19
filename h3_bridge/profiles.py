from __future__ import annotations

import os
import platform
from dataclasses import dataclass

from .models import QualityProfile, ResourceProfile


@dataclass(frozen=True)
class QualityOptions:
    steps: int
    layers: int
    reuse: int
    core_reuse: int


QUALITY_PROFILES: dict[QualityProfile, QualityOptions] = {
    "preview": QualityOptions(steps=4, layers=50, reuse=1, core_reuse=1),
    "balanced": QualityOptions(steps=20, layers=45, reuse=2, core_reuse=1),
    "quality": QualityOptions(steps=20, layers=50, reuse=1, core_reuse=1),
    "reference": QualityOptions(steps=50, layers=50, reuse=1, core_reuse=1),
}


def physical_ram_gib() -> float:
    if platform.system() != "Darwin":
        return 0.0
    try:
        return int(os.popen("sysctl -n hw.memsize").read().strip()) / (1024**3)
    except (OSError, ValueError):
        return 0.0


def should_stream(profile: ResourceProfile, threshold_gib: int) -> bool:
    if profile == "low":
        return True
    if profile == "max":
        return False
    ram = physical_ram_gib()
    return bool(ram and ram < threshold_gib)


def process_prefix(profile: ResourceProfile) -> list[str]:
    if platform.system() != "Darwin":
        return []
    if profile == "low":
        return ["/usr/sbin/taskpolicy", "-b", "-c", "background", "-d", "throttle", "/usr/bin/nice", "-n", "15"]
    if profile == "auto":
        return ["/usr/sbin/taskpolicy", "-b", "/usr/bin/nice", "-n", "10"]
    return []
