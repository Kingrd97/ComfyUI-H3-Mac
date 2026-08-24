from __future__ import annotations

import hashlib
import fcntl
import json
import math
import os
import queue
import shutil
import subprocess
import threading
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
    project_root: Path | None = None
    model: str = "local/MiniMax-H3-FL2VA-8bit"
    lora: str = "larryvrh/MiniMax-H3-Turbo-Lora-v4-600-ema"
    lora_768p: str = "lightx2v/Minimax-h3-Turbo-4step-768p"
    output_subdir: str = "h3-jobs"
    low_memory_cap_mb: int = 12288
    low_wired_pool_mb: int = 8192
    worker_enabled: bool = False
    worker_heartbeat_timeout_seconds: float = 15.0
    worker_cooldown_seconds: float = 90.0
    worker_memory_poll_seconds: float = 5.0
    worker_memory_stable_samples: int = 3
    worker_min_memory_free_percent: float = 20.0
    worker_min_reclaimable_mb: int = 6144
    worker_max_wired_percent: float = 18.0
    worker_memory_retry_limit: int = 1


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
    adapter_profile: str = "turbo_544p"


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
        local_candidate = Path.home() / ".local" / "bin" / candidate
        if (
            not candidate.is_absolute()
            and candidate.name == str(candidate)
            and local_candidate.is_file()
        ):
            binary = local_candidate.resolve()
        else:
            binary = (
                candidate
                if candidate.is_absolute()
                else (project_root / candidate).resolve()
            )

    configured_work = os.environ.get(
        "VPIPE_WORK_DIR", str(raw.get("vpipe_work_dir", "~/workspace/github/vpipe-work"))
    )
    work_dir = Path(configured_work).expanduser()
    if not work_dir.is_absolute():
        work_dir = (project_root / work_dir).resolve()

    output_subdir = str(raw.get("output_subdir", "h3-jobs"))
    worker_cooldown_seconds = float(
        raw.get("vpipe_worker_cooldown_seconds", 90.0)
    )
    worker_memory_poll_seconds = float(
        raw.get("vpipe_worker_memory_poll_seconds", 5.0)
    )
    worker_memory_stable_samples = int(
        raw.get("vpipe_worker_memory_stable_samples", 3)
    )
    worker_min_memory_free_percent = float(
        raw.get("vpipe_worker_min_memory_free_percent", 20.0)
    )
    worker_min_reclaimable_mb = int(
        raw.get("vpipe_worker_min_reclaimable_mb", 6144)
    )
    worker_max_wired_percent = float(
        raw.get("vpipe_worker_max_wired_percent", 18.0)
    )
    worker_memory_retry_limit = int(
        raw.get("vpipe_worker_memory_retry_limit", 1)
    )
    if not all(
        math.isfinite(value)
        for value in (
            worker_cooldown_seconds,
            worker_memory_poll_seconds,
            worker_min_memory_free_percent,
            worker_max_wired_percent,
        )
    ):
        raise ValueError("vpipe worker memory-gate values must be finite")
    if worker_cooldown_seconds < 0 or worker_memory_poll_seconds <= 0:
        raise ValueError("vpipe worker cooldown must be non-negative and poll positive")
    if worker_memory_stable_samples < 1:
        raise ValueError("vpipe worker stable samples must be at least one")
    if not 0 <= worker_min_memory_free_percent <= 100:
        raise ValueError("vpipe worker minimum memory-free percent is invalid")
    if not 0 < worker_max_wired_percent <= 100:
        raise ValueError("vpipe worker maximum wired percent is invalid")
    if worker_min_reclaimable_mb < 0 or worker_memory_retry_limit < 0:
        raise ValueError("vpipe worker reclaimable memory and retries cannot be negative")

    return VPipeConfig(
        binary=binary,
        work_dir=work_dir,
        project_root=project_root.resolve(),
        model=str(raw.get("vpipe_model", "local/MiniMax-H3-FL2VA-8bit")),
        lora=str(
            raw.get(
                "vpipe_lora", "larryvrh/MiniMax-H3-Turbo-Lora-v4-600-ema"
            )
        ),
        lora_768p=str(
            raw.get(
                "vpipe_lora_768p", "lightx2v/Minimax-h3-Turbo-4step-768p"
            )
        ),
        output_subdir=output_subdir,
        low_memory_cap_mb=int(raw.get("vpipe_low_memory_cap_mb", 12288)),
        low_wired_pool_mb=int(raw.get("vpipe_low_wired_pool_mb", 8192)),
        worker_enabled=bool(raw.get("vpipe_worker_enabled", True)),
        worker_heartbeat_timeout_seconds=float(
            raw.get("vpipe_worker_heartbeat_timeout_seconds", 15.0)
        ),
        worker_cooldown_seconds=worker_cooldown_seconds,
        worker_memory_poll_seconds=worker_memory_poll_seconds,
        worker_memory_stable_samples=worker_memory_stable_samples,
        worker_min_memory_free_percent=worker_min_memory_free_percent,
        worker_min_reclaimable_mb=worker_min_reclaimable_mb,
        worker_max_wired_percent=worker_max_wired_percent,
        worker_memory_retry_limit=worker_memory_retry_limit,
    )


