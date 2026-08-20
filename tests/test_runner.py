from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from h3_bridge.config import BridgeConfig
from h3_bridge.job_registry import registered_jobs
from h3_bridge.models import H3Reference, H3Request
from h3_bridge.profiles import QUALITY_PROFILES
from h3_bridge.runner import H3Runner, _terminate_process_group


def make_runner(tmp_path: Path) -> H3Runner:
    fake = Path(__file__).with_name("fake_h3.py")
    engine = tmp_path / "engine"
    engine.mkdir()
    binary = engine / "h3"
    binary.write_bytes(fake.read_bytes())
    binary.chmod(0o755)
    (engine / "h3_shaders.metal").write_text("// test shader\n", encoding="utf-8")
    model_root = tmp_path / "models"
    (model_root / "FL2VA").mkdir(parents=True)
    (model_root / "Ref2VA").mkdir(parents=True)
    config = BridgeConfig(
        project_root=tmp_path,
        h3_binary=binary,
        model_root=model_root,
        allow_unmanaged_model=True,
        output_subdir="h3-jobs",
    )
    return H3Runner(config)


def test_command_is_an_argument_vector_and_preserves_reference_order(tmp_path: Path):
    runner = make_runner(tmp_path)
    first = tmp_path / "cat.png"
    second = tmp_path / "stream.png"
    first.write_bytes(b"cat")
    second.write_bytes(b"stream")
    request = H3Request(
        prompt="cat; touch /tmp/never-run",
        resource_profile="max",
        references=(H3Reference("image", first), H3Reference("image", second)),
    )
    command = runner.build_command(request, tmp_path / "out.mp4")
    assert request.prompt in command
    positions = [index for index, item in enumerate(command) if item == "--ref-image"]
    assert command[positions[0] + 1] == str(first)
    assert command[positions[1] + 1] == str(second)
    assert command[command.index("/usr/bin/caffeinate") + 1] == "-s"


def test_cache_digest_includes_resolved_quality_and_streaming(tmp_path: Path):
    runner = make_runner(tmp_path)
    request = H3Request(prompt="cat", resource_profile="auto", quality_profile="quality")

    with patch("h3_bridge.profiles.physical_ram_gib", return_value=48):
        streaming_digest = runner._request_digest(request)
        streaming_command = runner.build_command(request, tmp_path / "stream.mp4")
    with patch("h3_bridge.profiles.physical_ram_gib", return_value=96):
        resident_digest = runner._request_digest(request)
        resident_command = runner.build_command(request, tmp_path / "resident.mp4")

    assert streaming_digest != resident_digest
    assert "--ssd-streaming" in streaming_command
    assert "--ssd-streaming" not in resident_command

    changed_quality = replace(QUALITY_PROFILES["quality"], steps=21)
    with patch.dict(QUALITY_PROFILES, {"quality": changed_quality}), patch(
        "h3_bridge.profiles.physical_ram_gib", return_value=96
    ):
        changed_quality_digest = runner._request_digest(request)
    assert changed_quality_digest != resident_digest


@patch(
    "h3_bridge.runner.os.uname",
    return_value=SimpleNamespace(sysname="Darwin", machine="arm64"),
)
def test_runner_persists_job_and_reuses_completed_result(_uname, tmp_path: Path):
    runner = make_runner(tmp_path)
    request = H3Request(prompt="a cat playing in water", resource_profile="max")
    updates: list[tuple[int, int]] = []
    real_popen = subprocess.Popen
    inherited_lock_fds: list[tuple[int, ...]] = []

    def launch(*args, **kwargs):
        inherited_lock_fds.append(tuple(kwargs.get("pass_fds", ())))
        return real_popen(*args, **kwargs)

    with patch("h3_bridge.runner.shutil.which", return_value="/usr/bin/true"), patch(
        "h3_bridge.runner.subprocess.Popen", side_effect=launch
    ):
        first = runner.run(
            request,
            tmp_path / "output",
            progress=lambda current, total, _line: updates.append((current, total)),
        )
    assert first.output_path.read_bytes() == b"fake-mp4-for-tests"
    assert (first.job_dir / "request.json").is_file()
    assert (first.job_dir / "engine.log").is_file()
    assert (first.job_dir / "control.json").is_file()
    process_status = json.loads((first.job_dir / "process.json").read_text(encoding="utf-8"))
    assert process_status["state"] == "completed"
    assert process_status["engine_profile"] == "max"
    assert updates[-1] == (20, 20)
    inherited = [fds for fds in inherited_lock_fds if fds]
    # The child receives the generation lock plus gate and acknowledgement
    # descriptors for the activation handshake.
    assert inherited and len(inherited[0]) == 3
    assert inherited[0][0] >= 0
    assert list((tmp_path / "runtime" / "job-registry").glob("*.json")) == []
    assert not (first.job_dir / ".h3-job-registry.json").exists()

    with patch("h3_bridge.runner.shutil.which", return_value="/usr/bin/true"), patch.object(
        runner,
        "_generation_lock",
        side_effect=AssertionError("cache hit must not wait for the H3 lock"),
    ):
        second = runner.run(request, tmp_path / "output", reuse_completed=True)
    assert second.output_path == first.output_path
    assert second.elapsed_seconds == 0.0


