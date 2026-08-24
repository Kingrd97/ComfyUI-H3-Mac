#!/usr/bin/env python3
"""Persistent launchd worker for vpipe jobs submitted by ComfyUI.

The worker is the lifetime controller.  ComfyUI only publishes a durable job
ticket and observes status, so closing/restarting the UI does not terminate an
active Metal process.  launchd keeps this small controller alive; it must not
keep an individual successful vpipe command alive, which would rerun finished
shots forever.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import selectors
import signal
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

from .config import BridgeConfig, load_config
from .job_registry import (
    JobRegistration,
    finish_job,
    mark_cleanup_needed,
    register_starting_job,
    registered_jobs,
    remove_registered_job,
)
from .locking import publication_control_guard
from .runner import (
    _original_process_group_state,
    _stable_process_start_signature,
    _terminate_process_group,
    _write_generation_lock_metadata,
)
from .scheduler import AdaptiveScheduler, process_group_alive
from .vpipe import VPipeConfig, _progress_from_line, build_vpipe_command, load_vpipe_config


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class VPipeWorker:
    def __init__(
        self,
        project_root: Path,
        *,
        vpipe_config: VPipeConfig | None = None,
        bridge_config: BridgeConfig | None = None,
    ):
        self.project_root = project_root.resolve()
        self.root = self.project_root / "runtime" / "vpipe-worker"
        self.queue_root = self.root / "queue"
        self.heartbeat_path = self.root / "heartbeat.json"
        self.active_path = self.root / "active.json"
        self.output_root = (
            self.project_root / "runtime" / "ComfyUI" / "output"
        ).resolve()
        self.vpipe_config = vpipe_config or load_vpipe_config(self.project_root)
        selected_bridge = bridge_config or load_config(self.project_root / "config.json")
        self.bridge_config = replace(selected_bridge, project_root=self.project_root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.queue_root.mkdir(parents=True, exist_ok=True)
        self._active_job = ""
        self._last_heartbeat_at = float("-inf")
        self._last_heartbeat_signature: tuple[str, str, str] | None = None

    def heartbeat(self, *, state: str = "idle", message: str = "") -> None:
        now = time.monotonic()
        signature = (state, self._active_job, message)
        if (
            signature == self._last_heartbeat_signature
            and now - self._last_heartbeat_at < 1.0
        ):
            return
        _atomic_json(
            self.heartbeat_path,
            {
                "schema_version": 1,
                "pid": os.getpid(),
                "state": state,
                "active_job": self._active_job,
                "message": message,
                "updated_at": time.time(),
            },
        )
        self._last_heartbeat_at = now
        self._last_heartbeat_signature = signature

    def _canonical_job_dir(self, job_id: str, raw: object) -> Path:
        if not job_id.startswith("vpipe-") or len(job_id) != 26:
            raise ValueError("Invalid vpipe worker job ID")
        expected_parent = self.output_root / self.vpipe_config.output_subdir
        candidate = Path(str(raw))
        if not candidate.is_absolute():
            raise ValueError("vpipe worker job directory must be absolute")
        resolved = candidate.resolve(strict=True)
        if (
            candidate != resolved
            or resolved.parent != expected_parent
            or resolved.name != job_id
        ):
            raise ValueError("vpipe worker job directory is outside ComfyUI output")
        return resolved

    @staticmethod
    def _status_path(job_dir: Path) -> Path:
        return job_dir / "vpipe-status.json"

    def _status(self, job_dir: Path, **values: object) -> None:
        path = self._status_path(job_dir)
        current = _read_json(path)
        current.update(values)
        current["updated_at"] = time.time()
        _atomic_json(path, current)

    def _wait_for_generation_lock(self, job_dir: Path):
        lock_path = self.project_root / "runtime" / "h3-generation.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = lock_path.open("a+", encoding="utf-8")
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return lock
            except BlockingIOError:
                if (job_dir / "cancel.request").exists():
                    lock.close()
                    raise InterruptedError("vpipe generation cancelled before launch.")
                self._status(
                    job_dir,
                    state="queued",
                    progress=1,
                    message="Waiting for the active H3/vpipe job",
                )
                self.heartbeat(state="waiting", message="generation lock occupied")
                time.sleep(0.5)

    def _drain_log(
        self,
        job_dir: Path,
        log_path: Path,
        offset: int,
        remainder: str,
        reported: int,
    ) -> tuple[int, str, int]:
        try:
            with log_path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(offset)
                text = handle.read()
                offset = handle.tell()
        except OSError:
            return offset, remainder, reported
        records = (remainder + text).split("\n")
        remainder = records.pop()
        for line in records:
            update = _progress_from_line(line)
            if update and update[0] > reported:
                reported = update[0]
                self._status(
                    job_dir,
                    state="running",
                    progress=reported,
                    message=update[1],
                )
        return offset, remainder, reported

    def _launch(self, ticket: dict[str, object], job_dir: Path) -> None:
        job_id = str(ticket["job_id"])
        pipeline_path = job_dir / "pipeline.vpipeline"
        expected_digest = str(ticket.get("pipeline_sha256", ""))
        actual_digest = hashlib.sha256(pipeline_path.read_bytes()).hexdigest()
        if not expected_digest or actual_digest != expected_digest:
            raise RuntimeError("vpipe pipeline changed after it was queued")
        profile = str(ticket.get("resource_profile", "low"))
        if profile not in {"low", "auto", "max"}:
            raise ValueError("Invalid vpipe resource profile")

        result_path = job_dir / "result.mp4"
        partial_path = job_dir / "result.partial.mp4"
        log_path = job_dir / "engine.log"
        cancel_path = job_dir / "cancel.request"
        if result_path.is_file() and result_path.stat().st_size > 0:
            self._status(
                job_dir,
                state="completed",
                progress=100,
                message="Reused completed vpipe job",
                elapsed_seconds=0.0,
            )
            return
        partial_path.unlink(missing_ok=True)
        command = build_vpipe_command(self.vpipe_config, profile, pipeline_path)
        lock = self._wait_for_generation_lock(job_dir)
        started = time.monotonic()
        process: subprocess.Popen[bytes] | None = None
        scheduler: AdaptiveScheduler | None = None
        registration: JobRegistration | None = None
        terminal_state = "failed"
        engine_signature = ""
        registry_cleanup_safe = True
        gate_read_fd = gate_write_fd = ack_read_fd = ack_write_fd = -1
        controller_pid = os.getpid()
        controller_signature = _stable_process_start_signature(controller_pid)
        if not controller_signature:
            lock.close()
            raise RuntimeError("Could not identify the launchd vpipe worker process")

        try:
            registration = register_starting_job(
                self.project_root,
                job_dir,
                job_id,
                self.vpipe_config.output_subdir,
                controller_pid=controller_pid,
                controller_start_signature=controller_signature,
                engine_profile=profile,
            )
            with publication_control_guard(self.project_root):
                _write_generation_lock_metadata(
                    lock.fileno(),
                    controller_pid=controller_pid,
                    controller_start_signature=controller_signature,
                    registration_token=registration.token,
                    job_id=job_id,
                )

            launcher = Path(__file__).with_name("h3_launch.py").resolve(strict=True)
            with log_path.open("w", encoding="utf-8", buffering=1) as log:
                log.write("COMMAND " + json.dumps(command, ensure_ascii=False) + "\n")
                log.flush()
                gate_read_fd, gate_write_fd = os.pipe()
                ack_read_fd, ack_write_fd = os.pipe()
                child_command = [
                    sys.executable,
                    str(launcher),
                    "--project-root",
                    str(self.project_root),
                    "--registry",
                    str(registration.entry_path),
                    "--token",
                    registration.token,
                    "--gate-fd",
                    str(gate_read_fd),
                    "--ack-fd",
                    str(ack_write_fd),
                    "--",
                    *command,
                ]
                try:
                    process = subprocess.Popen(
                        child_command,
                        cwd=self.vpipe_config.work_dir,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                        pass_fds=(lock.fileno(), gate_read_fd, ack_write_fd),
                    )
                finally:
                    os.close(gate_read_fd)
                    gate_read_fd = -1
                    os.close(ack_write_fd)
                    ack_write_fd = -1
                registry_cleanup_safe = False
                engine_signature = _stable_process_start_signature(process.pid)
                if not engine_signature:
                    raise RuntimeError("Could not identify the gated vpipe process")
                scheduler = AdaptiveScheduler(
                    job_dir,
                    process.pid,
                    profile,  # type: ignore[arg-type]
                    self.bridge_config,
                    controller_pid=controller_pid,
                    controller_start_signature=controller_signature,
                )
                os.write(gate_write_fd, b"G")
                os.close(gate_write_fd)
                gate_write_fd = -1
                os.set_blocking(ack_read_fd, False)
                with selectors.DefaultSelector() as selector:
                    selector.register(ack_read_fd, selectors.EVENT_READ)
                    deadline = time.monotonic() + 5.0
                    activated = False
                    while time.monotonic() < deadline:
                        if process.poll() is not None:
                            break
                        if selector.select(timeout=0.05):
                            activated = os.read(ack_read_fd, 1) == b"A"
                            break
                os.close(ack_read_fd)
                ack_read_fd = -1
                if not activated:
                    raise RuntimeError("vpipe launcher did not activate its registry")
                scheduler.start()
                _atomic_json(
                    self.active_path,
                    {
                        "schema_version": 1,
                        "job_id": job_id,
                        "job_dir": str(job_dir),
                        "pgid": process.pid,
                        "process_start_signature": engine_signature,
                        "controller_pid": controller_pid,
                        "controller_start_signature": controller_signature,
                        "resource_profile": profile,
                        "started_at": time.time(),
                    },
                )
                self._status(
                    job_dir,
                    state="running",
                    progress=1,
                    message="Starting vpipe under launchd",
                    pgid=process.pid,
                    process_start_signature=engine_signature,
                    started_at=time.time(),
                )
                offset = 0
                remainder = ""
                reported = 1
                last_worker_status_at = float("-inf")
                last_worker_state = ""
                while process.poll() is None:
                    decision = scheduler.tick()
                    offset, remainder, reported = self._drain_log(
                        job_dir, log_path, offset, remainder, reported
                    )
                    worker_state = "paused" if decision.paused else "running"
                    now = time.monotonic()
                    if (
                        worker_state != last_worker_state
                        or now - last_worker_status_at >= 2.0
                    ):
                        self._status(
                            job_dir,
                            state=worker_state,
                            progress=reported,
                            message=decision.reason if decision.paused else str(
                                _read_json(self._status_path(job_dir)).get(
                                    "message", "Generating with vpipe"
                                )
                            ),
                            scheduler_policy=decision.policy,
                        )
                        last_worker_status_at = now
                        last_worker_state = worker_state
                    self.heartbeat(state="paused" if decision.paused else "running")
                    if cancel_path.exists():
                        scheduler.prepare_termination()
                        raise InterruptedError("vpipe generation cancelled.")
                    time.sleep(0.25)
                offset, remainder, reported = self._drain_log(
                    job_dir, log_path, offset, remainder, reported
                )
                return_code = process.wait()
                registry_cleanup_safe = _original_process_group_state(
                    process.pid, engine_signature
                ) in {"gone", "reused"}
                if return_code != 0:
                    raise RuntimeError(
                        f"vpipe exited with status {return_code}. See {log_path}"
                    )
                if not partial_path.is_file() or partial_path.stat().st_size == 0:
                    raise RuntimeError(f"vpipe finished without a video. See {log_path}")
                partial_path.replace(result_path)
                elapsed = time.monotonic() - started
                scheduler.finish("completed")
                terminal_state = "completed"
                self._status(
                    job_dir,
                    state="completed",
                    progress=100,
                    message="vpipe complete",
                    elapsed_seconds=elapsed,
                    error="",
                )
        except BaseException as exc:
            if scheduler is not None:
                scheduler.prepare_termination()
            if process is not None:
                registry_cleanup_safe = _terminate_process_group(
                    process, engine_signature
                )
            terminal_state = "cancelled" if isinstance(exc, InterruptedError) else "failed"
            if scheduler is not None:
                scheduler.finish(terminal_state, str(exc))
            if not self.bridge_config.keep_failed_output:
                partial_path.unlink(missing_ok=True)
            self._status(
                job_dir,
                state=terminal_state,
                message=terminal_state,
                error=str(exc),
                elapsed_seconds=time.monotonic() - started,
            )
            raise
        finally:
            for descriptor in (gate_read_fd, gate_write_fd, ack_read_fd, ack_write_fd):
                if descriptor >= 0:
                    os.close(descriptor)
            if registration is not None:
                if registry_cleanup_safe:
                    finish_job(registration, terminal_state)
                else:
                    mark_cleanup_needed(
                        registration,
                        f"{terminal_state}: vpipe process group exit unverified",
                    )
            self.active_path.unlink(missing_ok=True)
            lock.close()

    def _recover_active(self) -> None:
        active = _read_json(self.active_path)
        if not active:
            for registration in registered_jobs(
                self.project_root, self.vpipe_config.output_subdir
            ):
                if (
                    registration.job_id.startswith("vpipe-")
                    and _original_process_group_state(
                        registration.pgid,
                        registration.process_start_signature,
                    )
                    in {"exact", "leaderless"}
                ):
                    active = {
                        "schema_version": 1,
                        "job_id": registration.job_id,
                        "job_dir": str(registration.job_dir),
                        "pgid": registration.pgid,
                        "process_start_signature": registration.process_start_signature,
                        "controller_pid": registration.controller_pid,
                        "controller_start_signature": registration.controller_start_signature,
                        "resource_profile": registration.engine_profile,
                    }
                    _atomic_json(self.active_path, active)
                    break
        if not active:
            return
        job_id = str(active.get("job_id", ""))
        try:
            job_dir = self._canonical_job_dir(job_id, active.get("job_dir", ""))
            pgid = int(active.get("pgid", 0))
        except (OSError, TypeError, ValueError):
            self.active_path.unlink(missing_ok=True)
            return
        signature = str(active.get("process_start_signature", ""))
        state = _original_process_group_state(pgid, signature)
        if state not in {"exact", "leaderless"}:
            partial = job_dir / "result.partial.mp4"
            result = job_dir / "result.mp4"
            if self._valid_recovered_video(partial):
                partial.replace(result)
                self._status(
                    job_dir,
                    state="completed",
                    progress=100,
                    message="vpipe complete after worker recovery",
                )
            else:
                self._status(
                    job_dir,
                    state="failed",
                    message="failed",
                    error="vpipe exited while its launchd worker was restarting",
                )
            self._remove_recovered_registration(job_dir, pgid, signature)
            (self.queue_root / f"{job_id}.json").unlink(missing_ok=True)
            self.active_path.unlink(missing_ok=True)
            return
        self._active_job = job_id
        profile = str(active.get("resource_profile", "low"))
        controller_pid = int(active.get("controller_pid", 0))
        controller_signature = str(active.get("controller_start_signature", ""))
        scheduler = AdaptiveScheduler(
            job_dir,
            pgid,
            profile,  # type: ignore[arg-type]
            self.bridge_config,
            controller_pid=controller_pid,
            controller_start_signature=controller_signature,
        )
        cancellation_started: float | None = None
        last_status_at = float("-inf")
        last_state = ""
        while process_group_alive(pgid):
            decision = scheduler.tick()
            self.heartbeat(state="recovering", message="observing surviving vpipe")
            recovered_state = "paused" if decision.paused else "running"
            now = time.monotonic()
            if recovered_state != last_state or now - last_status_at >= 2.0:
                self._status(
                    job_dir,
                    state=recovered_state,
                    message="Recovered after worker restart",
                    scheduler_policy=decision.policy,
                )
                last_state = recovered_state
                last_status_at = now
            if (job_dir / "cancel.request").exists():
                scheduler.prepare_termination()
                if cancellation_started is None:
                    os.killpg(pgid, signal.SIGTERM)
                    cancellation_started = now
                elif now - cancellation_started >= 10.0:
                    os.killpg(pgid, signal.SIGKILL)
            time.sleep(0.5)
        partial = job_dir / "result.partial.mp4"
        result = job_dir / "result.mp4"
        if self._valid_recovered_video(partial):
            partial.replace(result)
            scheduler.finish("completed")
            self._status(
                job_dir,
                state="completed",
                progress=100,
                message="vpipe complete after worker recovery",
            )
        else:
            scheduler.finish("failed", "surviving vpipe exited without a video")
            self._status(
                job_dir,
                state="failed",
                message="failed",
                error="surviving vpipe exited without a video",
            )
        self._remove_recovered_registration(job_dir, pgid, signature)
        (self.queue_root / f"{job_id}.json").unlink(missing_ok=True)
        self.active_path.unlink(missing_ok=True)
        self._active_job = ""

    @staticmethod
    def _valid_recovered_video(path: Path) -> bool:
        if not path.is_file() or path.stat().st_size == 0:
            return False
        ffprobe = shutil.which("ffprobe")
        if ffprobe is None:
            return True
        try:
            result = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=codec_type",
                    "-of",
                    "default=nw=1:nk=1",
                    str(path),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0 and "video" in result.stdout

    def _remove_recovered_registration(
        self, job_dir: Path, pgid: int, signature: str
    ) -> None:
        for registration in list(
            registered_jobs(self.project_root, self.vpipe_config.output_subdir)
        ):
            if registration.job_dir == job_dir and registration.pgid == pgid:
                remove_registered_job(
                    self.project_root,
                    self.vpipe_config.output_subdir,
                    job_dir,
                    pgid=pgid,
                    process_start_signature=signature,
                )

    def serve(self, *, once: bool = False) -> int:
        self.heartbeat(state="starting")
        self._recover_active()
        while True:
            tickets = sorted(
                self.queue_root.glob("vpipe-*.json"),
                key=lambda path: path.stat().st_mtime,
            )
            if not tickets:
                self._active_job = ""
                self.heartbeat()
                if once:
                    return 0
                time.sleep(0.5)
                continue
            ticket_path = tickets[0]
            ticket = _read_json(ticket_path)
            job_id = str(ticket.get("job_id", ""))
            job_dir: Path | None = None
            try:
                job_dir = self._canonical_job_dir(job_id, ticket.get("job_dir", ""))
                self._active_job = job_id
                self.heartbeat(state="starting-job")
                self._launch(ticket, job_dir)
            except BaseException as exc:
                try:
                    if job_dir is not None:
                        self._status(
                            job_dir,
                            state=(
                                "cancelled"
                                if isinstance(exc, InterruptedError)
                                else "failed"
                            ),
                            message="worker error",
                            error=str(exc),
                        )
                except OSError:
                    pass
            finally:
                ticket_path.unlink(missing_ok=True)
                self._active_job = ""
                self.heartbeat()
            if once:
                return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Persistent vpipe launchd worker")
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    return VPipeWorker(args.project_root).serve(once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
