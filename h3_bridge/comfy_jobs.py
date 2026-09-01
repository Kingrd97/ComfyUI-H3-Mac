from __future__ import annotations

import fcntl
import functools
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from .vpipe import _valid_video_file, load_vpipe_config


_JOB_ID = re.compile(r"vpipe-[0-9a-f]{20}")
_ACTIVE_STATES = {"queued", "launching", "running", "paused"}
_ROUTES_REGISTERED = False


@functools.lru_cache(maxsize=512)
def _cached_video_validation(path: str, size: int, mtime_ns: int) -> bool:
    del size, mtime_ns
    return _valid_video_file(Path(path))


def _valid_result(path: Path) -> bool:
    try:
        stat = path.stat()
    except OSError:
        return False
    return _cached_video_validation(str(path), stat.st_size, stat.st_mtime_ns)


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_json(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


class VPipeJobManager:
    def __init__(self, project_root: Path, output_root: Path):
        self.project_root = project_root.resolve()
        self.config = load_vpipe_config(self.project_root)
        self.output_root = output_root.resolve()
        self.job_root = self.output_root / self.config.output_subdir
        self.worker_root = self.project_root / "runtime" / "vpipe-worker"
        self.queue_root = self.worker_root / "queue"

    def _job_dir(self, job_id: str, *, must_exist: bool = True) -> Path:
        if not _JOB_ID.fullmatch(job_id):
            raise ValueError("Invalid vpipe job ID")
        candidate = self.job_root / job_id
        if candidate.is_symlink():
            raise ValueError("A vpipe job directory cannot be a symlink")
        resolved = candidate.resolve(strict=must_exist)
        if resolved.parent != self.job_root.resolve() or resolved.name != job_id:
            raise ValueError("vpipe job is outside the ComfyUI output directory")
        return resolved

    def _queue_positions(self) -> dict[str, int]:
        try:
            tickets = sorted(
                self.queue_root.glob("vpipe-*.json"),
                key=lambda path: path.stat().st_mtime,
            )
        except OSError:
            return {}
        return {path.stem: index + 1 for index, path in enumerate(tickets)}

    def _job(self, job_dir: Path, positions: dict[str, int]) -> dict[str, object]:
        request = _read_json(job_dir / "request.json")
        status = _read_json(job_dir / "vpipe-status.json")
        control = _read_json(job_dir / "control.json")
        result = job_dir / "result.mp4"
        job_id = job_dir.name
        state = str(status.get("state", "unknown"))
        invalid_completed_result = False
        actively_managed = (
            job_id in positions or self._active_job_id() == job_id
        ) and state in _ACTIVE_STATES
        if state in {"failed", "cancelled"}:
            # A force-rerun deliberately keeps the last good result until a
            # replacement is validated.  Its terminal state must therefore
            # win over the old file's existence.
            pass
        elif actively_managed:
            pass
        elif result.is_file() and _valid_result(result):
            state = "completed"
        elif result.is_file() and state == "completed":
            state = "failed"
            invalid_completed_result = True
        elif job_id in positions and state not in _ACTIVE_STATES:
            state = "queued"
        elif state == "unknown" and (job_dir / "pipeline.vpipeline").is_file() and request:
            state = "failed"
        try:
            progress = int(status.get("progress", 100 if state == "completed" else 0))
        except (TypeError, ValueError):
            progress = 0
        prompt = str(request.get("prompt", "")).strip()
        seed = request.get("seed", "")
        updated = status.get("updated_at", status.get("created_at", 0))
        try:
            updated_at = float(updated)
        except (TypeError, ValueError):
            updated_at = 0.0
        video_url = ""
        if state == "completed":
            video_url = (
                "/view?filename=result.mp4&subfolder="
                f"{self.config.output_subdir}/{job_id}&type=output"
            )
        return {
            "job_id": job_id,
            "state": state,
            "progress": max(0, min(100, progress)),
            "message": (
                "Cached result is not a playable video"
                if invalid_completed_result
                else str(
                    status.get(
                        "message",
                        "Incomplete job; retry is available"
                        if state == "failed"
                        else state,
                    )
                )
            ),
            "error": (
                "result.mp4 is not a playable video (no video stream or positive duration); "
                "retry this job"
                if invalid_completed_result
                else str(status.get("error", ""))
            ),
            "resource_profile": str(
                status.get(
                    "scheduler_policy",
                    status.get("resource_profile", request.get("resource_profile", "low")),
                )
            ),
            "paused": state == "paused" or bool(control.get("paused", False)),
            "queue_position": positions.get(job_id),
            "seed": seed,
            "width": request.get("width", ""),
            "height": request.get("height", ""),
            "frames": request.get("frames", ""),
            "fps": request.get("fps", ""),
            "prompt": prompt,
            "prompt_preview": prompt[:140],
            "created_at": status.get("created_at", 0),
            "updated_at": updated_at,
            "elapsed_seconds": status.get("elapsed_seconds", 0),
            "video_url": video_url,
        }

    def snapshot(self) -> dict[str, object]:
        positions = self._queue_positions()
        jobs: list[dict[str, object]] = []
        if self.job_root.is_dir() and not self.job_root.is_symlink():
            for candidate in self.job_root.glob("vpipe-*"):
                if not candidate.is_dir() or candidate.is_symlink():
                    continue
                try:
                    job_dir = self._job_dir(candidate.name)
                    jobs.append(self._job(job_dir, positions))
                except (OSError, ValueError):
                    continue
        jobs.sort(
            key=lambda item: (
                str(item["state"]) not in _ACTIVE_STATES,
                -float(item["updated_at"] or 0),
            )
        )
        heartbeat = _read_json(self.worker_root / "heartbeat.json")
        active = _read_json(self.worker_root / "active.json")
        try:
            heartbeat_age = max(0.0, time.time() - float(heartbeat.get("updated_at", 0)))
        except (TypeError, ValueError):
            heartbeat_age = float("inf")
        worker_state = str(heartbeat.get("state", "offline"))
        try:
            worker_pid = int(heartbeat.get("pid", 0))
            pid_alive = worker_pid > 1
            if pid_alive:
                os.kill(worker_pid, 0)
        except (OSError, TypeError, ValueError):
            pid_alive = False
        return {
            "snapshot_at": round(time.time(), 3),
            "worker": {
                "online": (
                    heartbeat_age <= self.config.worker_heartbeat_timeout_seconds
                    and worker_state != "starting"
                    and pid_alive
                ),
                "state": worker_state,
                "message": heartbeat.get("message", ""),
                "active_job": active.get("job_id", heartbeat.get("active_job", "")),
                "heartbeat_age_seconds": round(heartbeat_age, 1),
            },
            "jobs": jobs,
        }

    def _status(self, job_dir: Path, **updates: object) -> None:
        path = job_dir / "vpipe-status.json"
        current = _read_json(path)
        current.update(updates)
        current["updated_at"] = time.time()
        _atomic_json(path, current)

    def _control_file(self, job_dir: Path, **updates: object) -> None:
        lock_path = job_dir / "control.lock"
        with lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            path = job_dir / "control.json"
            current = _read_json(path)
            current.update(updates)
            current["control_generation"] = time.time_ns()
            current["updated_at"] = time.time()
            _atomic_json(path, current)

    def _active_job_id(self) -> str:
        return str(_read_json(self.worker_root / "active.json").get("job_id", ""))

    def _control_active(self, job_id: str, action: str) -> str:
        command = [
            sys.executable,
            str(self.project_root / "scripts" / "h3_control.py"),
            action,
            "--job",
            job_id,
        ]
        result = subprocess.run(
            command, check=False, capture_output=True, text=True, timeout=8
        )
        message = (result.stdout or result.stderr).strip()
        if result.returncode != 0:
            raise RuntimeError(message or f"Could not {action} {job_id}")
        return message

    def _retry(self, job_dir: Path) -> str:
        status = _read_json(job_dir / "vpipe-status.json")
        state = str(status.get("state", "unknown"))
        if state == "completed" and not _valid_result(job_dir / "result.mp4"):
            state = "failed"
        if state not in {"failed", "cancelled", "unknown"}:
            raise ValueError("Only failed or cancelled jobs can be retried")
        pipeline_path = job_dir / "pipeline.vpipeline"
        request = _read_json(job_dir / "request.json")
        if not pipeline_path.is_file() or not request:
            raise ValueError("This job has no reusable pipeline or request")
        control = _read_json(job_dir / "control.json")
        profile = str(
            control.get(
                "policy",
                status.get(
                    "scheduler_policy",
                    status.get(
                        "resource_profile", request.get("resource_profile", "low")
                    ),
                ),
            )
        )
        if profile not in {"low", "auto", "max"}:
            raise ValueError("Invalid resource profile in the saved request")
        (job_dir / "cancel.request").unlink(missing_ok=True)
        (job_dir / "pause.request").unlink(missing_ok=True)
        (job_dir / "result.partial.mp4").unlink(missing_ok=True)
        self._control_file(job_dir, paused=False, policy=profile)
        self.queue_root.mkdir(parents=True, exist_ok=True)
        force_rerun = bool(status.get("force_rerun", False))
        _atomic_json(
            self.queue_root / f"{job_dir.name}.json",
            {
                "schema_version": 1,
                "job_id": job_dir.name,
                "job_dir": str(job_dir),
                "pipeline_sha256": hashlib.sha256(pipeline_path.read_bytes()).hexdigest(),
                "resource_profile": profile,
                "force_rerun": force_rerun,
                "created_at": time.time(),
            },
        )
        self._status(
            job_dir,
            state="queued",
            progress=1,
            message="Queued again from ComfyUI",
            error="",
            resource_profile=profile,
            force_rerun=force_rerun,
        )
        return "Job queued again"

    def act(self, job_id: str, action: str) -> dict[str, object]:
        job_dir = self._job_dir(job_id)
        status = _read_json(job_dir / "vpipe-status.json")
        state = str(status.get("state", "unknown"))
        active = self._active_job_id() == job_id
        message = ""
        if action == "cancel":
            (job_dir / "cancel.request").touch(exist_ok=True)
            (job_dir / "pause.request").unlink(missing_ok=True)
            if not active:
                (self.queue_root / f"{job_id}.json").unlink(missing_ok=True)
                self._status(
                    job_dir,
                    state="cancelled",
                    message="Cancelled from ComfyUI",
                    error="Cancelled by user",
                )
            message = "Cancellation requested"
        elif action == "pause":
            if active:
                message = self._control_active(job_id, "pause")
            elif state in {"queued", "launching"} and (
                self.queue_root / f"{job_id}.json"
            ).is_file():
                (job_dir / "pause.request").touch(exist_ok=True)
                self._control_file(job_dir, paused=True)
                self._status(job_dir, state="paused", message="Paused before launch")
                message = "Queued job paused"
            else:
                raise ValueError("Only running or queued jobs can be paused")
        elif action == "resume":
            if active:
                message = self._control_active(job_id, "resume")
            elif state == "paused" and (self.queue_root / f"{job_id}.json").is_file():
                (job_dir / "pause.request").unlink(missing_ok=True)
                self._control_file(job_dir, paused=False)
                self._status(job_dir, state="queued", message="Resumed from ComfyUI")
                message = "Queued job resumed"
            else:
                raise ValueError("Only paused jobs can be resumed")
        elif action in {"low", "auto", "max"}:
            if active:
                message = self._control_active(job_id, action)
            elif state in {"queued", "paused", "launching"}:
                with (job_dir / "submit.lock").open("a+", encoding="utf-8") as lock:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                    latest_state = str(
                        _read_json(job_dir / "vpipe-status.json").get(
                            "state", "unknown"
                        )
                    )
                    if latest_state not in {"queued", "paused"}:
                        raise ValueError(
                            "The vpipe launch profile is already frozen for this process"
                        )
                    ticket_path = self.queue_root / f"{job_id}.json"
                    ticket = _read_json(ticket_path)
                    if not ticket:
                        raise ValueError("The queued job ticket is missing")
                    ticket["resource_profile"] = action
                    _atomic_json(ticket_path, ticket)
                    self._control_file(job_dir, policy=action)
                    self._status(job_dir, resource_profile=action)
                message = f"Queued job switched to {action}"
            else:
                raise ValueError("Only active or queued jobs can change profile")
        elif action == "retry":
            message = self._retry(job_dir)
        else:
            raise ValueError("Unsupported vpipe job action")
        return {"ok": True, "message": message, "job": self._job(job_dir, self._queue_positions())}


def register_comfy_routes() -> None:
    global _ROUTES_REGISTERED
    if _ROUTES_REGISTERED:
        return

    import folder_paths
    from aiohttp import web
    from server import PromptServer

    project_root = Path(__file__).resolve().parents[1]

    @PromptServer.instance.routes.get("/h3/vpipe/jobs")
    async def list_vpipe_jobs(_request):
        manager = VPipeJobManager(
            project_root, Path(folder_paths.get_output_directory())
        )
        return web.json_response(
            manager.snapshot(),
            headers={
                "Cache-Control": "no-store, max-age=0",
                "Pragma": "no-cache",
            },
        )

    @PromptServer.instance.routes.post("/h3/vpipe/jobs/{job_id}/{action}")
    async def control_vpipe_job(request):
        manager = VPipeJobManager(
            project_root, Path(folder_paths.get_output_directory())
        )
        try:
            result = manager.act(
                request.match_info["job_id"], request.match_info["action"]
            )
        except (OSError, RuntimeError, ValueError) as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=409)
        return web.json_response(result)

    _ROUTES_REGISTERED = True