@patch(
    "h3_bridge.runner.os.uname",
    return_value=SimpleNamespace(sysname="Darwin", machine="arm64"),
)
def test_scheduler_cannot_stop_launcher_before_registry_activation(
    _uname, tmp_path: Path
):
    runner = make_runner(tmp_path)
    request = H3Request(prompt="activation handshake", resource_profile="max")
    from h3_bridge.scheduler import AdaptiveScheduler

    real_start = AdaptiveScheduler.start
    observed: list[int] = []

    def stop_immediately_after_start_is_invoked(scheduler: AdaptiveScheduler) -> None:
        os.killpg(scheduler.pgid, signal.SIGSTOP)
        try:
            selected = list(registered_jobs(tmp_path, "h3-jobs"))
            assert len(selected) == 1
            assert selected[0].pgid == scheduler.pgid
            observed.append(scheduler.pgid)
        finally:
            os.killpg(scheduler.pgid, signal.SIGCONT)
        real_start(scheduler)

    with patch("h3_bridge.runner.shutil.which", return_value="/usr/bin/true"), patch(
        "h3_bridge.runner.AdaptiveScheduler.start",
        new=stop_immediately_after_start_is_invoked,
    ):
        result = runner.run(request, tmp_path / "output")

    assert result.output_path.is_file()
    assert observed


@patch(
    "h3_bridge.runner.os.uname",
    return_value=SimpleNamespace(sysname="Darwin", machine="arm64"),
)
def test_references_cannot_mix_with_frame_anchors(_uname, tmp_path: Path):
    runner = make_runner(tmp_path)
    image = tmp_path / "cat.png"
    image.write_bytes(b"cat")
    request = H3Request(
        prompt="cat",
        resource_profile="max",
        references=(H3Reference("image", image),),
        first_frame=image,
    )
    with patch("h3_bridge.runner.shutil.which", return_value="/usr/bin/true"):
        try:
            runner.validate(request)
        except ValueError as exc:
            assert "cannot be combined" in str(exc)
        else:
            raise AssertionError("Expected mixed conditioning modes to be rejected")


@patch(
    "h3_bridge.runner.os.uname",
    return_value=SimpleNamespace(sysname="Darwin", machine="arm64"),
)
@patch("h3_bridge.runner._physical_memory_gib", return_value=48.0)
def test_runner_enforces_h3_canvas_frame_and_low_memory_duration_limits(
    _memory, _uname, tmp_path: Path
):
    runner = make_runner(tmp_path)
    invalid = [
        (H3Request(prompt="cat", width=648), "multiples of 32"),
        (H3Request(prompt="cat", width=1344, height=800), "Canvas area"),
        (H3Request(prompt="cat", seconds=5 / 24), "22..362"),
        (H3Request(prompt="cat", seconds=21 / 24), "22..362"),
        (H3Request(prompt="cat", fps=60), "fixed 24 fps"),
        (H3Request(prompt="cat", seconds=5.5), "less than 64 GiB"),
        (H3Request(prompt="cat", seconds=15.5), "22..362"),
    ]
    with patch("h3_bridge.runner.shutil.which", return_value="/usr/bin/true"):
        for request, expected in invalid:
            with pytest.raises(ValueError, match=expected):
                runner.validate(request)


@patch(
    "h3_bridge.runner.os.uname",
    return_value=SimpleNamespace(sysname="Darwin", machine="arm64"),
)
@patch("h3_bridge.runner._physical_memory_gib", return_value=48.0)
def test_large_job_override_keeps_h3_hard_limit(_memory, _uname, tmp_path: Path):
    runner = make_runner(tmp_path)
    with patch("h3_bridge.runner.shutil.which", return_value="/usr/bin/true"), patch.dict(
        "os.environ", {"H3_ALLOW_LARGE_JOB": "1"}
    ):
        runner.validate(H3Request(prompt="cat", seconds=15.0))
        with pytest.raises(ValueError, match="22..362"):
            runner.validate(H3Request(prompt="cat", seconds=15.5))


