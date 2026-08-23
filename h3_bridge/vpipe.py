from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable


ProgressCallback = Callable[[int, int, str], None]
CancelCallback = Callable[[], bool]


@dataclass(frozen=True)
class VPipeConfig:
    binary: Path
    work_dir: Path
    model: str = "local/MiniMax-H3-FL2VA-8bit"
    lora: str = "larryvrh/MiniMax-H3-Turbo-Lora-v4-600-ema"
    output_subdir: str = "h3-jobs"
    low_memory_cap_mb: int = 12288
    low_wired_pool_mb: int = 8192


@dataclass(frozen=True)
class VPipeRequest:
    prompt: str
    first_frame: Path
    width: int = 960
    height: int = 544
    frames: int = 124
    fps: int = 24
    steps: int = 6
    seed: int = 42
    resource_profile: str = "low"
    enable_h3_audio: bool = False


@dataclass(frozen=True)
class VPipeResult:
    job_id: str
    output_path: Path
    job_dir: Path
    elapsed_seconds: float
    reused: bool


def load_vpipe_config(project_root: Path) -> VPipeConfig:
    """Load optional vpipe settings without changing the h3.c config schema."""

    selected = project_root / "config.json"
    fallback = project_root / "config.example.json"
    source = selected if selected.exists() else fallback
    raw = json.loads(source.read_text(encoding="utf-8"))

    configured = os.environ.get("VPIPE_BIN", str(raw.get("vpipe_binary", "vpipe")))
    discovered = shutil.which(configured)
    if discovered:
        binary = Path(discovered).resolve()
    else:
        candidate = Path(configured).expanduser()
        binary = candidate if candidate.is_absolute() else (project_root / candidate).resolve()

    configured_work = os.environ.get(
        "VPIPE_WORK_DIR", str(raw.get("vpipe_work_dir", "~/workspace/github/vpipe-work"))
    )
    work_dir = Path(configured_work).expanduser()
    if not work_dir.is_absolute():
        work_dir = (project_root / work_dir).resolve()

    output_subdir = str(raw.get("output_subdir", "h3-jobs"))
    return VPipeConfig(
        binary=binary,
        work_dir=work_dir,
        model=str(raw.get("vpipe_model", "local/MiniMax-H3-FL2VA-8bit")),
        lora=str(
            raw.get(
                "vpipe_lora", "larryvrh/MiniMax-H3-Turbo-Lora-v4-600-ema"
            )
        ),
        output_subdir=output_subdir,
        low_memory_cap_mb=int(raw.get("vpipe_low_memory_cap_mb", 12288)),
        low_wired_pool_mb=int(raw.get("vpipe_low_wired_pool_mb", 8192)),
    )


def _pipeline(config: VPipeConfig, request: VPipeRequest, output: Path) -> dict:
    audio_source = "audio-vae-decode" if request.enable_h3_audio else ""
    stages: list[dict] = [
        {
            "id": "model-select",
            "type": "model-select",
            "iports": [],
            "config": {"hf_dir": config.model},
        },
        {
            "id": "load-image",
            "type": "load-image",
            "iports": [],
            "config": {"url": [str(request.first_frame)]},
        },
        {
            "id": "first-frame",
            "type": "image-resample",
            "iports": [{"src": "load-image", "oport": 0}],
            "config": {
                "width": request.width,
                "height": request.height,
                "fit": "crop",
                "algorithm": "lanczos",
            },
        },
        {
            "id": "vae-encode-first",
            "type": "vae-encode",
            "iports": [
                {"src": "first-frame", "oport": 0},
                {"src": "model-select", "oport": 0},
            ],
            "config": {"unload_when_idle": "always"},
        },
        {
            "id": "text-prompt",
            "type": "text-prompt",
            "iports": [],
            "config": {"text": request.prompt},
        },
        {
            "id": "diffusion-conditioner",
            "type": "diffusion-conditioner",
            "iports": [
                {"src": "text-prompt", "oport": 0},
                {"src": "", "oport": 0},
                {"src": "model-select", "oport": 0},
            ],
            "config": {"unload_when_idle": "always"},
        },
        {
            "id": "minimax-h3-model-config",
            "type": "minimax-h3-model-config",
            "iports": [],
            "config": {
                "video_shift": 12.0,
                "audio_shift": 3.0,
                "condition_timestep": 1.0,
                "audio_seconds": 0.0,
                "lora": config.lora,
                "lora_scale": 1.0,
            },
        },
        {
            "id": "generate-video",
            "type": "generate-video",
            "iports": [
                {"src": "diffusion-conditioner", "oport": 0},
                {"src": "", "oport": 0},
                {"src": "model-select", "oport": 0},
                {"src": "", "oport": 0},
                {"src": "", "oport": 0},
                {"src": "vae-encode-first", "oport": 0},
                {"src": "", "oport": 0},
                {"src": "", "oport": 0},
                {"src": "", "oport": 0},
                {"src": "minimax-h3-model-config", "oport": 0},
            ],
            "config": {
                "height": request.height,
                "width": request.width,
                "frames": request.frames,
                "fps": request.fps,
                "steps": request.steps,
                "seed": request.seed,
                "i8_gemm": True,
                "unload_when_idle": "always",
            },
        },
        {
            "id": "vae-decode",
            "type": "vae-decode",
            "iports": [
                {"src": "generate-video", "oport": 0},
                {"src": "model-select", "oport": 0},
            ],
            "config": {},
        },
    ]
    if request.enable_h3_audio:
        stages.append(
            {
                "id": "audio-vae-decode",
                "type": "audio-vae-decode",
                "iports": [
                    {"src": "generate-video", "oport": 1},
                    {"src": "model-select", "oport": 0},
                ],
                "config": {},
            }
        )
    stages.extend(
        [
            {
                "id": "rgb-to-video",
                "type": "rgb-to-video",
                "iports": [{"src": "vae-decode", "oport": 0}],
                "config": {"fps": request.fps},
            },
            {
                "id": "save-video",
                "type": "save-video",
                "iports": [
                    {"src": "rgb-to-video", "oport": 0},
                    {"src": audio_source, "oport": 0},
                ],
                "config": {
                    "output_url": str(output),
                    "enable_video": True,
                    "enable_audio": request.enable_h3_audio,
                },
            },
        ]
    )
    return {"id": "comfyui-h3-vpipe-shot", "stages": stages, "subpipelines": []}


