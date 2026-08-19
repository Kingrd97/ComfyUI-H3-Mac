import ast
import json
import os
import platform
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts import h3_control
from h3_bridge.scheduler import process_group_stopped


ROOT = Path(__file__).resolve().parents[1]


def test_every_shell_script_parses():
    scripts = [
        *sorted(ROOT.glob("*.command")),
        *sorted((ROOT / "scripts").glob("*.sh")),
    ]

    for script in scripts:
        subprocess.run(["bash", "-n", str(script)], check=True)


def test_h3_control_uses_only_the_locked_shared_signal_helper():
    """Direct controls are serialized; raw killpg calls remain forbidden."""

    control_path = ROOT / "scripts" / "h3_control.py"
    tree = ast.parse(control_path.read_text(encoding="utf-8"), filename=str(control_path))
    shared_signal_calls = 0

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "signal_process_group":
                shared_signal_calls += 1
            if isinstance(node.func, ast.Attribute) and node.func.attr == "killpg":
                raise AssertionError("h3_control must use the shared signal helper")

    assert shared_signal_calls == 1


def test_pause_serializes_intent_and_immediate_stop(tmp_path: Path):
    updates: list[dict[str, object]] = []
    status = {"pgid": 4242, "state": "running"}
    with patch.object(h3_control, "active_jobs", return_value=[(tmp_path, status)]), patch.object(
        h3_control,
        "update_control",
        side_effect=lambda _job, **values: updates.append(values),
    ):
        assert h3_control.control("pause") == 0

    assert updates == [
        {"pgid": 4242, "selected_signal": signal.SIGSTOP, "paused": True}
    ]


def test_process_match_searches_whole_group_for_h3(tmp_path: Path):
    binary = tmp_path / "h3"
    output = f"/usr/bin/caffeinate -i\n{binary} --prompt cat\n"
    completed = subprocess.CompletedProcess([], 0, stdout=output, stderr="")
    with patch.object(h3_control.subprocess, "run", return_value=completed):
        assert h3_control.process_matches_h3(4242, binary)


def test_control_updates_are_serialized_without_lost_fields(tmp_path: Path):
    (tmp_path / "control.json").write_text(
        json.dumps({"paused": False, "policy": "auto"}),
        encoding="utf-8",
    )
    ready = threading.Barrier(3)

    def update(**values: object) -> None:
        ready.wait()
        h3_control.update_control(tmp_path, **values)

    first = threading.Thread(target=update, kwargs={"paused": True})
    second = threading.Thread(target=update, kwargs={"policy": "low"})
    first.start()
    second.start()
    ready.wait()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    value = json.loads((tmp_path / "control.json").read_text(encoding="utf-8"))
    assert value["paused"] is True
    assert value["policy"] == "low"


def write_job_status(tmp_path: Path, **overrides: object) -> Path:
    job_dir = tmp_path / "runtime" / "ComfyUI" / "output" / "h3-jobs" / "job"
    job_dir.mkdir(parents=True)
    value: dict[str, object] = {
        "pgid": 4242,
        "state": "running",
        "updated_at": time.time(),
        "process_start_signature": "expected-birth",
    }
    value.update(overrides)
    (job_dir / "process.json").write_text(json.dumps(value), encoding="utf-8")
    return job_dir


def test_active_jobs_rejects_reused_pid_with_birth_mismatch(tmp_path: Path):
    write_job_status(tmp_path)
    selected_config = SimpleNamespace(
        output_subdir="h3-jobs",
        h3_binary=tmp_path / "h3",
        auto_metrics_poll_seconds=2.0,
    )
    with patch.object(h3_control, "PROJECT_ROOT", tmp_path), patch.object(
        h3_control, "load_config", return_value=selected_config
    ), patch.object(
        h3_control, "process_start_signature", return_value="different-birth"
    ), patch.object(h3_control, "process_group_alive", return_value=True), patch.object(
        h3_control, "process_matches_h3", return_value=True
    ):
        assert h3_control.active_jobs() == []


def test_active_jobs_accepts_stale_status_with_matching_birth(tmp_path: Path):
    job_dir = write_job_status(tmp_path, updated_at=0)
    selected_config = SimpleNamespace(
        output_subdir="h3-jobs",
        h3_binary=tmp_path / "h3",
        auto_metrics_poll_seconds=2.0,
    )
    with patch.object(h3_control, "PROJECT_ROOT", tmp_path), patch.object(
        h3_control, "load_config", return_value=selected_config
    ), patch.object(
        h3_control, "process_start_signature", return_value="expected-birth"
    ), patch.object(h3_control, "process_group_alive", return_value=True), patch.object(
        h3_control, "process_matches_h3", return_value=True
    ):
        selected = h3_control.active_jobs()

    assert selected[0][0] == job_dir


def test_locked_cli_control_really_stops_and_resumes_group(tmp_path: Path):
    if platform.system() != "Darwin":
        return
    (tmp_path / "control.json").write_text(
        json.dumps({"paused": False, "policy": "max"}),
        encoding="utf-8",
    )
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    try:
        assert h3_control.update_control(
            tmp_path,
            pgid=child.pid,
            selected_signal=signal.SIGSTOP,
            paused=True,
        )
        if process_group_stopped(child.pid) is None:
            pytest.skip("sandbox does not allow ps process-state inspection")
        for _ in range(100):
            if process_group_stopped(child.pid) is True:
                break
            time.sleep(0.01)
        assert process_group_stopped(child.pid) is True

        assert h3_control.update_control(
            tmp_path,
            pgid=child.pid,
            selected_signal=signal.SIGCONT,
            paused=False,
        )
        for _ in range(100):
            if process_group_stopped(child.pid) is False:
                break
            time.sleep(0.01)
        assert process_group_stopped(child.pid) is False
    finally:
        os.killpg(child.pid, signal.SIGCONT)
        os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=5)