@patch(
    "h3_bridge.runner.os.uname",
    return_value=SimpleNamespace(sysname="Darwin", machine="arm64"),
)
def test_runner_requires_shader_and_managed_manifest_in_production(_uname, tmp_path: Path):
    runner = make_runner(tmp_path)
    (runner.config.h3_binary.parent / "h3_shaders.metal").unlink()
    with patch("h3_bridge.runner.shutil.which", return_value="/usr/bin/true"):
        with pytest.raises(FileNotFoundError, match="shader source"):
            runner.validate(H3Request(prompt="cat"))

    (runner.config.h3_binary.parent / "h3_shaders.metal").write_text("// test\n")
    managed_runner = H3Runner(replace(runner.config, allow_unmanaged_model=False))
    with patch("h3_bridge.runner.shutil.which", return_value="/usr/bin/true"):
        with pytest.raises(RuntimeError, match="manifest is missing"):
            managed_runner.validate(H3Request(prompt="cat"))


@patch(
    "h3_bridge.runner.os.uname",
    return_value=SimpleNamespace(sysname="Darwin", machine="arm64"),
)
def test_carriage_return_progress_does_not_block_cancel(_uname, tmp_path: Path):
    runner = make_runner(tmp_path)
    request = H3Request(
        prompt="cr-progress-wait",
        resource_profile="max",
        quality_profile="preview",
    )
    started = time.monotonic()
    with patch("h3_bridge.runner.shutil.which", return_value="/usr/bin/true"):
        with pytest.raises(InterruptedError, match="cancelled"):
            runner.run(
                request,
                tmp_path / "custom-output",
                cancelled=lambda: time.monotonic() - started > 0.6,
            )
    assert time.monotonic() - started < 3.0
    jobs = list((tmp_path / "custom-output" / "h3-jobs").iterdir())
    assert len(jobs) == 1
    assert "sample 1/4" in (jobs[0] / "engine.log").read_text(encoding="utf-8")
    progress = json.loads((jobs[0] / "progress.json").read_text(encoding="utf-8"))
    assert progress["current"] == 1


def test_generation_lock_rejects_a_second_comfy_instance(tmp_path: Path):
    runner = make_runner(tmp_path)
    lock_path = tmp_path / "runtime" / "h3-generation.lock"
    lock_path.parent.mkdir(parents=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="Another H3 generation"):
            with runner._generation_lock():
                raise AssertionError("unreachable")


def test_termination_reports_leader_or_child_group_residue():
    process = SimpleNamespace(
        pid=4242,
        wait=Mock(
            side_effect=[
                subprocess.TimeoutExpired("h3", 10),
                subprocess.TimeoutExpired("h3", 2),
            ]
        ),
    )
    signals: list[signal.Signals] = []
    with patch(
        "h3_bridge.runner._original_process_group_alive", return_value=True
    ), patch(
        "h3_bridge.runner._wait_for_original_group_exit", return_value=False
    ), patch(
        "h3_bridge.runner.os.killpg",
        side_effect=lambda _pgid, selected: signals.append(selected),
    ):
        assert not _terminate_process_group(process, "engine-birth")
    assert signals == [signal.SIGCONT, signal.SIGTERM, signal.SIGKILL]


@patch(
    "h3_bridge.runner.os.uname",
    return_value=SimpleNamespace(sysname="Darwin", machine="arm64"),
)
def test_ref2va_requires_a_completed_manifest_task(_uname, tmp_path: Path):
    runner = make_runner(tmp_path)
    base_file = runner.config.model_root / "FL2VA" / "base.bin"
    base_file.write_bytes(b"base")
    manifest = runner.config.model_root.parent / "models.manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "repo_id": "MiniMaxAI/MiniMax-H3",
                "revision": "pinned",
                "installed_tasks": ["FL2VA"],
                "storage": "legacy-local-directory",
                "files": [
                    {
                        "path": "FL2VA/base.bin",
                        "size": 4,
                        "blob_key": "base",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="Ref2VA is not completely installed"):
        runner.validate(H3Request(prompt="cat", task="Ref2VA"))

    revision_runner = H3Runner(
        replace(runner.config, expected_model_revision="expected-revision")
    )
    with pytest.raises(RuntimeError, match="Model revision mismatch"):
        revision_runner.validate(H3Request(prompt="cat", task="FL2VA"))