def build_vpipe_command(
    config: VPipeConfig,
    resource_profile: str,
    pipeline_path: Path,
) -> list[str]:
    """Build the engine command used by both direct tests and the worker."""

    command = [str(config.binary)]
    if resource_profile in {"low", "auto"}:
        taskpolicy = shutil.which("taskpolicy")
        if taskpolicy:
            command = [taskpolicy, "-b", *command]
        command.extend(
            [
                "--memory-cap-mb",
                str(config.low_memory_cap_mb),
                "--wired-pool-mb",
                str(config.low_wired_pool_mb),
            ]
        )
    command.extend(["--launch", str(pipeline_path)])
    return command


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _pipeline(config: VPipeConfig, request: VPipeRequest, output: Path) -> dict:
    use_768p = request.adapter_profile == "turbo_highres_4step"
    lora = config.lora_768p if use_768p else config.lora
    video_shift = 6.0 if use_768p else 12.0
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
                "video_shift": video_shift,
                "audio_shift": 3.0,
                "condition_timestep": 1.0,
                "audio_seconds": 0.0,
                "lora": lora,
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
    save_iports = [{"src": "rgb-to-video", "oport": 0}]
    if request.enable_h3_audio:
        save_iports.append({"src": "audio-vae-decode", "oport": 0})
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
                "iports": save_iports,
                "config": {
                    "output_url": str(output),
                    "enable_video": True,
                    "enable_audio": request.enable_h3_audio,
                },
            },
        ]
    )
    return {"id": "comfyui-h3-vpipe-shot", "stages": stages, "subpipelines": []}


