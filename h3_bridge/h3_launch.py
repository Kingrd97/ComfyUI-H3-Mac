#!/usr/bin/env python3
"""Register the child identity, then replace this process with the H3 command."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from h3_bridge.job_registry import abandon_starting_job, activate_job
from h3_bridge.scheduler import process_start_signature


def stable_process_start_signature(pid: int, attempts: int = 5) -> str:
    for attempt in range(max(1, attempts)):
        signature = process_start_signature(pid)
        if signature:
            return signature
        if attempt + 1 < attempts:
            time.sleep(0.05)
    return ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--gate-fd", type=int, required=True)
    parser.add_argument("--ack-fd", type=int, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        parser.error("missing H3 command after --")
    try:
        go = os.read(args.gate_fd, 1)
    finally:
        os.close(args.gate_fd)
    if go != b"G":
        os.close(args.ack_fd)
        abandon_starting_job(
            args.project_root,
            args.registry,
            args.token,
        )
        return 0
    pgid = os.getpid()
    signature = stable_process_start_signature(pgid)
    activate_job(
        args.project_root,
        args.registry,
        args.token,
        pgid=pgid,
        process_start_signature=signature,
    )
    os.write(args.ack_fd, b"A")
    os.close(args.ack_fd)
    os.execvp(command[0], command)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
