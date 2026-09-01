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
from .scheduler import (
    AdaptiveScheduler,
    ResourceHealth,
    process_group_alive,
    resource_health,
)
from .vpipe import (
    VPipeConfig,
    _valid_video_file,
    _progress_from_line,
    build_vpipe_command,
    load_vpipe_config,
    validate_vpipe_installation,
)


_MIB = 1024 * 1024


class _RetryableMemoryPressureError(RuntimeError):
    """The engine refused a launch safely and the same ticket may be retried."""


class _PausedBeforeLaunch(RuntimeError):
    """A durable ticket was paused before any model process could start."""


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
        self.recovery_path = self.root / "memory-recovery.json"
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
        self._last_readiness_at = float("-inf")
        self._last_readiness_error = ""

    def _readiness_error(self, *, force: bool = False) -> str:
        """Return a cached, actionable preflight error for release installs."""

        now = time.monotonic()
        if not force and now - self._last_readiness_at < 30.0:
            return self._last_readiness_error
        error = ""
        try:
            validate_vpipe_installation(self.vpipe_config)
            # Test/fake configurations intentionally omit an expected build ref.
            # Release configurations always load it from versions.env and must
            # also pass the complete Q8 + LoRA verifier before a ticket starts.
            if self.vpipe_config.expected_ref:
                verifier = self.project_root / "scripts" / "verify_vpipe_assets.py"
                result = subprocess.run(
                    [
                        sys.executable,
                        str(verifier),
                        "--project-root",
                        str(self.project_root),
                        *([] if force else ["--files-only"]),
                    ],
                    cwd=self.project_root,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode != 0:
                    detail = (result.stdout or result.stderr).strip()
                    error = detail or "vpipe Q8 assets are incomplete"
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            error = str(exc)
        self._last_readiness_at = now
        self._last_readiness_error = error
        return error

    @staticmethod
    def _growth_rate(
        previous: int | None,
        current: int | None,
        elapsed: float,
    ) -> float | None:
        if previous is None or current is None or elapsed <= 0:
            return None
        if current <= previous:
            return 0.0
        return (current - previous) / _MIB * 60.0 / elapsed

    def _memory_gate_reason(
        self,
        current: ResourceHealth,
        previous: ResourceHealth | None,
        elapsed: float,
    ) -> tuple[str, dict[str, object]]:
        details: dict[str, object] = {}
        free = current.memory_free_percent
        if free is not None:
            details["memory_free_percent"] = round(free, 1)
            if free < self.vpipe_config.worker_min_memory_free_percent:
                return "memory pressure has not recovered", details

        total = current.physical_memory_bytes
        if free is not None and total is not None:
            reclaimable_mb = total * free / 100.0 / _MIB
            details["estimated_reclaimable_mb"] = round(reclaimable_mb)
            if reclaimable_mb < self.vpipe_config.worker_min_reclaimable_mb:
                return "reclaimable memory is below the vpipe launch floor", details

        wired = current.wired_bytes
        if wired is not None and total is not None and total > 0:
            wired_percent = wired / total * 100.0
            details["wired_percent"] = round(wired_percent, 1)
            details["wired_mb"] = round(wired / _MIB)
            if wired_percent > self.vpipe_config.worker_max_wired_percent:
                return "system wired memory is still too high", details

        if previous is not None:
            swap_growth = self._growth_rate(
                previous.swap_used_bytes, current.swap_used_bytes, elapsed
            )
            pageout_growth = self._growth_rate(
                previous.pageout_bytes, current.pageout_bytes, elapsed
            )
            if swap_growth is not None:
                details["swap_growth_mib_per_minute"] = round(swap_growth, 1)
                if (
                    swap_growth
                    >= self.bridge_config.auto_swap_growth_pause_mib_per_minute
                ):
                    return "swap is still growing", details
            if pageout_growth is not None:
                details["pageout_growth_mib_per_minute"] = round(pageout_growth, 1)
                if (
                    pageout_growth
                    >= self.bridge_config.auto_pageout_pause_mib_per_minute
                ):
                    return "pageouts are still growing", details
        return "", details

    def _wait_for_memory_recovery(self, job_dir: Path) -> None:
        recovery = _read_json(self.recovery_path)
        try:
            not_before = float(recovery.get("not_before", 0.0))
        except (TypeError, ValueError):
            not_before = 0.0
        previous: ResourceHealth | None = None
        previous_at = 0.0
        stable = 0
        while True:
            if (job_dir / "cancel.request").exists():
                raise InterruptedError("vpipe generation cancelled before launch.")
            self._raise_if_paused_before_launch(job_dir)
            sampled_at = time.monotonic()
            current = resource_health()
            reason, details = self._memory_gate_reason(
                current,
                previous,
                sampled_at - previous_at if previous is not None else 0.0,
            )
            cooldown_remaining = max(0.0, not_before - time.time())
            if cooldown_remaining > 0:
                reason = f"cooling down after the previous shot ({cooldown_remaining:.0f}s)"
                details["cooldown_remaining_seconds"] = round(cooldown_remaining)
            if reason:
                stable = 0
            else:
                stable += 1
                if stable >= self.vpipe_config.worker_memory_stable_samples:
                    self._raise_if_paused_before_launch(job_dir)
                    self._status(
                        job_dir,
                        state="queued",
                        progress=1,
                        message="Memory recovered; starting vpipe",
                        memory_gate=details,
                    )
                    return
                reason = (
                    "verifying stable memory recovery "
                    f"({stable}/{self.vpipe_config.worker_memory_stable_samples})"
                )
            self._status(
                job_dir,
                state="queued",
                progress=1,
                message=f"Waiting for memory recovery: {reason}",
                memory_gate=details,
            )
            self.heartbeat(state="waiting-memory", message=reason)
            previous = current
            previous_at = sampled_at
            time.sleep(self.vpipe_config.worker_memory_poll_seconds)

    def _raise_if_paused_before_launch(self, job_dir: Path) -> None:
        """Return a paused ticket without holding the worker or global lock."""

        if (job_dir / "cancel.request").exists():
            raise InterruptedError("vpipe generation cancelled before launch.")
        if (job_dir / "pause.request").exists():
            self._status(
                job_dir,
                state="paused",
                progress=1,
                message="Paused before vpipe model launch",
                error="",
            )
            self.heartbeat(state="paused", message="job paused before launch")
            raise _PausedBeforeLaunch("vpipe job paused before model launch")

    def _record_memory_cooldown(self, job_id: str) -> None:
        now = time.time()
        _atomic_json(
            self.recovery_path,
            {
                "schema_version": 1,
                "last_job": job_id,
                "last_engine_exit_at": now,
                "not_before": now + self.vpipe_config.worker_cooldown_seconds,
            },
        )

    @staticmethod
    def _memory_refusal(log_path: Path) -> bool:
        try:
            with log_path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 128 * 1024))
                tail = handle.read().decode("utf-8", errors="replace").lower()
        except OSError:
            return False
        return any(
            marker in tail
            for marker in (
                "not enough memory for a",
                "refusing rather than thrashing",
                "wired metal buffers cannot be paged out",
            )
        )

    def _can_retry_memory_refusal(
        self, ticket: dict[str, object], log_path: Path
    ) -> bool:
        try:
            attempts = int(ticket.get("memory_retry_attempts", 0))
        except (TypeError, ValueError):
            attempts = self.vpipe_config.worker_memory_retry_limit
        return (
            attempts < self.vpipe_config.worker_memory_retry_limit
            and self._memory_refusal(log_path)
        )

    def _retain_memory_retry_ticket(
        self,
        ticket_path: Path,
        job_dir: Path,
        error: str,
    ) -> bool:
        ticket = _read_json(ticket_path)
        if not ticket or not self._can_retry_memory_refusal(
            ticket, job_dir / "engine.log"
        ):
            return False
        retry_ticket = dict(ticket)
        retry_ticket["memory_retry_attempts"] = (
            int(ticket.get("memory_retry_attempts", 0)) + 1
        )
        retry_ticket["last_memory_error"] = error
        _atomic_json(ticket_path, retry_ticket)
        self._status(
            job_dir,
            state="queued",
            progress=1,
            message="Waiting for memory recovery before retrying the same shot",
            error="",
            last_memory_error=error,
            memory_retry_attempts=retry_ticket["memory_retry_attempts"],
        )
        return True

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
                self._raise_if_paused_before_launch(job_dir)
            except BaseException:
                lock.close()
                raise
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                if (job_dir / "pause.request").exists():
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                    lock.close()
                    self._raise_if_paused_before_launch(job_dir)
                    lock = lock_path.open("a+", encoding="utf-8")
                    continue
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
        ticket_path = self.queue_root / f"{job_id}.json"
        latest_ticket = _read_json(ticket_path)
        if latest_ticket:
            ticket = latest_ticket
        pipeline_path = job_dir / "pipeline.vpipeline"
        expected_digest = str(ticket.get("pipeline_sha256", ""))
        actual_digest = hashlib.sha256(pipeline_path.read_bytes()).hexdigest()
        if not expected_digest or actual_digest != expected_digest:
            raise RuntimeError("vpipe pipeline changed after it was queued")
        result_path = job_dir / "result.mp4"
        partial_path = job_dir / "result.partial.mp4"
        log_path = job_dir / "engine.log"
        cancel_path = job_dir / "cancel.request"
        if self._valid_recovered_video(result_path):
            if not bool(ticket.get("force_rerun", False)):
                self._status(
                    job_dir,
                    state="completed",
                    progress=100,
                    message="Reused completed vpipe job",
                    elapsed_seconds=0.0,
                )
                return
        partial_path.unlink(missing_ok=True)
        self._wait_for_memory_recovery(job_dir)
        lock = self._wait_for_generation_lock(job_dir)
        try:
            self._raise_if_paused_before_launch(job_dir)
        except BaseException:
            lock.close()
            raise
        # Serialize the last queued profile change with launch-time cap
        # selection.  Once state becomes `launching`, later profile controls
        # affect scheduling only after the process is active; they cannot
        # pretend to replace already-frozen vpipe pool caps.
        with (job_dir / "submit.lock").open("a+", encoding="utf-8") as submit_lock:
            fcntl.flock(submit_lock.fileno(), fcntl.LOCK_EX)
            latest_ticket = _read_json(ticket_path)
            if latest_ticket:
                ticket = latest_ticket
            control = _read_json(job_dir / "control.json")
            profile = str(control.get("policy", ticket.get("resource_profile", "low")))
            if profile not in {"low", "auto", "max"}:
                raise ValueError("Invalid vpipe resource profile")
            self._status(
                job_dir,
                state="launching",
                progress=1,
                message=f"Launching vpipe; {profile} memory caps are now fixed",
                resource_profile=profile,
                launch_resource_profile=profile,
            )
        command = build_vpipe_command(self.vpipe_config, profile, pipeline_path)
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
                    self._raise_if_paused_before_launch(job_dir)
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
                self._raise_if_paused_before_launch(job_dir)
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
                scheduler.start(preserve_existing_control=True)
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
                    if self._can_retry_memory_refusal(ticket, log_path):
                        raise _RetryableMemoryPressureError(
                            "vpipe refused the launch under memory pressure; "
                            "waiting to retry the same shot"
                        )
                    raise RuntimeError(
                        f"vpipe exited with status {return_code}. See {log_path}"
                    )
                if not self._valid_recovered_video(partial_path):
                    if self._can_retry_memory_refusal(ticket, log_path):
                        raise _RetryableMemoryPressureError(
                            "vpipe refused the launch under memory pressure; "
                            "waiting to retry the same shot"
                        )
                    raise RuntimeError(
                        f"vpipe finished without a valid video. See {log_path}"
                    )
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
            retrying = isinstance(exc, _RetryableMemoryPressureError)
            paused_before_launch = isinstance(exc, _PausedBeforeLaunch)
            terminal_state = (
                "paused"
                if paused_before_launch
                else "cancelled"
                if isinstance(exc, InterruptedError)
                else "failed"
            )
            if scheduler is not None:
                scheduler.finish(terminal_state, str(exc))
            if not paused_before_launch and not self.bridge_config.keep_failed_output:
                partial_path.unlink(missing_ok=True)
            self._status(
                job_dir,
                state="queued" if retrying else terminal_state,
                message=(
                    "Memory pressure detected; cooling down before one automatic retry"
                    if retrying
                    else "Paused before vpipe model launch"
                    if paused_before_launch
                    else terminal_state
                ),
                error="" if retrying or paused_before_launch else str(exc),
                last_memory_error=str(exc) if retrying else "",
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
            if process is not None:
                self._record_memory_cooldown(job_id)
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
            ticket_path = self.queue_root / f"{job_id}.json"
            keep_ticket = False
            if self._valid_recovered_video(partial):
                partial.replace(result)
                self._status(
                    job_dir,
                    state="completed",
                    progress=100,
                    message="vpipe complete after worker recovery",
                )
            else:
                keep_ticket = self._retain_memory_retry_ticket(
                    ticket_path,
                    job_dir,
                    "vpipe hit memory pressure while its worker was restarting",
                )
                if not keep_ticket:
                    self._status(
                        job_dir,
                        state="failed",
                        message="failed",
                        error="vpipe exited while its launchd worker was restarting",
                    )
            self._remove_recovered_registration(job_dir, pgid, signature)
            if not keep_ticket:
                ticket_path.unlink(missing_ok=True)
            self._record_memory_cooldown(job_id)
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
        ticket_path = self.queue_root / f"{job_id}.json"
        keep_ticket = False
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
            keep_ticket = self._retain_memory_retry_ticket(
                ticket_path,
                job_dir,
                "surviving vpipe exited under memory pressure",
            )
            if not keep_ticket:
                self._status(
                    job_dir,
                    state="failed",
                    message="failed",
                    error="surviving vpipe exited without a video",
                )
        self._remove_recovered_registration(job_dir, pgid, signature)
        if not keep_ticket:
            ticket_path.unlink(missing_ok=True)
        self._record_memory_cooldown(job_id)
        self.active_path.unlink(missing_ok=True)
        self._active_job = ""

    @staticmethod
    def _valid_recovered_video(path: Path) -> bool:
        return _valid_video_file(path)

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
                readiness_error = self._readiness_error()
                if readiness_error:
                    self.heartbeat(state="degraded", message=readiness_error)
                else:
                    self.heartbeat()
                if once:
                    return 0
                time.sleep(0.5)
                continue
            ticket_path = next(
                (
                    path
                    for path in tickets
                    if not (
                        Path(str(_read_json(path).get("job_dir", "")))
                        / "pause.request"
                    ).exists()
                    or (
                        Path(str(_read_json(path).get("job_dir", "")))
                        / "cancel.request"
                    ).exists()
                ),
                None,
            )
            if ticket_path is None:
                self._active_job = ""
                self.heartbeat(state="paused", message="queued jobs paused by user")
                if once:
                    return 0
                time.sleep(0.5)
                continue
            ticket = _read_json(ticket_path)
            job_id = str(ticket.get("job_id", ""))
            job_dir: Path | None = None
            keep_ticket = False
            preserve_heartbeat = False
            try:
                job_dir = self._canonical_job_dir(job_id, ticket.get("job_dir", ""))
                if (job_dir / "cancel.request").exists():
                    raise InterruptedError("vpipe generation cancelled before launch.")
                if not ticket_path.is_file():
                    # ComfyUI may withdraw a queued ticket while the worker is
                    # selecting it.  Preserve the UI's terminal status.
                    continue
                readiness_error = self._readiness_error(force=True)
                if (job_dir / "cancel.request").exists():
                    raise InterruptedError("vpipe generation cancelled before launch.")
                if not ticket_path.is_file():
                    # In particular, do not overwrite a concurrent cancelled
                    # state with "Waiting for assets" after a slow verifier.
                    continue
                if readiness_error:
                    keep_ticket = True
                    preserve_heartbeat = True
                    self._status(
                        job_dir,
                        state="queued",
                        progress=1,
                        message=f"Waiting for vpipe assets: {readiness_error}",
                    )
                    self.heartbeat(state="degraded", message=readiness_error)
                    if once:
                        return 0
                    time.sleep(5.0)
                    continue
                self._active_job = job_id
                self.heartbeat(state="starting-job")
                self._launch(ticket, job_dir)
            except BaseException as exc:
                try:
                    if job_dir is not None:
                        if isinstance(exc, _PausedBeforeLaunch):
                            keep_ticket = True
                            self._status(
                                job_dir,
                                state="paused",
                                progress=1,
                                message="Paused before vpipe model launch",
                                error="",
                            )
                        elif isinstance(exc, _RetryableMemoryPressureError):
                            keep_ticket = self._retain_memory_retry_ticket(
                                ticket_path,
                                job_dir,
                                str(exc),
                            )
                            if not keep_ticket:
                                self._status(
                                    job_dir,
                                    state="failed",
                                    message="worker error",
                                    error=str(exc),
                                )
                        else:
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
                if not keep_ticket:
                    ticket_path.unlink(missing_ok=True)
                self._active_job = ""
                if not preserve_heartbeat:
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