class VPipeRunner:
    def __init__(self, config: VPipeConfig):
        self.config = config

    def validate(self, request: VPipeRequest) -> None:
        if not request.prompt.strip():
            raise ValueError("Prompt must not be empty.")
        if not request.first_frame.is_file():
            raise FileNotFoundError(f"First-frame image not found: {request.first_frame}")
        if not self.config.binary.is_file() or not os.access(self.config.binary, os.X_OK):
            raise FileNotFoundError(
                f"vpipe executable not found: {self.config.binary}. Set VPIPE_BIN or config.json."
            )
        if not self.config.work_dir.is_dir():
            raise FileNotFoundError(
                f"vpipe work directory not found: {self.config.work_dir}. Set VPIPE_WORK_DIR or config.json."
            )
        if request.width % 32 or request.height % 32:
            raise ValueError("vpipe H3 width and height must be multiples of 32.")
        if request.frames < 22 or request.frames > 362:
            raise ValueError("vpipe H3 frame count must be between 22 and 362.")
        if request.fps != 24:
            raise ValueError("MiniMax H3 uses 24 fps in this integration.")
        if not 1 <= request.steps <= 60:
            raise ValueError("Steps must be between 1 and 60.")
        if request.resource_profile not in {"low", "max"}:
            raise ValueError("Resource profile must be low or max.")

    def _job_id(self, request: VPipeRequest) -> str:
        image_stat = request.first_frame.stat()
        payload = asdict(request)
        payload["first_frame"] = str(request.first_frame.resolve())
        payload["first_frame_size"] = image_stat.st_size
        payload["first_frame_mtime_ns"] = image_stat.st_mtime_ns
        payload["model"] = self.config.model
        payload["lora"] = self.config.lora
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return "vpipe-" + hashlib.sha256(encoded).hexdigest()[:20]

    def run(
        self,
        request: VPipeRequest,
        *,
        output_root: Path,
        progress: ProgressCallback | None = None,
        cancelled: CancelCallback | None = None,
        reuse_completed: bool = True,
    ) -> VPipeResult:
        self.validate(request)
        job_id = self._job_id(request)
        job_dir = output_root.resolve() / self.config.output_subdir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        output_path = job_dir / "result.mp4"
        if reuse_completed and output_path.is_file() and output_path.stat().st_size > 0:
            return VPipeResult(job_id, output_path, job_dir, 0.0, True)

        partial = job_dir / "result.partial.mp4"
        partial.unlink(missing_ok=True)
        pipeline_path = job_dir / "pipeline.vpipeline"
        request_path = job_dir / "request.json"
        log_path = job_dir / "engine.log"
        request_path.write_text(
            json.dumps(asdict(request), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        pipeline_path.write_text(
            json.dumps(_pipeline(self.config, request, partial), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        command = [str(self.config.binary)]
        if request.resource_profile == "low":
            taskpolicy = shutil.which("taskpolicy")
            if taskpolicy:
                command = [taskpolicy, "-b", *command]
            command.extend(
                [
                    "--memory-cap-mb",
                    str(self.config.low_memory_cap_mb),
                    "--wired-pool-mb",
                    str(self.config.low_wired_pool_mb),
                ]
            )
        command.extend(["--launch", str(pipeline_path)])
        if progress:
            progress(1, 100, "Starting vpipe")
        started = time.monotonic()
        with log_path.open("w", encoding="utf-8") as log:
            log.write("COMMAND " + json.dumps(command, ensure_ascii=False) + "\n")
            log.flush()
            process = subprocess.Popen(
                command,
                cwd=self.config.work_dir,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            while process.poll() is None:
                if cancelled and cancelled():
                    process.terminate()
                    process.wait(timeout=10)
                    raise RuntimeError("vpipe generation cancelled.")
                time.sleep(0.25)
            returncode = process.returncode
        elapsed = time.monotonic() - started
        if returncode != 0:
            partial.unlink(missing_ok=True)
            raise RuntimeError(f"vpipe failed with exit code {returncode}. See {log_path}")
        if not partial.is_file() or partial.stat().st_size == 0:
            raise RuntimeError(f"vpipe finished without a video. See {log_path}")
        partial.replace(output_path)
        if progress:
            progress(100, 100, "vpipe complete")
        return VPipeResult(job_id, output_path, job_dir, elapsed, False)
