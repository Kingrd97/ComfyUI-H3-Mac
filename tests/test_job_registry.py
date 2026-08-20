from __future__ import annotations

import fcntl
import os
import platform
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from h3_bridge.job_registry import (
    activate_job,
    finish_job,
    mark_cleanup_needed,
    register_starting_job,
    registered_jobs,
)
from h3_bridge.scheduler import process_start_signature


ROOT = Path(__file__).resolve().parents[1]


def test_child_registers_identity_before_process_status_and_retains_lock(tmp_path: Path):
    if platform.system() != "Darwin":
        pytest.skip("process birth fingerprints are a macOS contract")
    job_id = "a" * 20
    job_dir = (tmp_path / "custom-output" / "h3-jobs" / job_id).resolve()
    job_dir.mkdir(parents=True)
    controller_pid = os.getpid()
    registration = register_starting_job(
        tmp_path,
        job_dir,
        job_id,
        "h3-jobs",
        controller_pid=controller_pid,
        controller_start_signature=process_start_signature(controller_pid),
    )
    lock_path = tmp_path / "runtime" / "h3-generation.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = lock_path.open("a+")
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    gate_read, gate_write = os.pipe()
    ack_read, ack_write = os.pipe()
    child = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "h3_bridge" / "h3_launch.py"),
            "--project-root",
            str(tmp_path),
            "--registry",
            str(registration.entry_path),
            "--token",
            registration.token,
            "--gate-fd",
            str(gate_read),
            "--ack-fd",
            str(ack_write),
            "--",
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
        ],
        start_new_session=True,
        pass_fds=(lock.fileno(), gate_read, ack_write),
    )
    os.close(gate_read)
    os.close(ack_write)
    os.write(gate_write, b"G")
    os.close(gate_write)
    assert os.read(ack_read, 1) == b"A"
    os.close(ack_read)
    lock.close()
    try:
        deadline = time.monotonic() + 3
        registered = []
        while time.monotonic() < deadline:
            registered = list(registered_jobs(tmp_path, "h3-jobs"))
            if registered:
                break
            time.sleep(0.02)
        assert len(registered) == 1
        assert registered[0].pgid == child.pid
        assert not (job_dir / "process.json").exists()

        competitor = lock_path.open("a+")
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(competitor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            competitor.close()
    finally:
        os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=5)
        finish_job(registration, "test-cleanup")

    competitor = lock_path.open("a+")
    try:
        fcntl.flock(competitor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        competitor.close()


def test_gated_child_exits_on_parent_eof_without_starting_engine(tmp_path: Path):
    if platform.system() != "Darwin":
        pytest.skip("process birth fingerprints are a macOS contract")
    job_id = "e" * 20
    job_dir = (tmp_path / "custom-output" / "h3-jobs" / job_id).resolve()
    job_dir.mkdir(parents=True)
    registration = register_starting_job(
        tmp_path,
        job_dir,
        job_id,
        "h3-jobs",
        controller_pid=os.getpid(),
        controller_start_signature=process_start_signature(os.getpid()),
    )
    lock_path = tmp_path / "runtime" / "h3-generation.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    marker = tmp_path / "engine-started"
    lock = lock_path.open("a+")
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    gate_read, gate_write = os.pipe()
    ack_read, ack_write = os.pipe()
    child = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "h3_bridge" / "h3_launch.py"),
            "--project-root",
            str(tmp_path),
            "--registry",
            str(registration.entry_path),
            "--token",
            registration.token,
            "--gate-fd",
            str(gate_read),
            "--ack-fd",
            str(ack_write),
            "--",
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).touch()",
        ],
        start_new_session=True,
        pass_fds=(lock.fileno(), gate_read, ack_write),
    )
    os.close(gate_read)
    os.close(ack_write)
    os.close(gate_write)
    lock.close()
    assert child.wait(timeout=5) == 0
    assert os.read(ack_read, 1) == b""
    os.close(ack_read)
    assert not marker.exists()
    assert not registration.entry_path.exists()
    assert list(registered_jobs(tmp_path, "h3-jobs")) == []

    competitor = lock_path.open("a+")
    try:
        fcntl.flock(competitor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        competitor.close()


def test_registry_rejects_noncanonical_injected_job_path(tmp_path: Path):
    registry = tmp_path / "runtime" / "job-registry"
    registry.mkdir(parents=True)
    token = "b" * 32
    (registry / f"{token}.json").write_text(
        """{
          "schema_version": 1,
          "token": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
          "job_id": "aaaaaaaaaaaaaaaaaaaa",
          "job_dir": "../../injected/h3-jobs/aaaaaaaaaaaaaaaaaaaa",
          "output_subdir": "h3-jobs",
          "state": "active",
          "pgid": 4242,
          "process_start_signature": "engine",
          "controller_pid": 777,
          "controller_start_signature": "controller"
        }""",
        encoding="utf-8",
    )
    assert list(registered_jobs(tmp_path, "h3-jobs")) == []


def test_cleanup_needed_entry_remains_discoverable(tmp_path: Path):
    job_id = "d" * 20
    job_dir = (tmp_path / "custom" / "h3-jobs" / job_id).resolve()
    job_dir.mkdir(parents=True)
    registration = register_starting_job(
        tmp_path,
        job_dir,
        job_id,
        "h3-jobs",
        controller_pid=777,
        controller_start_signature="controller-birth",
    )
    activate_job(
        tmp_path,
        registration.entry_path,
        registration.token,
        pgid=4242,
        process_start_signature="engine-birth",
    )
    mark_cleanup_needed(registration, "child still holds generation lock")
    try:
        selected = list(registered_jobs(tmp_path, "h3-jobs"))
        assert len(selected) == 1
        assert selected[0].registry_state == "cleanup-needed"
    finally:
        finish_job(registration, "test-cleanup")
