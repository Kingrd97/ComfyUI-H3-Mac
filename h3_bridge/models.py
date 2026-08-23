from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal


ReferenceKind = Literal["image", "silent_video", "video", "video_audio", "audio"]
ResourceProfile = Literal["low", "auto", "max"]
QualityProfile = Literal["preview", "balanced", "quality", "reference"]


@dataclass(frozen=True)
class H3Reference:
    kind: ReferenceKind
    path: Path
    audio_path: Path | None = None

    def to_json(self) -> dict[str, str | None]:
        return {
            "kind": self.kind,
            "path": str(self.path),
            "audio_path": str(self.audio_path) if self.audio_path else None,
        }


@dataclass(frozen=True)
class H3Request:
    prompt: str
    task: str = "Ref2VA"
    width: int = 640
    height: int = 384
    seconds: float = 5.0
    fps: int = 24
    quality_profile: QualityProfile = "quality"
    resource_profile: ResourceProfile = "low"
    seed: int = -1
    references: tuple[H3Reference, ...] = field(default_factory=tuple)
    first_frame: Path | None = None
    last_frame: Path | None = None

    @property
    def frames(self) -> int:
        return max(1, round(self.seconds * self.fps))

    def to_json(self) -> dict[str, object]:
        payload = asdict(self)
        payload["references"] = [item.to_json() for item in self.references]
        payload["first_frame"] = str(self.first_frame) if self.first_frame else None
        payload["last_frame"] = str(self.last_frame) if self.last_frame else None
        payload["frames"] = self.frames
        return payload


@dataclass(frozen=True)
class H3Result:
    job_id: str
    output_path: Path
    job_dir: Path
    elapsed_seconds: float
    command: tuple[str, ...]
