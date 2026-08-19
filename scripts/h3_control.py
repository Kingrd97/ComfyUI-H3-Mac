#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from h3_bridge.config import load_config  # noqa: E402
from h3_bridge.scheduler import (  # noqa: E402
    process_group_alive,
    read_json,
    set_process_group_background,
    signal_process_group,
)


def atomic_json(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def jobs_root() -> Path:
    config = load_config()
    return PROJECT_ROOT / "runtime" / "ComfyUI" / "output" / config.output_subdir


def active_jobs(selected_job: str = "") -> list[tuple[Path, dict[str, object]]]:
    root = jobs_root()
    if not root.is_dir():
        return []
    selected: list[tuple[Path, dict[str, object]]] = []
    for status_path in sorted(root.glob("*/process.json")):
        if selected_job and status_path.parent.name != selected_job:
            continue
        status = read_json(status_path)
        try:
            pgid = int(status.get("pgid", 0))
        except (TypeError, ValueError):
            continue
        if pgid > 1 and process_group_alive(pgid):
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


def update_control(job_dir: Path, **updates: object) -> None:
    path = job_dir / "control.json"
    value = read_json(path)
    value.update(updates)
    value["updated_at"] = time.time()
    atomic_json(path, value)


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
        pgid = int(status["pgid"])
        if action == "pause":
            update_control(job_dir, paused=True)
            signal_process_group(pgid, signal.SIGSTOP)
            message = "已暂停 / paused"
        elif action == "resume":
            update_control(job_dir, paused=False)
            signal_process_group(pgid, signal.SIGCONT)
            message = "已继续（仍遵循当前策略） / resumed"
        elif action in {"low", "auto", "max"}:
            update_control(job_dir, paused=False, policy=action)
            signal_process_group(pgid, signal.SIGCONT)
            set_process_group_background(pgid, action != "max")
            message = f"已切换为 {action} 并继续 / switched to {action}"
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
