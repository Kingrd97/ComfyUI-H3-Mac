from __future__ import annotations

import codecs
import fcntl
import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterator, TextIO

from .config import BridgeConfig
from .job_registry import finish_job, mark_cleanup_needed, register_starting_job
from .locking import publication_control_guard
from .models import H3Request, H3Result
from .profiles import QUALITY_PROFILES, process_prefix, should_stream
from .scheduler import AdaptiveScheduler, process_group_alive, process_start_signature


ProgressCallback = Callable[[int, int, str], None]
CancelCallback = Callable[[], bool]
_PROGRESS_RE = re.compile(r"(?:^|\s)(\d+)\s*/\s*(\d+)(?:\s|$)")
_H3_CANVAS_MULTIPLE = 32
_H3_MAX_PIXELS = 768 * 1344
_H3_MIN_FRAMES = 22
_H3_MAX_FRAMES = 362
_H3_FPS = 24
_SAFE_48_GIB_ALIGNED_FRAMES = 124
_LARGE_JOB_ENV = "H3_ALLOW_LARGE_JOB"
_VERIFIED_MANIFESTS: set[tuple[str, int, int, str]] = set()
_MANIFEST_CACHE_LOCK = threading.Lock()
_GENERATION_LOCK_SCHEMA_VERSION = 1
_PROCESS_SIGNATURE_ATTEMPTS = 5
_PROCESS_SIGNATURE_RETRY_SECONDS = 0.05


def _is_apple_silicon() -> bool:
    """Keep the platform probe narrow so tests never patch global ``os.uname``."""

    identity = os.uname()
    return identity.sysname == "Darwin" and identity.machine == "arm64"


def _write_generation_lock_metadata(
    lock_fd: int,
    *,
    controller_pid: int,
    controller_start_signature: str,
    registration_token: str = "",
    job_id: str = "",
) -> None:
    """Replace lock-owner metadata while retaining the inherited flock."""

    payload = {
        "schema_version": _GENERATION_LOCK_SCHEMA_VERSION,
        "controller_pid": controller_pid,
        "controller_start_signature": controller_start_signature,
        "registration_token": registration_token,
        "job_id": job_id,
    }
    encoded = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
    os.lseek(lock_fd, 0, os.SEEK_SET)
    os.ftruncate(lock_fd, 0)
    view = memoryview(encoded)
    while view:
        written = os.write(lock_fd, view)
        if written <= 0:
            raise OSError("Could not publish H3 generation-lock identity")
        view = view[written:]
    os.fsync(lock_fd)


@dataclass(frozen=True)
class _InferenceSemantics:
    """Resolved output-affecting CLI values used by both cache and launch."""

    schema_version: int
    steps: int
    layers: int
    reuse: int
    core_reuse: int
    ssd_streaming: bool
    model_revision: str


class _OutputFramer:
    """Split streaming CLI output on CR, LF, or CRLF without blocking."""

    def __init__(self) -> None:
        self._current: list[str] = []
        self._previous_was_cr = False

    def feed(self, text: str) -> list[str]:
        records: list[str] = []
        for character in text:
            if character == "\r":
                records.append("".join(self._current))
                self._current.clear()
                self._previous_was_cr = True
            elif character == "\n":
                if not self._previous_was_cr:
                    records.append("".join(self._current))
                    self._current.clear()
                self._previous_was_cr = False
            else:
                self._previous_was_cr = False
                self._current.append(character)
        return records

    def finish(self) -> list[str]:
        if not self._current:
            return []
        record = "".join(self._current)
        self._current.clear()
        return [record]


def _h3_aligned_frames(requested: int) -> int:
    """Mirror pinned h3.c h3_align_frame_count()."""

    value = max(5, requested)
    remainder = (value - 5) % 17
    return value if remainder == 0 else value + 17 - remainder


def _original_process_group_state(pgid: int, expected_birth: str) -> str:
    """Classify a launched process group without collapsing ambiguity into exit."""

    if pgid <= 1:
        return "gone"
    if not expected_birth:
        return "ambiguous"
    if not process_group_alive(pgid):
        return "gone"
    current_birth = _stable_process_start_signature(pgid, attempts=3)
    if current_birth:
        return "exact" if current_birth == expected_birth else "reused"
    try:
        os.kill(pgid, 0)
    except ProcessLookupError:
        # The original leader is gone. A still-existing PGID can only be its
        # remaining descendants; a PGID cannot be reused while that group lives.
        return "leaderless" if process_group_alive(pgid) else "gone"
    except PermissionError:
        pass
    # A leader still exists but ps could not fingerprint it. This is ambiguous,
    # so fail closed rather than signalling a potentially reused PID.
    return "ambiguous"


