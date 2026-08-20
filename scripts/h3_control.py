#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from h3_bridge.config import load_config  # noqa: E402
from h3_bridge.job_registry import (  # noqa: E402
    RegisteredJob,
    registered_jobs,
    remove_registered_job,
)
from h3_bridge.locking import publication_control_guard  # noqa: E402
from h3_bridge.scheduler import (  # noqa: E402
    process_group_alive,
    process_start_signature,
    read_json,
    signal_process_group,
)


def atomic_json(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def process_matches_h3(pgid: int, h3_binary: Path) -> bool:
    """Refuse to signal a stale job whose process-group ID was reused."""

    try:
        result = subprocess.run(
            ["/bin/ps", "-ww", "-g", str(pgid), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    executable = str(h3_binary.resolve())
    return result.returncode == 0 and any(
        command == executable or command.startswith(executable + " ")
        for command in (line.strip() for line in result.stdout.splitlines())
    )


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _stable_process_birth(pgid: int, attempts: int = 3) -> str:
    """Retry transient ps failures before treating a group leader as absent."""

    for attempt in range(attempts):
        birth = process_start_signature(pgid)
        if birth:
            return birth
        if attempt + 1 < attempts:
            time.sleep(0.05)
    return ""


def _original_group_state(pgid: int, expected_birth: str) -> str:
    """Classify a process group without treating a failed ps as authorization."""

    if pgid <= 1 or not process_group_alive(pgid):
        return "gone"
    current_birth = _stable_process_birth(pgid)
    if current_birth:
        return "exact" if current_birth == expected_birth else "reused"
    if process_alive(pgid):
        return "ambiguous"
    time.sleep(0.05)
    return "leaderless" if process_group_alive(pgid) else "gone"


def original_process_group_alive(pgid: int, expected_birth: str) -> bool:
    return _original_group_state(pgid, expected_birth) in {"exact", "leaderless"}


def _read_lock_metadata(lock: TextIO) -> dict[str, object]:
    try:
        lock.seek(0)
        value = json.loads(lock.read())
    except (json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


@contextmanager
def _generation_lock_observation_guarded(
) -> Iterator[tuple[bool, dict[str, object]]]:
    """Observe the generation lock while the publication guard is held."""

    lock_path = PROJECT_ROOT / "runtime" / "h3-generation.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        acquired = False
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            pass
        metadata = _read_lock_metadata(lock)
        try:
            yield acquired, metadata
        finally:
            if acquired:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


@contextmanager
def generation_lock_observation() -> Iterator[tuple[bool, dict[str, object]]]:
    """Serialize an observation against runner publication and CLI control."""

    with publication_control_guard(PROJECT_ROOT):
        with _generation_lock_observation_guarded() as observation:
            yield observation


def _lock_matches_registration(
    metadata: dict[str, object], registration: RegisteredJob
) -> bool:
    try:
        return (
            int(metadata.get("schema_version", 0)) == 1
            and str(metadata.get("registration_token", ""))
            == registration.token
            and str(metadata.get("job_id", "")) == registration.job_id
            and int(metadata.get("controller_pid", 0))
            == registration.controller_pid
            and str(metadata.get("controller_start_signature", ""))
            == registration.controller_start_signature
        )
    except (TypeError, ValueError):
        return False


def _registry_status(registration: RegisteredJob) -> dict[str, object]:
    return {
        "pid": registration.pgid,
        "pgid": registration.pgid,
        "process_start_signature": registration.process_start_signature,
        "controller_pid": registration.controller_pid,
        "controller_start_signature": registration.controller_start_signature,
        "state": "running",
        "engine_profile": registration.engine_profile,
        "scheduler_policy": registration.engine_profile,
        "paused": False,
        "reason": "launching",
        "updated_at": registration.activated_at,
        "cleanup_needed": registration.registry_state == "cleanup-needed",
        # A synthetic status closes the controller-crash cleanup window, but
        # must not expose pause/resume before AdaptiveScheduler publishes its
        # locked control protocol.
        "_registry_only": True,
    }


def _registration_matches_status(
    registration: RegisteredJob,
    status: dict[str, object],
) -> bool:
    try:
        return (
            int(status.get("pgid", 0)) == registration.pgid
            and int(status.get("controller_pid", 0)) == registration.controller_pid
            and str(status.get("process_start_signature", ""))
            == registration.process_start_signature
            and str(status.get("controller_start_signature", ""))
            == registration.controller_start_signature
        )
    except (TypeError, ValueError):
        return False


def _job_status_candidates(
    config: object,
) -> list[tuple[Path, dict[str, object], bool]]:
    """Discover registered custom outputs plus the trusted legacy default."""

    output_subdir = str(getattr(config, "output_subdir"))
    selected: list[tuple[Path, dict[str, object], bool]] = []
    seen: set[Path] = set()
    seen_identities: set[tuple[int, str]] = set()
    for registration in registered_jobs(PROJECT_ROOT, output_subdir):
        status_path = registration.job_dir / "process.json"
        if status_path.is_symlink():
            continue
        if status_path.is_file() and registration.registry_state != "cleanup-needed":
            status = read_json(status_path)
            if not _registration_matches_status(registration, status):
                # A previous run of the same deterministic job ID can leave a
                # terminal process.json. The two-sided live registry is the
                # current authority; synthesize its identity for orphan cleanup.
                status = _registry_status(registration)
        else:
            # The child publishes this complete identity before exec. This
            # closes the Popen -> first scheduler status window if the Comfy
            # controller is killed while the child still holds the global lock.
            status = _registry_status(registration)
        selected.append((registration.job_dir, status, True))
        seen.add(registration.job_dir)
        seen_identities.add(
            (registration.pgid, registration.process_start_signature)
        )

    legacy_base = (PROJECT_ROOT / "runtime" / "ComfyUI" / "output").resolve()
    legacy_root = (legacy_base / output_subdir).resolve()
    if legacy_root.parent != legacy_base or not legacy_root.is_dir():
        return selected
    for status_path in sorted(legacy_root.glob("*/process.json")):
        if status_path.is_symlink():
            continue
        job_dir = status_path.parent.resolve()
        if job_dir in seen or job_dir.parent != legacy_root:
            continue
        status = read_json(status_path)
        try:
            identity = (
                int(status.get("pgid", 0)),
                str(status.get("process_start_signature", "")),
            )
        except (TypeError, ValueError):
            identity = (0, "")
        if identity[0] > 1 and identity[1]:
            if identity in seen_identities:
                continue
            seen_identities.add(identity)
        selected.append((job_dir, status, False))
        seen.add(job_dir)
    return selected


def orphan_jobs() -> list[tuple[Path, dict[str, object]]]:
    """Return only jobs whose engine is exact but controller is provably gone."""

    config = load_config()
    selected: list[tuple[Path, dict[str, object]]] = []
    for job_dir, status, registry_trusted in _job_status_candidates(config):
        if status.get("state") not in {"running", "paused"}:
            continue
        try:
            pgid = int(status.get("pgid", 0))
            controller_pid = int(status.get("controller_pid", 0))
        except (TypeError, ValueError):
            continue
        engine_birth = str(status.get("process_start_signature", ""))
        controller_birth = str(status.get("controller_start_signature", ""))
        if min(pgid, controller_pid) <= 1 or not engine_birth or not controller_birth:
            # Legacy/incomplete status is deliberately never auto-terminated.
            continue
        registration: RegisteredJob | None = None
        if registry_trusted:
            registration = _matching_registration(config, job_dir, status)
            if registration is None:
                # Never downgrade a two-sided registry candidate into the
                # legacy path if its token changed during discovery.
                continue
            if not original_process_group_alive(pgid, engine_birth):
                continue
        elif (
            not process_group_alive(pgid)
            or process_start_signature(pgid) != engine_birth
            or not process_matches_h3(pgid, config.h3_binary)
        ):
            continue
        controller_is_alive = process_alive(controller_pid)
        current_controller_birth = process_start_signature(controller_pid)
        if controller_is_alive and not current_controller_birth:
            # ps may have failed transiently; do not infer an orphan.
            continue
        if (
            not bool(status.get("cleanup_needed"))
            and controller_is_alive
            and current_controller_birth == controller_birth
        ):
            continue
        selected_status = dict(status)
        if registration is not None:
            selected_status["_registry_trusted"] = True
            selected_status["_registration_token"] = registration.token
        selected.append((job_dir, selected_status))
    return selected


def _matching_registration(
    config: object,
    job_dir: Path,
    status: dict[str, object],
) -> RegisteredJob | None:
    output_subdir = str(getattr(config, "output_subdir"))
    try:
        pgid = int(status.get("pgid", 0))
        engine_birth = str(status.get("process_start_signature", ""))
        controller_pid = int(status.get("controller_pid", 0))
        controller_birth = str(status.get("controller_start_signature", ""))
    except (TypeError, ValueError):
        return None
    for registration in registered_jobs(PROJECT_ROOT, output_subdir):
        if (
            registration.job_dir == job_dir
            and registration.pgid == pgid
            and registration.process_start_signature == engine_birth
            and registration.controller_pid == controller_pid
            and registration.controller_start_signature == controller_birth
        ):
            return registration
    return None


def _registered_group_is_signalable(
    registration: RegisteredJob,
    lock_metadata: dict[str, object],
) -> bool:
    """Prove an occupied generation lock and PGID still describe this job."""

    if not _lock_matches_registration(lock_metadata, registration):
        return False
    return _original_group_state(
        registration.pgid, registration.process_start_signature
    ) in {"exact", "leaderless"}


def _registered_control_authorized(registration: RegisteredJob) -> bool:
    """Authorize interactive control only for the exact inherited-lock owner."""

    with publication_control_guard(PROJECT_ROOT):
        return _registered_control_authorized_guarded(registration)


def _registered_control_authorized_guarded(
    registration: RegisteredJob,
) -> bool:
    """Authorize while the caller holds the publication/control guard."""

    with _generation_lock_observation_guarded() as (lock_available, metadata):
        if lock_available:
            return False
        if not _registration_is_current(registration):
            return False
        return _registered_group_is_signalable(registration, metadata)


def _registration_is_current(registration: RegisteredJob) -> bool:
    """Revalidate the two-sided token before touching a deterministic job dir."""

    output_subdir = registration.job_dir.parent.name
    return any(
        current.token == registration.token
        and current.entry_path == registration.entry_path
        and current.job_dir == registration.job_dir
        and current.pgid == registration.pgid
        and current.process_start_signature
        == registration.process_start_signature
        and current.controller_pid == registration.controller_pid
        and current.controller_start_signature
        == registration.controller_start_signature
        for current in registered_jobs(PROJECT_ROOT, output_subdir)
    )


def _registered_cleanup_state(
    registration: RegisteredJob,
    *,
    deadline_seconds: float = 0.0,
    on_gone: Callable[[], bool] | None = None,
) -> str:
    """Poll inherited-flock release and finalize while still holding the lock.

    Returning ``finalized`` means ``on_gone`` ran inside the same exclusive
    lock observation that proved the old token no longer has an inherited FD.
    ``superseded`` means a newer two-sided registration owns the job directory.
    """

    deadline = time.monotonic() + max(0.0, deadline_seconds)
    while True:
        with generation_lock_observation() as (lock_available, metadata):
            if lock_available:
                if on_gone is None:
                    return "gone"
                return "finalized" if on_gone() else "superseded"
            if not _registration_is_current(registration):
                return "superseded"
            state = (
                "signalable"
                if _registered_group_is_signalable(registration, metadata)
                else "ambiguous"
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return state
        time.sleep(min(0.05, remaining))


def _record_orphan_terminal(
    job_dir: Path,
    *,
    state: str,
    reason: str,
) -> None:
    current = read_json(job_dir / "process.json")
    current.update(
        {
            "state": state,
            "paused": False,
            "reason": reason,
            "updated_at": time.time(),
        }
    )
    atomic_json(job_dir / "process.json", current)


def _finalize_registered_locked(
    config: object,
    registration: RegisteredJob,
    *,
    state: str,
    reason: str,
) -> bool:
    """Record/remove only if the old token is still current under flock."""

    if not _registration_is_current(registration):
        return False
    _record_orphan_terminal(registration.job_dir, state=state, reason=reason)
    remove_registered_job(
        PROJECT_ROOT,
        str(getattr(config, "output_subdir")),
        registration.job_dir,
        pgid=registration.pgid,
        process_start_signature=registration.process_start_signature,
    )
    return True


def _state_after_denied_control(
    config: object,
    job_dir: Path,
    registration: RegisteredJob | None,
    *,
    pgid: int,
    expected_birth: str,
    stage: str,
) -> str:
    """Reclassify a denied signal without turning normal exit into failure."""

    if registration is not None:
        return _registered_cleanup_state(
            registration,
            deadline_seconds=1.5,
            on_gone=lambda: _finalize_registered_locked(
                config,
                registration,
                state=("orphan-stale" if stage == "CONT" else "orphan-terminated"),
                reason=f"process-exited-before-{stage.lower()}",
            ),
        )
    return _finalize_legacy_if_gone(
        job_dir,
        pgid=pgid,
        expected_birth=expected_birth,
        state=("orphan-stale" if stage == "CONT" else "orphan-terminated"),
        reason=f"process-exited-before-{stage.lower()}",
    )


def _job_dir_has_live_registry_claim(job_dir: Path) -> bool:
    marker = job_dir / ".h3-job-registry.json"
    if marker.is_symlink():
        return True
    value = read_json(marker)
    token = str(value.get("token", ""))
    if len(token) != 32 or any(character not in "0123456789abcdef" for character in token):
        return False
    entry = PROJECT_ROOT / "runtime" / "job-registry" / f"{token}.json"
    if entry.is_symlink():
        return True
    peer = read_json(entry)
    return (
        peer.get("token") == token
        and peer.get("state") in {"starting", "active", "cleanup-needed"}
        and str(peer.get("job_dir", "")) == str(job_dir)
        and value.get("token") == peer.get("token")
        and str(value.get("job_dir", "")) == str(peer.get("job_dir", ""))
    )


def _finalize_legacy_if_gone(
    job_dir: Path,
    *,
    pgid: int,
    expected_birth: str,
    state: str,
    reason: str,
) -> str:
    """Write a legacy terminal status only while excluding a new generation."""

    with publication_control_guard(PROJECT_ROOT):
        with _generation_lock_observation_guarded() as (lock_available, _metadata):
            if _job_dir_has_live_registry_claim(job_dir):
                return "superseded"
            if not lock_available:
                return "ambiguous"
            group_state = _original_group_state(pgid, expected_birth)
            if group_state not in {"gone", "reused"}:
                return group_state
            _record_orphan_terminal(job_dir, state=state, reason=reason)
            return "finalized"


def cleanup_orphans() -> int:
    cleaned = 0
    failed = False
    seen_identities: set[tuple[int, str]] = set()
    config = load_config()
    for job_dir, status in orphan_jobs():
        pgid = int(status["pgid"])
        expected_birth = str(status["process_start_signature"])
        identity = (pgid, expected_birth)
        if identity in seen_identities:
            continue
        seen_identities.add(identity)
        registration = _matching_registration(config, job_dir, status)
        registry_trusted = bool(status.get("_registry_trusted"))
        expected_token = str(status.get("_registration_token", ""))
        if registry_trusted and (
            registration is None or registration.token != expected_token
        ):
            print(
                f"{job_dir.name}: 注册已被新任务替代，跳过清理 / "
                "registry was superseded; cleanup skipped"
            )
            cleaned += 1
            continue
        if registration is not None:
            initial_state = _registered_cleanup_state(
                registration,
                on_gone=lambda: _finalize_registered_locked(
                    config,
                    registration,
                    state="orphan-stale",
                    reason="generation-lock-released",
                ),
            )
            if initial_state in {"finalized", "superseded"}:
                message = (
                    "stale registry removed without signalling"
                    if initial_state == "finalized"
                    else "stale cleanup skipped because a newer job owns this path"
                )
                print(f"{job_dir.name}: {message}")
                cleaned += 1
                continue
            if initial_state != "signalable":
                print(
                    f"{job_dir.name}: 无法安全确认孤儿进程身份，保留以便重试 / "
                    "orphan identity could not be proven safely",
                    file=sys.stderr,
                )
                failed = True
                continue
        if not _authorized_update_control(
            config,
            job_dir,
            status,
            registration,
            pgid=pgid,
            selected_signal=signal.SIGCONT,
            paused=False,
        ):
            denied_state = _state_after_denied_control(
                config,
                job_dir,
                registration,
                pgid=pgid,
                expected_birth=expected_birth,
                stage="CONT",
            )
            if denied_state in {"finalized", "superseded"}:
                cleaned += 1
            else:
                print(
                    f"{job_dir.name}: CONT 前任务身份无法安全确认，未发送信号 / "
                    "job identity could not be safely confirmed before CONT",
                    file=sys.stderr,
                )
                failed = True
            continue
        if not _authorized_update_control(
            config,
            job_dir,
            status,
            registration,
            pgid=pgid,
            selected_signal=signal.SIGTERM,
        ):
            denied_state = _state_after_denied_control(
                config,
                job_dir,
                registration,
                pgid=pgid,
                expected_birth=expected_birth,
                stage="TERM",
            )
            if denied_state in {"finalized", "superseded"}:
                cleaned += 1
            else:
                print(
                    f"{job_dir.name}: TERM 前任务身份无法安全确认，未发送信号 / "
                    "job identity could not be safely confirmed before TERM",
                    file=sys.stderr,
                )
                failed = True
            continue
        deadline = time.monotonic() + 3.0
        while process_group_alive(pgid) and time.monotonic() < deadline:
            time.sleep(0.1)
        if registration is not None:
            post_term_state = _registered_cleanup_state(
                registration,
                deadline_seconds=1.5,
                on_gone=lambda: _finalize_registered_locked(
                    config,
                    registration,
                    state="orphan-terminated",
                    reason="controller-exited",
                ),
            )
            if post_term_state in {"finalized", "superseded"}:
                print(f"{job_dir.name}: 已清理孤儿 H3 进程 / orphan H3 terminated")
                cleaned += 1
                continue
            should_kill = post_term_state == "signalable"
            ambiguous = post_term_state == "ambiguous"
        else:
            post_term_state = _original_group_state(pgid, expected_birth)
            should_kill = post_term_state in {"exact", "leaderless"}
            ambiguous = post_term_state == "ambiguous"
        if ambiguous:
            print(
                f"{job_dir.name}: TERM 后无法安全确认进程身份，保留以便重试 / "
                "process identity became ambiguous after TERM",
                file=sys.stderr,
            )
            failed = True
            continue
        if should_kill:
            if not _authorized_update_control(
                config,
                job_dir,
                status,
                registration,
                pgid=pgid,
                selected_signal=signal.SIGKILL,
            ):
                denied_state = _state_after_denied_control(
                    config,
                    job_dir,
                    registration,
                    pgid=pgid,
                    expected_birth=expected_birth,
                    stage="KILL",
                )
                if denied_state in {"finalized", "superseded"}:
                    cleaned += 1
                else:
                    print(
                        f"{job_dir.name}: KILL 前任务身份无法安全确认，未发送信号 / "
                        "job identity could not be safely confirmed before KILL",
                        file=sys.stderr,
                    )
                    failed = True
                continue
            kill_deadline = time.monotonic() + 1.0
            while process_group_alive(pgid) and time.monotonic() < kill_deadline:
                time.sleep(0.05)
        if registration is not None:
            post_kill_state = _registered_cleanup_state(
                registration,
                deadline_seconds=2.0,
                on_gone=lambda: _finalize_registered_locked(
                    config,
                    registration,
                    state="orphan-terminated",
                    reason="controller-exited",
                ),
            )
            if post_kill_state in {"finalized", "superseded"}:
                print(f"{job_dir.name}: 已清理孤儿 H3 进程 / orphan H3 terminated")
                cleaned += 1
                continue
            exit_unverified = True
        else:
            post_kill_state = _finalize_legacy_if_gone(
                job_dir,
                pgid=pgid,
                expected_birth=expected_birth,
                state="orphan-terminated",
                reason="controller-exited",
            )
            if post_kill_state in {"finalized", "superseded"}:
                print(f"{job_dir.name}: 已清理孤儿 H3 进程 / orphan H3 terminated")
                cleaned += 1
                continue
            exit_unverified = True
        if exit_unverified:
            print(
                f"{job_dir.name}: 无法确认孤儿进程已退出，保留运行状态 / "
                "orphan exit could not be verified",
                file=sys.stderr,
            )
            failed = True
            continue
    return -1 if failed else cleaned


def _legacy_control_authorized(
    config: object,
    status: dict[str, object],
) -> bool:
    try:
        pgid = int(status.get("pgid", 0))
        status_age = time.time() - float(status.get("updated_at", 0))
    except (TypeError, ValueError):
        return False
    if pgid <= 1 or not process_group_alive(pgid):
        return False
    expected_birth = str(status.get("process_start_signature", ""))
    if expected_birth:
        if process_start_signature(pgid) != expected_birth:
            return False
    else:
        freshness_limit = max(
            20.0,
            float(getattr(config, "auto_metrics_poll_seconds")) * 3.0 + 10.0,
        )
        if not (-5 <= status_age <= freshness_limit):
            return False
    return process_matches_h3(pgid, Path(getattr(config, "h3_binary")))


def _active_job_candidates(
    config: object,
    selected_job: str = "",
) -> list[tuple[Path, dict[str, object], RegisteredJob | None]]:
    selected: list[tuple[Path, dict[str, object], RegisteredJob | None]] = []
    for job_dir, status, registry_trusted in _job_status_candidates(config):
        if selected_job and job_dir.name != selected_job:
            continue
        if bool(status.get("_registry_only")):
            continue
        if status.get("state") not in {"running", "paused"}:
            continue
        if registry_trusted:
            registration = _matching_registration(config, job_dir, status)
            if registration is None or not _registered_control_authorized(
                registration
            ):
                continue
            selected.append((job_dir, status, registration))
        elif _legacy_control_authorized(config, status):
            selected.append((job_dir, status, None))
    return selected


def active_jobs(selected_job: str = "") -> list[tuple[Path, dict[str, object]]]:
    config = load_config()
    return [
        (job_dir, status)
        for job_dir, status, _registration in _active_job_candidates(
            config, selected_job
        )
    ]


def describe(job_dir: Path, status: dict[str, object]) -> str:
    state = status.get("state", "unknown")
    policy = status.get("scheduler_policy", status.get("engine_profile", "unknown"))
    reason = status.get("reason", "unknown")
    progress = read_json(job_dir / "progress.json")
    current = progress.get("current", "?")
    total = progress.get("total", "?")
    diagnostics: list[str] = []
    if status.get("memory_free_percent") is not None:
        diagnostics.append(f"memory_free={status['memory_free_percent']}%")
    if status.get("swap_growth_mib_per_minute") is not None:
        diagnostics.append(f"swap_growth={status['swap_growth_mib_per_minute']}MiB/min")
    if status.get("thermal_state"):
        diagnostics.append(f"thermal={status['thermal_state']}")
    suffix = " | " + " | ".join(diagnostics) if diagnostics else ""
    return (
        f"{job_dir.name} | state={state} | policy={policy} | "
        f"reason={reason} | step={current}/{total}{suffix}"
    )


def update_control(
    job_dir: Path,
    *,
    pgid: int = 0,
    selected_signal: signal.Signals | None = None,
    **updates: object,
) -> bool:
    path = job_dir / "control.json"
    lock_path = job_dir / "control.lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        value = read_json(path)
        value.update(updates)
        value["control_generation"] = time.time_ns()
        value["updated_at"] = time.time()
        atomic_json(path, value)
        signalled = bool(
            selected_signal is not None
            and pgid > 1
            and signal_process_group(pgid, selected_signal)
        )
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return signalled


def _authorized_update_control(
    config: object,
    job_dir: Path,
    status: dict[str, object],
    registration: RegisteredJob | None,
    *,
    pgid: int,
    selected_signal: signal.Signals,
    **updates: object,
) -> bool:
    """Reauthorize and update/signal under one publication guard section."""

    with publication_control_guard(PROJECT_ROOT):
        if registration is not None:
            if not _registered_control_authorized_guarded(registration):
                return False
            return update_control(
                job_dir,
                pgid=pgid,
                selected_signal=selected_signal,
                **updates,
            )
        # Legacy jobs have no token that can bind a busy generation lock to
        # their status. Hold a free lock through revalidation, control.json,
        # and the signal so a new runner cannot publish in between.
        with _generation_lock_observation_guarded() as (lock_available, _metadata):
            if not lock_available or _job_dir_has_live_registry_claim(job_dir):
                return False
            if not _legacy_control_authorized(config, status):
                return False
            return update_control(
                job_dir,
                pgid=pgid,
                selected_signal=selected_signal,
                **updates,
            )


def control(action: str, selected_job: str = "") -> int:
    if action == "cleanup-orphans":
        return 2 if cleanup_orphans() < 0 else 0
    config = load_config()
    jobs = _active_job_candidates(config, selected_job)
    if not jobs:
        print("没有正在运行的 H3 任务。 / No active H3 jobs.")
        return 1 if selected_job else 0

    if action == "status":
        for job_dir, status, _registration in jobs:
            print(describe(job_dir, status))
        return 0

    controlled = 0
    for job_dir, status, registration in jobs:
        try:
            pgid = int(status.get("pgid", 0))
        except (TypeError, ValueError):
            pgid = 0
        if action == "pause":
            selected_signal = signal.SIGSTOP
            updates = {"paused": True}
            message = "已发送暂停请求 / pause requested"
        elif action == "resume":
            selected_signal = signal.SIGCONT
            updates = {"paused": False}
            message = "已请求继续（仍遵循当前策略） / resume requested"
        elif action in {"low", "auto", "max"}:
            selected_signal = signal.SIGCONT
            updates = {
                "paused": False,
                "policy": action,
            }
            message = f"已请求切换为 {action} 并继续 / switch requested"
        else:
            raise ValueError(f"Unsupported action: {action}")
        if not _authorized_update_control(
            config,
            job_dir,
            status,
            registration,
            pgid=pgid,
            selected_signal=selected_signal,
            **updates,
        ):
            print(
                f"{job_dir.name}: 任务身份已变化，未发送控制信号 / "
                "job identity changed; control was not sent",
                file=sys.stderr,
            )
            continue
        print(f"{job_dir.name}: {message}")
        controlled += 1
    return 0 if controlled else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pause, resume, inspect, or reschedule active ComfyUI-H3-Mac jobs."
    )
    parser.add_argument(
        "action",
        choices=["status", "pause", "resume", "low", "auto", "max", "cleanup-orphans"],
    )
    parser.add_argument("--job", default="", help="Operate on one job ID instead of all active jobs.")
    args = parser.parse_args()
    return control(args.action, args.job)


if __name__ == "__main__":
    raise SystemExit(main())
