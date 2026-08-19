from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable

from .config import BridgeConfig
from .models import H3Request, H3Result
from .profiles import QUALITY_PROFILES, process_prefix, should_stream


ProgressCallback = Callable[[int, int, str], None]
CancelCallback = Callable[[], bool]
_PROGRESS_RE = re.compile(r"(?:^|\s)(\d+)\s*/\s*(\d+)(?:\s|$)")


class H3Runner:
    def __init__(self, config: BridgeConfig):
        self.config = config

    def validate(self, request: H3Request) -> None:
        if os.uname().sysname != "Darwin" or os.uname().machine != "arm64":
            raise RuntimeError("h3.c requires an Apple Silicon Mac (arm64).")
        if not self.config.h3_binary.is_file() or not os.access(self.config.h3_binary, os.X_OK):
            raise FileNotFoundError(
                f"h3 executable not found: {self.config.h3_binary}. Run Install.command first."
            )
        model_dir = self.config.model_dir(request.task)
        fl2va_dir = model_dir / "FL2VA"
        if not fl2va_dir.is_dir():
            raise FileNotFoundError(
                f"FL2VA base model not found: {fl2va_dir}. Run Download Model.command first."
            )
        if request.task == "Ref2VA" and not (model_dir / "Ref2VA").is_dir():
            raise FileNotFoundError(
                f"Ref2VA model not found: {model_dir / 'Ref2VA'}. Download the Ref2VA bundle."
            )
        if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
            raise FileNotFoundError("FFmpeg/ffprobe not found. Run Install.command first.")
        if not request.prompt.strip():
            raise ValueError("Prompt must not be empty.")
        if request.width % 16 or request.height % 16:
            raise ValueError("Width and height must be multiples of 16.")
        if request.seconds <= 0 or request.fps <= 0:
            raise ValueError("Duration and FPS must be positive.")
        if request.references and request.task != "Ref2VA":
            raise ValueError("Ordered references require the Ref2VA task.")
        if request.references and (request.first_frame or request.last_frame):
            raise ValueError("Ref2VA references cannot be combined with first/last-frame anchors.")

    def _request_digest(self, request: H3Request) -> str:
        payload = request.to_json()
        inputs: list[dict[str, int | str]] = []
        paths = [ref.path for ref in request.references]
        paths += [ref.audio_path for ref in request.references if ref.audio_path]
        paths += [item for item in (request.first_frame, request.last_frame) if item]
        for path in paths:
            assert path is not None
            stat = path.stat()
            inputs.append({"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
        payload["input_files"] = inputs
        engine_stat = self.config.h3_binary.stat()
        payload["engine"] = {
            "path": str(self.config.h3_binary),
            "size": engine_stat.st_size,
            "mtime_ns": engine_stat.st_mtime_ns,
        }
        model_index = self.config.model_root / "model_index.json"
        if model_index.is_file():
            index_stat = model_index.stat()
            payload["model_index"] = {
                "size": index_stat.st_size,
                "mtime_ns": index_stat.st_mtime_ns,
            }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:20]

    def build_command(self, request: H3Request, output_path: Path) -> list[str]:
        quality = QUALITY_PROFILES[request.quality_profile]
        command = process_prefix(request.resource_profile)
        command += [
            "/usr/bin/caffeinate",
            "-i",
            str(self.config.h3_binary),
            "-d",
            str(self.config.model_dir(request.task)),
            "-p",
            request.prompt,
            "-o",
            str(output_path),
            "--width",
            str(request.width),
            "--height",
            str(request.height),
            "--frames",
            str(request.frames),
            "--steps",
            str(quality.steps),
            "--layers",
            str(quality.layers),
            "--reuse",
            str(quality.reuse),
            "--core-reuse",
            str(quality.core_reuse),
            "--seed",
            str(request.seed),
        ]
        if should_stream(request.resource_profile, self.config.auto_ssd_streaming_ram_gib):
            command.append("--ssd-streaming")
        if request.first_frame:
            command += ["--first-frame", str(request.first_frame)]
        if request.last_frame:
            command += ["--last-frame", str(request.last_frame)]
        for ref in request.references:
            option = {
                "image": "--ref-image",
                "silent_video": "--ref-silent-video",
                "video": "--ref-video",
                "audio": "--ref-audio",
            }.get(ref.kind)
            if ref.kind == "video_audio":
                if ref.audio_path is None:
                    raise ValueError("video_audio reference requires an audio_path")
                command += ["--ref-video-audio", str(ref.path), str(ref.audio_path)]
            elif option:
                command += [option, str(ref.path)]
            else:
                raise ValueError(f"Unsupported reference kind: {ref.kind}")
        return command

    def run(
        self,
        request: H3Request,
        output_root: Path,
        progress: ProgressCallback | None = None,
        cancelled: CancelCallback | None = None,
        reuse_completed: bool = True,
    ) -> H3Result:
        self.validate(request)
        job_id = self._request_digest(request)
        job_dir = output_root / self.config.output_subdir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        result_path = job_dir / "result.mp4"
        partial_path = job_dir / "result.partial.mp4"
        request_path = job_dir / "request.json"
        progress_path = job_dir / "progress.json"
        log_path = job_dir / "engine.log"

        if reuse_completed and result_path.is_file() and result_path.stat().st_size > 0:
            return H3Result(job_id, result_path, job_dir, 0.0, tuple())

        request_path.write_text(
            json.dumps(request.to_json(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        command = self.build_command(request, partial_path)
        expected_steps = QUALITY_PROFILES[request.quality_profile].steps
        started = time.monotonic()
        process: subprocess.Popen[str] | None = None

        with log_path.open("w", encoding="utf-8", buffering=1) as log:
            log.write("COMMAND " + json.dumps(command, ensure_ascii=False) + "\n")
            try:
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    start_new_session=True,
                )
                assert process.stdout is not None
                selector = selectors.DefaultSelector()
                selector.register(process.stdout, selectors.EVENT_READ)
                while process.poll() is None:
                    if cancelled and cancelled():
                        os.killpg(process.pid, signal.SIGTERM)
                        raise InterruptedError("H3 generation cancelled; partial output and logs were kept.")
                    for _key, _events in selector.select(timeout=0.5):
                        line = process.stdout.readline()
                        if not line:
                            continue
                        log.write(line)
                        match = _PROGRESS_RE.search(line)
                        if match:
                            current, total = int(match.group(1)), int(match.group(2))
                            if total == expected_steps:
                                progress_path.write_text(
                                    json.dumps({"current": current, "total": total, "line": line.strip()}),
                                    encoding="utf-8",
                                )
                                if progress:
                                    progress(current, total, line.strip())
                for line in process.stdout:
                    log.write(line)
                selector.close()
                return_code = process.wait()
                if return_code != 0:
                    raise RuntimeError(f"h3.c exited with status {return_code}. See {log_path}")
                if not partial_path.is_file() or partial_path.stat().st_size == 0:
                    raise RuntimeError(f"h3.c finished without a video. See {log_path}")
                partial_path.replace(result_path)
            except BaseException:
                if process and process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                if not self.config.keep_failed_output:
                    partial_path.unlink(missing_ok=True)
                raise

        elapsed = time.monotonic() - started
        return H3Result(job_id, result_path, job_dir, elapsed, tuple(command))