def _original_process_group_alive(pgid: int, expected_birth: str) -> bool:
    return _original_process_group_state(pgid, expected_birth) in {
        "exact",
        "leaderless",
    }


def _stable_process_start_signature(
    pid: int,
    attempts: int = _PROCESS_SIGNATURE_ATTEMPTS,
) -> str:
    """Require a non-empty PID birth fingerprint before opening the gate."""

    for attempt in range(max(1, attempts)):
        signature = process_start_signature(pid)
        if signature:
            return signature
        if attempt + 1 < attempts:
            time.sleep(_PROCESS_SIGNATURE_RETRY_SECONDS)
    return ""


def _abort_gated_launcher(process: subprocess.Popen[bytes], gate_fd: int) -> None:
    """Bound cleanup of a known direct child that has not crossed its gate."""

    if gate_fd >= 0:
        os.close(gate_fd)
    try:
        process.wait(timeout=2)
        return
    except subprocess.TimeoutExpired:
        process.terminate()
    try:
        process.wait(timeout=2)
        return
    except subprocess.TimeoutExpired:
        process.kill()
    process.wait(timeout=2)


def _wait_for_original_group_exit(
    pgid: int,
    expected_birth: str,
    timeout: float,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _original_process_group_state(pgid, expected_birth) in {"gone", "reused"}:
            return True
        time.sleep(0.05)
    return _original_process_group_state(pgid, expected_birth) in {"gone", "reused"}


def _terminate_process_group(
    process: subprocess.Popen[bytes],
    expected_birth: str,
) -> bool:
    """Bound termination and confirm no original group member still holds FDs."""

    pgid = process.pid
    initial_state = _original_process_group_state(pgid, expected_birth)
    if initial_state in {"gone", "reused"}:
        return True
    if initial_state == "ambiguous":
        return False
    try:
        os.killpg(pgid, signal.SIGCONT)
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    if _wait_for_original_group_exit(pgid, expected_birth, 0.5):
        return True
    pre_kill_state = _original_process_group_state(pgid, expected_birth)
    if pre_kill_state in {"gone", "reused"}:
        return True
    if pre_kill_state == "ambiguous":
        return False
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    return _wait_for_original_group_exit(pgid, expected_birth, 2.0)


def _physical_memory_gib() -> float | None:
    """Return physical RAM without importing platform-specific packages."""

    try:
        completed = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "hw.memsize"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        value = int(completed.stdout.strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return value / (1024**3) if completed.returncode == 0 and value > 0 else None


def _verify_model_manifest(
    model_root: Path,
    required_task: str,
    expected_revision: str,
    allow_unmanaged_model: bool,
) -> None:
    """Validate a managed snapshot once per manifest revision in this server."""

    manifest_path = model_root.parent / f"{model_root.name}.manifest.json"
    if not manifest_path.is_file():
        if allow_unmanaged_model:
            return
        raise RuntimeError(
            f"Pinned model manifest is missing: {manifest_path}. "
            "Rerun Download Model.command before generation."
        )
    try:
        manifest_stat = manifest_path.stat()
        root_target = str(model_root.resolve(strict=True))
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
        revision = str(value["revision"])
        raw_files = value["files"]
        raw_tasks = value["installed_tasks"]
        if value.get("schema_version") != 1:
            raise ValueError("unsupported schema")
        if value.get("repo_id") != "MiniMaxAI/MiniMax-H3":
            raise ValueError("wrong repository")
        if expected_revision and revision != expected_revision:
            raise RuntimeError(
                f"Model revision mismatch: expected {expected_revision}, got {revision}. "
                "Rerun Download Model.command."
            )
        if not isinstance(raw_files, list) or not raw_files:
            raise ValueError("empty file list")
        if (
            not isinstance(raw_tasks, list)
            or "FL2VA" not in raw_tasks
            or len(set(str(item) for item in raw_tasks)) != len(raw_tasks)
            or any(str(item) not in {"FL2VA", "Ref2VA"} for item in raw_tasks)
        ):
            raise ValueError("invalid task list")
        if required_task not in raw_tasks:
            raise RuntimeError(
                f"Model task {required_task} is not completely installed. "
                "Rerun Download Model.command."
            )
    except (FileNotFoundError, KeyError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Model manifest is invalid: {manifest_path}. Rerun Download Model.command."
        ) from exc
    cache_key = (
        str(manifest_path),
        manifest_stat.st_mtime_ns,
        manifest_stat.st_size,
        root_target,
    )
    with _MANIFEST_CACHE_LOCK:
        if cache_key in _VERIFIED_MANIFESTS:
            return

    content_addressed = value.get("storage") == "huggingface-content-addressed-cache"
    seen_paths: set[str] = set()
    seen_prefixes: set[str] = set()
    for raw in raw_files:
        try:
            if not isinstance(raw, dict):
                raise TypeError
            relative = str(raw["path"])
            parts = relative.split("/")
            if (
                relative.startswith("/")
                or "\\" in relative
                or any(part in {"", ".", ".."} for part in parts)
                or relative in seen_paths
            ):
                raise ValueError("unsafe or duplicate path")
            seen_paths.add(relative)
            if len(parts) > 1:
                seen_prefixes.add(parts[0])
                if parts[0] not in raw_tasks:
                    raise ValueError("path outside installed tasks")
            elif relative not in {"model_index.json", "modular_model_index.json"}:
                raise ValueError("unexpected root file")
            expected_size = int(raw["size"])
            if expected_size < 0:
                raise ValueError("negative size")
            blob_key = str(raw.get("blob_key", ""))
            candidate = model_root / relative
            actual_size = candidate.stat().st_size
        except (KeyError, OSError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Model snapshot {revision} is incomplete at {raw!r}. "
                "Rerun Download Model.command, then Doctor.command."
            ) from exc
        if not candidate.is_file() or actual_size != expected_size:
            raise RuntimeError(
                f"Model snapshot {revision} has a missing or truncated file: {relative}. "
                "Rerun Download Model.command, then Doctor.command."
            )
        if content_addressed and (not blob_key or not candidate.is_symlink()):
            raise RuntimeError(
                f"Model file is not a content-addressed blob link: {relative}. "
                "Rerun Download Model.command."
            )
        if content_addressed:
            try:
                actual_blob = candidate.resolve(strict=True).name
            except OSError as exc:
                raise RuntimeError(f"Model blob link is broken: {relative}") from exc
            if actual_blob != blob_key:
                raise RuntimeError(
                    f"Model blob identity mismatch for {relative}. Rerun Download Model.command."
                )
    if "FL2VA" not in seen_prefixes or ("Ref2VA" in raw_tasks) != ("Ref2VA" in seen_prefixes):
        raise RuntimeError(
            "Model manifest task list does not match its file tree. "
            "Rerun Download Model.command."
        )
    with _MANIFEST_CACHE_LOCK:
        _VERIFIED_MANIFESTS.add(cache_key)


class H3Runner:
    def __init__(self, config: BridgeConfig):
        self.config = config

    def _resolved_h3_binary(self) -> Path:
        try:
            binary = self.config.h3_binary.resolve(strict=True)
        except OSError as exc:
            raise FileNotFoundError(
                f"h3 executable not found: {self.config.h3_binary}. "
                "Run Install.command first."
            ) from exc
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise FileNotFoundError(
                f"h3 executable is not runnable: {binary}. Run Install.command first."
            )
        shader = binary.parent / "h3_shaders.metal"
        if not shader.is_file():
            raise FileNotFoundError(
                f"h3 Metal shader source not found beside the engine: {shader}. "
                "Run Install.command first."
            )
        return binary

    def validate(self, request: H3Request) -> None:
        if not _is_apple_silicon():
            raise RuntimeError("h3.c requires an Apple Silicon Mac (arm64).")
        self._resolved_h3_binary()
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
        _verify_model_manifest(
            model_dir,
            request.task,
            self.config.expected_model_revision,
            self.config.allow_unmanaged_model,
        )
        if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
            raise FileNotFoundError("FFmpeg/ffprobe not found. Run Install.command first.")
        if not request.prompt.strip():
            raise ValueError("Prompt must not be empty.")
        if (
            request.width < _H3_CANVAS_MULTIPLE
            or request.height < _H3_CANVAS_MULTIPLE
            or request.width % _H3_CANVAS_MULTIPLE
            or request.height % _H3_CANVAS_MULTIPLE
        ):
            raise ValueError("Width and height must be multiples of 32 and at least 32.")
        if request.width * request.height > _H3_MAX_PIXELS:
            raise ValueError(
                "Canvas area must not exceed 768 × 1344 pixels "
                f"({_H3_MAX_PIXELS:,}); got {request.width} × {request.height}."
            )
        if request.seconds <= 0:
            raise ValueError("Duration must be positive.")
        if request.fps != _H3_FPS:
            raise ValueError("H3 generation uses a fixed 24 fps timeline.")
        aligned_frames = _h3_aligned_frames(request.frames)
        if request.frames < _H3_MIN_FRAMES or aligned_frames > _H3_MAX_FRAMES:
            raise ValueError(
                "H3 requires 22..362 requested frames whose aligned frame count stays "
                f"within 22..362; got {request.frames} requested / {aligned_frames} aligned."
            )
        large_job_allowed = os.environ.get(_LARGE_JOB_ENV) == "1"
        if aligned_frames > _SAFE_48_GIB_ALIGNED_FRAMES and not large_job_allowed:
            memory_gib = _physical_memory_gib()
        else:
            memory_gib = None
        if aligned_frames > _SAFE_48_GIB_ALIGNED_FRAMES and not large_job_allowed and (
            memory_gib is None or memory_gib < 64.0
        ):
            memory_label = "unknown" if memory_gib is None else f"{memory_gib:.0f} GiB"
            raise ValueError(
                "Shots longer than 5 seconds are disabled on Macs with less than 64 GiB RAM "
                f"(detected {memory_label}) because H3 can exhaust swap during VAE decode. "
                "Use 2–6 short shots with H3 Assemble Storyboard. Advanced users may set "
                f"{_LARGE_JOB_ENV}=1 after verifying memory pressure and free disk space."
            )
        if request.references and request.task != "Ref2VA":
            raise ValueError("Ordered references require the Ref2VA task.")
        if request.references and (request.first_frame or request.last_frame):
            raise ValueError("Ref2VA references cannot be combined with first/last-frame anchors.")

    def _inference_semantics(self, request: H3Request) -> _InferenceSemantics:
        quality = QUALITY_PROFILES[request.quality_profile]
        return _InferenceSemantics(
            # Bump this whenever the bridge changes an output-affecting h3 CLI
            # convention that is not already represented by H3Request.
            schema_version=1,
            steps=quality.steps,
            layers=quality.layers,
            reuse=quality.reuse,
            core_reuse=quality.core_reuse,
            ssd_streaming=should_stream(
                request.resource_profile,
                self.config.auto_ssd_streaming_ram_gib,
            ),
            model_revision=self.config.expected_model_revision,
        )

    def _request_digest(
        self,
        request: H3Request,
        semantics: _InferenceSemantics | None = None,
    ) -> str:
        payload = request.to_json()
        resolved = semantics or self._inference_semantics(request)
        payload["inference_semantics"] = asdict(resolved)
        inputs: list[dict[str, int | str]] = []
        paths = [ref.path for ref in request.references]
        paths += [ref.audio_path for ref in request.references if ref.audio_path]
        paths += [item for item in (request.first_frame, request.last_frame) if item]
        for path in paths:
            assert path is not None
            stat = path.stat()
            inputs.append({"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
        payload["input_files"] = inputs
        binary = self._resolved_h3_binary()
        engine_stat = binary.stat()
        payload["engine"] = {
            "path": str(binary),
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
        model_manifest = (
            self.config.model_root.parent
            / f"{self.config.model_root.name}.manifest.json"
        )
        if model_manifest.is_file():
            manifest_stat = model_manifest.stat()
            payload["model_manifest"] = {
                "size": manifest_stat.st_size,
                "mtime_ns": manifest_stat.st_mtime_ns,
            }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:20]

    def build_command(
        self,
        request: H3Request,
        output_path: Path,
        semantics: _InferenceSemantics | None = None,
    ) -> list[str]:
        resolved = semantics or self._inference_semantics(request)
        command = process_prefix(request.resource_profile)
        binary = self._resolved_h3_binary()
        command += [
            "/usr/bin/caffeinate",
            "-s",
            str(binary),
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
            str(resolved.steps),
            "--layers",
            str(resolved.layers),
            "--reuse",
            str(resolved.reuse),
            "--core-reuse",
            str(resolved.core_reuse),
            "--seed",
            str(request.seed),
        ]
        if resolved.ssd_streaming:
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

    @contextmanager
    def _generation_lock(self) -> Iterator[TextIO]:
        """Allow one H3 engine per installation, including across Comfy servers."""

        lock_path = self.config.project_root / "runtime" / "h3-generation.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as lock:
            with publication_control_guard(self.config.project_root):
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise RuntimeError(
                        "Another H3 generation is already running in this installation. "
                        "Wait for it to finish or cancel it before starting another shot."
                    ) from exc
                controller_pid = os.getpid()
                controller_signature = _stable_process_start_signature(controller_pid)
                if not controller_signature:
                    raise RuntimeError(
                        "Could not obtain a stable H3 controller process identity."
                    )
                _write_generation_lock_metadata(
                    lock.fileno(),
                    controller_pid=controller_pid,
                    controller_start_signature=controller_signature,
                )
            # Do not issue LOCK_UN here. The launcher inherits this exact
            # open-file-description, so explicitly unlocking the parent
            # descriptor would also release the child's safety lock. The
            # surrounding close releases it only after the final inherited
            # descriptor exits.
            yield lock

    def run(
        self,
        request: H3Request,
        output_root: Path,
        progress: ProgressCallback | None = None,
        cancelled: CancelCallback | None = None,
        reuse_completed: bool = True,
    ) -> H3Result:
        self.validate(request)
        output_root = output_root.expanduser().resolve()
        semantics = self._inference_semantics(request)
        job_id = self._request_digest(request, semantics)
        job_dir = output_root / self.config.output_subdir / job_id
        result_path = job_dir / "result.mp4"
        if reuse_completed and result_path.is_file() and result_path.stat().st_size > 0:
            return H3Result(job_id, result_path, job_dir, 0.0, tuple())
        with self._generation_lock() as generation_lock:
            return self._run_locked(
                request,
                output_root,
                job_id=job_id,
                semantics=semantics,
                generation_lock_fd=generation_lock.fileno(),
                progress=progress,
                cancelled=cancelled,
                reuse_completed=reuse_completed,
            )

    def _run_locked(
        self,
        request: H3Request,
        output_root: Path,
        job_id: str,
        semantics: _InferenceSemantics,
        generation_lock_fd: int,
        progress: ProgressCallback | None = None,
        cancelled: CancelCallback | None = None,
        reuse_completed: bool = True,
    ) -> H3Result:
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
        command = self.build_command(request, partial_path, semantics)
        expected_steps = semantics.steps
        started = time.monotonic()
        process: subprocess.Popen[bytes] | None = None
        scheduler: AdaptiveScheduler | None = None
        engine_signature = ""
        registry_cleanup_safe = True
        controller_pid = os.getpid()
        controller_signature = _stable_process_start_signature(controller_pid)
        if not controller_signature:
            raise RuntimeError(
                "Could not obtain a stable H3 controller process identity."
            )
        registration = register_starting_job(
            self.config.project_root,
            job_dir,
            job_id,
            self.config.output_subdir,
            controller_pid=controller_pid,
            controller_start_signature=controller_signature,
            engine_profile=request.resource_profile,
        )
        with publication_control_guard(self.config.project_root):
            _write_generation_lock_metadata(
                generation_lock_fd,
                controller_pid=controller_pid,
                controller_start_signature=controller_signature,
                registration_token=registration.token,
                job_id=job_id,
            )
        terminal_state = "failed"
        launcher = Path(__file__).with_name("h3_launch.py").resolve(strict=True)
        launch_command = [
            sys.executable,
            str(launcher),
            "--project-root",
            str(self.config.project_root.resolve()),
            "--registry",
            str(registration.entry_path),
            "--token",
            registration.token,
            "--gate-fd",
            "{gate_fd}",
            "--ack-fd",
            "{ack_fd}",
            "--",
            *command,
        ]
        gate_read_fd = -1
        gate_write_fd = -1
        ack_read_fd = -1
        ack_write_fd = -1

        try:
            with log_path.open("w", encoding="utf-8", buffering=1) as log:
                log.write("COMMAND " + json.dumps(command, ensure_ascii=False) + "\n")
                gate_read_fd, gate_write_fd = os.pipe()
                ack_read_fd, ack_write_fd = os.pipe()
                child_command = [
                    str(gate_read_fd)
                    if item == "{gate_fd}"
                    else str(ack_write_fd)
                    if item == "{ack_fd}"
                    else item
                    for item in launch_command
                ]
                try:
                    process = subprocess.Popen(
                        child_command,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        bufsize=0,
                        start_new_session=True,
                        cwd=self._resolved_h3_binary().parent,
                        pass_fds=(
                            generation_lock_fd,
                            gate_read_fd,
                            ack_write_fd,
                        ),
                    )
                finally:
                    os.close(gate_read_fd)
                    gate_read_fd = -1
                    os.close(ack_write_fd)
                    ack_write_fd = -1
                assert process.stdout is not None
                registry_cleanup_safe = False
                engine_signature = _stable_process_start_signature(process.pid)
                if not engine_signature:
                    gated_fd = gate_write_fd
                    gate_write_fd = -1
                    _abort_gated_launcher(process, gated_fd)
                    registry_cleanup_safe = True
                    raise RuntimeError(
                        "Could not obtain a stable H3 launcher process identity."
                    )
                scheduler = AdaptiveScheduler(
                    job_dir,
                    process.pid,
                    request.resource_profile,
                    self.config,
                    controller_pid=controller_pid,
                    controller_start_signature=controller_signature,
                )
                os.write(gate_write_fd, b"G")
                os.close(gate_write_fd)
                gate_write_fd = -1
                os.set_blocking(ack_read_fd, False)
                with selectors.DefaultSelector() as gate_selector:
                    gate_selector.register(ack_read_fd, selectors.EVENT_READ)
                    activation_deadline = time.monotonic() + 5.0
                    activated = False
                    while time.monotonic() < activation_deadline:
                        if process.poll() is not None:
                            break
                        if gate_selector.select(timeout=0.05):
                            acknowledgement = os.read(ack_read_fd, 1)
                            activated = acknowledgement == b"A"
                            break
                os.close(ack_read_fd)
                ack_read_fd = -1
                if not activated:
                    raise RuntimeError(
                        "H3 launcher exited or timed out before publishing its identity."
                    )
                # The child has atomically activated its two-sided registry.
                # Only now may adaptive scheduling send STOP/CONT signals.
                scheduler.start()
                output_fd = process.stdout.fileno()
                os.set_blocking(output_fd, False)
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                framer = _OutputFramer()

                def handle_records(records: list[str]) -> None:
                    for line in records:
                        log.write(line + "\n")
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

                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while process.poll() is None:
                        scheduler.tick()
                        if cancelled and cancelled():
                            scheduler.prepare_termination()
                            raise InterruptedError(
                                "H3 generation cancelled; partial output and logs were kept."
                            )
                        for _key, _events in selector.select(timeout=0.25):
                            # Bound each live drain so continuous verbose output
                            # cannot starve scheduler.tick() or cancellation.
                            for _ in range(4):
                                try:
                                    chunk = os.read(output_fd, 65_536)
                                except BlockingIOError:
                                    break
                                if not chunk:
                                    break
                                handle_records(framer.feed(decoder.decode(chunk)))
                                if len(chunk) < 65_536:
                                    break

                    # The producer exited, so draining the finite tail cannot
                    # delay resource scheduling any further.
                    while True:
                        try:
                            chunk = os.read(output_fd, 65_536)
                        except BlockingIOError:
                            break
                        if not chunk:
                            break
                        handle_records(framer.feed(decoder.decode(chunk)))
                    handle_records(framer.feed(decoder.decode(b"", final=True)))
                    handle_records(framer.finish())
                return_code = process.wait()
                registry_cleanup_safe = _original_process_group_state(
                    process.pid, engine_signature
                ) in {"gone", "reused"}
                if return_code != 0:
                    raise RuntimeError(f"h3.c exited with status {return_code}. See {log_path}")
                if not partial_path.is_file() or partial_path.stat().st_size == 0:
                    raise RuntimeError(f"h3.c finished without a video. See {log_path}")
                partial_path.replace(result_path)
                scheduler.finish("completed")
                terminal_state = "completed"
        except BaseException as exc:
            if scheduler:
                scheduler.prepare_termination()
            if process:
                registry_cleanup_safe = _terminate_process_group(
                    process, engine_signature
                )
            terminal_state = "cancelled" if isinstance(exc, InterruptedError) else "failed"
            if scheduler:
                scheduler.finish(terminal_state, str(exc))
            if not self.config.keep_failed_output:
                partial_path.unlink(missing_ok=True)
            raise
        finally:
            if gate_read_fd >= 0:
                os.close(gate_read_fd)
            if gate_write_fd >= 0:
                os.close(gate_write_fd)
            if ack_read_fd >= 0:
                os.close(ack_read_fd)
            if ack_write_fd >= 0:
                os.close(ack_write_fd)
            if registry_cleanup_safe:
                finish_job(registration, terminal_state)
            else:
                mark_cleanup_needed(
                    registration,
                    f"{terminal_state}: process group exit unverified",
                )

        elapsed = time.monotonic() - started
        return H3Result(job_id, result_path, job_dir, elapsed, tuple(command))