def _progress_from_line(line: str) -> tuple[int, str] | None:
    """Translate stable vpipe stage messages into conservative UI progress."""

    markers = (
        ("ImageResampleStage('first-frame')", 5, "Preparing reference frame"),
        ("VaeEncodeStage('vae-encode-first')", 10, "Encoding reference frame"),
        ("DiffusionConditionerStage('diffusion-conditioner')", 15, "Encoding prompt"),
        ("MiniMax-H3 (video", 20, "Preparing H3 denoiser"),
        ("[h3-dit] first forward", 25, "Generating video frames"),
        ("emitted MiniMax-H3 latents", 75, "Video latents complete"),
        ("VaeDecodeStage('vae-decode')", 82, "Decoding video frames"),
        ("decoded clip", 88, "Decoding audio"),
        ("RGBToVideoStage", 92, "Encoding video"),
        ("SaveVideoStage", 95, "Saving MP4"),
    )
    for marker, current, label in markers:
        if marker in line:
            return current, label
    return None


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
        if not 2 <= request.steps <= 60:
            raise ValueError("Steps must be between 2 and 60.")
        if request.resource_profile not in {"low", "auto", "max"}:
            raise ValueError("Resource profile must be low, auto, or max.")
        if request.adapter_profile not in {"turbo_544p", "turbo_highres_4step"}:
            raise ValueError("Adapter profile must be turbo_544p or turbo_highres_4step.")
        if request.adapter_profile == "turbo_highres_4step":
            if request.width * request.height < 1152 * 640:
                raise ValueError("The high-resolution Turbo adapter starts at 1152x640.")
            if request.steps != 4:
                raise ValueError("The high-resolution Turbo adapter requires exactly 4 steps.")

    def _job_id(self, request: VPipeRequest) -> str:
        image_stat = request.first_frame.stat()
        payload = asdict(request)
        payload["first_frame"] = str(request.first_frame.resolve())
        payload["first_frame_size"] = image_stat.st_size
        payload["first_frame_mtime_ns"] = image_stat.st_mtime_ns
        payload["model"] = self.config.model
        payload["lora"] = self.config.lora
        payload["lora_768p"] = self.config.lora_768p
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
        pipeline_path = job_dir / "pipeline.vpipeline"
        request_path = job_dir / "request.json"
        if self.config.worker_enabled and _read_json(job_dir / "vpipe-status.json").get(
            "state"
        ) in {"queued", "running", "paused"}:
            return self._run_via_worker(
                request,
                job_id=job_id,
                job_dir=job_dir,
                output_path=output_path,
                pipeline_path=pipeline_path,
                progress=progress,
                cancelled=cancelled,
                submit=False,
            )

        partial.unlink(missing_ok=True)
        request_path.write_text(
            json.dumps(asdict(request), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        pipeline_path.write_text(
            json.dumps(_pipeline(self.config, request, partial), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        if self.config.worker_enabled:
            return self._run_via_worker(
                request,
                job_id=job_id,
                job_dir=job_dir,
                output_path=output_path,
                pipeline_path=pipeline_path,
                progress=progress,
                cancelled=cancelled,
                submit=True,
            )

        return self._run_direct(
            request,
            job_id=job_id,
            job_dir=job_dir,
            output_path=output_path,
            partial=partial,
            pipeline_path=pipeline_path,
            progress=progress,
            cancelled=cancelled,
        )

    def _run_via_worker(
        self,
        request: VPipeRequest,
        *,
        job_id: str,
        job_dir: Path,
        output_path: Path,
        pipeline_path: Path,
        progress: ProgressCallback | None,
        cancelled: CancelCallback | None,
        submit: bool,
    ) -> VPipeResult:
        project_root = (
            self.config.project_root or Path(__file__).resolve().parents[1]
        ).resolve()
        worker_root = project_root / "runtime" / "vpipe-worker"
        heartbeat_path = worker_root / "heartbeat.json"
        heartbeat = _read_json(heartbeat_path)
        try:
            heartbeat_age = time.time() - float(heartbeat.get("updated_at", 0.0))
        except (TypeError, ValueError):
            heartbeat_age = float("inf")
        if heartbeat_age > self.config.worker_heartbeat_timeout_seconds:
            raise RuntimeError(
                "The launchd vpipe worker is not ready. Run "
                "'./Service Control.command install' and retry."
            )

        status_path = job_dir / "vpipe-status.json"
        cancel_path = job_dir / "cancel.request"
        queue_root = worker_root / "queue"
        queue_root.mkdir(parents=True, exist_ok=True)
        submit_lock_path = job_dir / "submit.lock"
        started = time.monotonic()
        if submit:
            cancel_path.unlink(missing_ok=True)
            with submit_lock_path.open("a+", encoding="utf-8") as submit_lock:
                fcntl.flock(submit_lock.fileno(), fcntl.LOCK_EX)
                current = _read_json(status_path)
                if current.get("state") not in {"queued", "running", "paused"}:
                    _atomic_json(
                        status_path,
                        {
                            "schema_version": 1,
                            "job_id": job_id,
                            "state": "queued",
                            "progress": 1,
                            "message": "Queued for launchd vpipe worker",
                            "resource_profile": request.resource_profile,
                            "created_at": time.time(),
                            "updated_at": time.time(),
                        },
                    )
                    pipeline_digest = hashlib.sha256(pipeline_path.read_bytes()).hexdigest()
                    _atomic_json(
                        queue_root / f"{job_id}.json",
                        {
                            "schema_version": 1,
                            "job_id": job_id,
                            "job_dir": str(job_dir.resolve()),
                            "pipeline_sha256": pipeline_digest,
                            "resource_profile": request.resource_profile,
                            "created_at": time.time(),
                        },
                    )
        if progress:
            progress(1, 100, "Queued for launchd vpipe worker")

        reported = 1
        last_message = "Queued for launchd vpipe worker"
        missing_worker_since: float | None = None
        while True:
            current = _read_json(status_path)
            state = str(current.get("state", "queued"))
            try:
                current_progress = int(current.get("progress", 1))
            except (TypeError, ValueError):
                current_progress = 1
            message = str(current.get("message", state))
            if progress and (current_progress > reported or message != last_message):
                reported = max(reported, current_progress)
                last_message = message
                progress(min(100, current_progress), 100, message)
            if state == "completed" and output_path.is_file() and output_path.stat().st_size > 0:
                elapsed = float(current.get("elapsed_seconds", time.monotonic() - started))
                if progress and reported < 100:
                    progress(100, 100, "vpipe complete")
                return VPipeResult(job_id, output_path, job_dir, elapsed, False)
            if state in {"failed", "cancelled"}:
                error = str(current.get("error", "vpipe worker did not complete the job"))
                exception = InterruptedError if state == "cancelled" else RuntimeError
                raise exception(error)
            if cancelled and cancelled():
                cancel_path.touch(exist_ok=True)

            heartbeat = _read_json(heartbeat_path)
            try:
                age = time.time() - float(heartbeat.get("updated_at", 0.0))
            except (TypeError, ValueError):
                age = float("inf")
            if age > self.config.worker_heartbeat_timeout_seconds:
                if missing_worker_since is None:
                    missing_worker_since = time.monotonic()
                elif time.monotonic() - missing_worker_since > 60.0:
                    raise RuntimeError(
                        "The launchd vpipe worker has been unavailable for over 60 seconds. "
                        f"Job state remains in {job_dir}."
                    )
            else:
                missing_worker_since = None
            time.sleep(0.25)

    def _run_direct(
        self,
        request: VPipeRequest,
        *,
        job_id: str,
        job_dir: Path,
        output_path: Path,
        partial: Path,
        pipeline_path: Path,
        progress: ProgressCallback | None,
        cancelled: CancelCallback | None,
    ) -> VPipeResult:
        log_path = job_dir / "engine.log"
        command = build_vpipe_command(
            self.config, request.resource_profile, pipeline_path
        )
        if progress:
            progress(1, 100, "Starting vpipe")
        started = time.monotonic()
        with log_path.open("w", encoding="utf-8") as log:
            log.write("COMMAND " + json.dumps(command, ensure_ascii=False) + "\n")
            log.flush()
            process = subprocess.Popen(
                command,
                cwd=self.config.work_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
            output_lines: queue.SimpleQueue[str] = queue.SimpleQueue()
            output_finished = threading.Event()

            def collect_output() -> None:
                assert process.stdout is not None
                try:
                    for line in process.stdout:
                        output_lines.put(line)
                finally:
                    process.stdout.close()
                    output_finished.set()

            reader = threading.Thread(target=collect_output, daemon=True)
            reader.start()
            reported_progress = 1

            def drain_output() -> None:
                nonlocal reported_progress
                while True:
                    try:
                        line = output_lines.get_nowait()
                    except queue.Empty:
                        break
                    log.write(line)
                    update = _progress_from_line(line)
                    if progress and update and update[0] > reported_progress:
                        reported_progress = update[0]
                        progress(update[0], 100, update[1])
                log.flush()

            while process.poll() is None:
                drain_output()
                if cancelled and cancelled():
                    process.terminate()
                    process.wait(timeout=10)
                    reader.join(timeout=2)
                    drain_output()
                    raise RuntimeError("vpipe generation cancelled.")
                time.sleep(0.25)
            reader.join(timeout=2)
            drain_output()
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
