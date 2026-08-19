#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import signal
import subprocess
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from h3_bridge.config import load_config  # noqa: E402
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


def active_jobs(selected_job: str = "") -> list[tuple[Path, dict[str, object]]]:
    config = load_config()
    root = PROJECT_ROOT / "runtime" / "ComfyUI" / "output" / config.output_subdir
    if not root.is_dir():
        return []
    selected: list[tuple[Path, dict[str, object]]] = []
    for status_path in sorted(root.glob("*/process.json")):
        if selected_job and status_path.parent.name != selected_job:
            continue
        status = read_json(status_path)
        if status.get("state") not in {"running", "paused"}:
            continue
        try:
            status_age = time.time() - float(status.get("updated_at", 0))
        except (TypeError, ValueError):
            continue
        try:
            pgid = int(status.get("pgid", 0))
        except (TypeError, ValueError):
            continue
        expected_start = str(status.get("process_start_signature", ""))
        same_process = bool(expected_start) and (
            process_start_signature(pgid) == expected_start
        )
        freshness_limit = max(20.0, config.auto_metrics_poll_seconds * 3.0 + 10.0)
        legacy_fresh = not expected_start and -5 <= status_age <= freshness_limit
        if pgid > 1 and (same_process or legacy_fresh) and process_group_alive(
            pgid
        ) and process_matches_h3(pgid, config.h3_binary):
            selected.append((status_path.parent, status))
    return selected


def describe(job_dir: Path, status: dict[str, object]) -> str:
    state = status.get("state", "unknown")
    policy = status.get("scheduler_policy", status.get("engine_profile", "unknown"))
    reason = status.get("reason", "unknown")
    progress = read_json(job_dir / "progress.json")
    current = progress.get("current", "?")
    total = progress.get("total", "?")
    return (
        f"{job_dir.name} | state={state} | policy={policy} | "
        f"reason={reason} | step={current}/{total}"
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


def control(action: str, selected_job: str = "") -> int:
    jobs = active_jobs(selected_job)
    if not jobs:
        print("没有正在运行的 H3 任务。 / No active H3 jobs.")
        return 1 if selected_job else 0

    if action == "status":
        for job_dir, status in jobs:
            print(describe(job_dir, status))
        return 0

    for job_dir, status in jobs:
        try:
            pgid = int(status.get("pgid", 0))
        except (TypeError, ValueError):
            pgid = 0
        if action == "pause":
            update_control(
                job_dir,
                pgid=pgid,
                selected_signal=signal.SIGSTOP,
                paused=True,
            )
            message = "已发送暂停请求 / pause requested"
        elif action == "resume":
            update_control(
                job_dir,
                pgid=pgid,
                selected_signal=signal.SIGCONT,
                paused=False,
            )
            message = "已请求继续（仍遵循当前策略） / resume requested"
        elif action in {"low", "auto", "max"}:
            update_control(
                job_dir,
                pgid=pgid,
                selected_signal=signal.SIGCONT,
                paused=False,
                policy=action,
            )
            message = f"已请求切换为 {action} 并继续 / switch requested"
        else:
            raise ValueError(f"Unsupported action: {action}")
        print(f"{job_dir.name}: {message}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pause, resume, inspect, or reschedule active ComfyUI-H3-Mac jobs."
    )
    parser.add_argument("action", choices=["status", "pause", "resume", "low", "auto", "max"])
    parser.add_argument("--job", default="", help="Operate on one job ID instead of all active jobs.")
    args = parser.parse_args()
    return control(args.action, args.job)


if __name__ == "__main__":
    raise SystemExit(main())
