from __future__ import annotations

import fcntl
import hashlib
import json
import platform
import subprocess
import threading
import time
from pathlib import Path

import pytest

from h3_bridge.config import BridgeConfig
from h3_bridge.scheduler import ResourceHealth
from h3_bridge.vpipe import VPipeConfig, VPipeRequest, VPipeRunner
from h3_bridge.vpipe_worker import VPipeWorker, _PausedBeforeLaunch


def make_fake_vpipe(tmp_path: Path) -> tuple[Path, Path]:
    work_dir = tmp_path / "vpipe-work"
    work_dir.mkdir()
    binary = tmp_path / "fake-vpipe"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys, time\n"
        "p = pathlib.Path(sys.argv[sys.argv.index('--launch') + 1])\n"
        "graph = json.loads(p.read_text())\n"
        "print(\"[INFO] ImageResampleStage('first-frame'): ready\", flush=True)\n"
        "print(\"[NORMAL] [h3-dit] first forward at 123 rows\", flush=True)\n"
        "save = next(x for x in graph['stages'] if x['id'] == 'save-video')\n"
        "pathlib.Path(save['config']['output_url']).write_bytes(b'worker-mp4')\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    return binary, work_dir


def worker_config(binary: Path, work_dir: Path, project: Path, **changes) -> VPipeConfig:
    values = {
        "binary": binary,
        "work_dir": work_dir,
        "project_root": project,
        "worker_enabled": True,
        "worker_heartbeat_timeout_seconds": 5.0,
        "worker_cooldown_seconds": 0.0,
        "worker_memory_poll_seconds": 0.001,
        "worker_memory_stable_samples": 1,
        "worker_min_memory_free_percent": 0.0,
        "worker_min_reclaimable_mb": 0,
        "worker_max_wired_percent": 100.0,
    }
    values.update(changes)
    return VPipeConfig(**values)


def test_worker_rejects_nonempty_but_invalid_partial_video(tmp_path: Path, monkeypatch):
    partial = tmp_path / "result.partial.mp4"
    partial.write_bytes(b"truncated-mp4")
    monkeypatch.setattr("h3_bridge.vpipe.shutil.which", lambda name: "/ffprobe")
    monkeypatch.setattr(
        "h3_bridge.vpipe.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", "invalid"),
    )

    assert VPipeWorker._valid_recovered_video(partial) is False


def test_runner_repairs_queued_status_missing_durable_ticket(tmp_path: Path):
    project = tmp_path / "project"
    output = project / "runtime" / "ComfyUI" / "output"
    output.mkdir(parents=True)
    binary, work_dir = make_fake_vpipe(tmp_path)
    image = tmp_path / "cat.png"
    image.write_bytes(b"image")
    config = worker_config(binary, work_dir, project)
    worker = VPipeWorker(
        project,
        vpipe_config=config,
        bridge_config=BridgeConfig(
            project_root=project,
            h3_binary=tmp_path / "unused-h3",
            model_root=tmp_path / "unused-model",
        ),
    )
    worker.heartbeat()

    request = VPipeRequest(
        prompt="The same cat sings toward the camera.",
        first_frame=image,
        resource_profile="max",
    )
    runner = VPipeRunner(config)
    job_id = runner._job_id(request)
    job_dir = output / config.output_subdir / job_id
    job_dir.mkdir(parents=True)
    pipeline = job_dir / "pipeline.vpipeline"
    pipeline.write_text("{}", encoding="utf-8")
    status = job_dir / "vpipe-status.json"
    status.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "job_id": job_id,
                "state": "queued",
                "progress": 1,
                "force_rerun": False,
            }
        ),
        encoding="utf-8",
    )

    errors: list[BaseException] = []

    def attach_client() -> None:
        try:
            runner.run(request, output_root=output)
        except BaseException as exc:
            errors.append(exc)

    client = threading.Thread(target=attach_client)
    client.start()
    ticket_path = worker.queue_root / f"{job_id}.json"
    deadline = time.monotonic() + 5.0
    while not ticket_path.is_file():
        if time.monotonic() >= deadline:
            raise AssertionError("runner did not repair the missing durable ticket")
        time.sleep(0.02)

    ticket = json.loads(ticket_path.read_text(encoding="utf-8"))
    assert ticket["job_id"] == job_id
    assert ticket["pipeline_sha256"] == hashlib.sha256(b"{}").hexdigest()
    repaired = json.loads(status.read_text(encoding="utf-8"))
    assert repaired["state"] == "queued"
    assert repaired["message"] == "Recovered missing durable worker ticket"

    status.write_text(
        json.dumps(
            {
                **repaired,
                "state": "cancelled",
                "error": "test finished observing the repaired ticket",
            }
        ),
        encoding="utf-8",
    )
    client.join(timeout=5)
    assert not client.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], InterruptedError)


@pytest.mark.skipif(platform.system() != "Darwin", reason="launch identity is macOS-only")
def test_launchd_worker_completes_durable_ticket_and_runner_observes_it(
    tmp_path: Path, monkeypatch
):
    project = tmp_path / "project"
    output = project / "runtime" / "ComfyUI" / "output"
    output.mkdir(parents=True)
    binary, work_dir = make_fake_vpipe(tmp_path)
    image = tmp_path / "cat.png"
    image.write_bytes(b"image")
    vpipe_config = worker_config(binary, work_dir, project)
    bridge_config = BridgeConfig(
        project_root=project,
        h3_binary=tmp_path / "unused-h3",
        model_root=tmp_path / "unused-model",
    )
    worker = VPipeWorker(
        project,
        vpipe_config=vpipe_config,
        bridge_config=bridge_config,
    )
    monkeypatch.setattr(
        worker,
        "_valid_recovered_video",
        lambda path: path.is_file() and path.stat().st_size > 0,
    )
    monkeypatch.setattr(
        "h3_bridge.vpipe._valid_video_file",
        lambda path: path.is_file() and path.stat().st_size > 0,
    )
    worker.heartbeat()

    result_box: list[object] = []
    error_box: list[BaseException] = []

    def run_client() -> None:
        try:
            result_box.append(
                VPipeRunner(vpipe_config).run(
                    VPipeRequest(
                        prompt="The same cat walks toward the camera.",
                        first_frame=image,
                        resource_profile="max",
                    ),
                    output_root=output,
                )
            )
        except BaseException as exc:
            error_box.append(exc)

    client = threading.Thread(target=run_client)
    client.start()
    deadline = time.monotonic() + 5.0
    while not list((worker.queue_root).glob("vpipe-*.json")):
        if time.monotonic() >= deadline:
            raise AssertionError("client did not publish a worker ticket")
        time.sleep(0.02)
    assert worker.serve(once=True) == 0
    client.join(timeout=5)

    assert not client.is_alive()
    assert error_box == []
    result = result_box[0]
    assert result.output_path.read_bytes() == b"worker-mp4"
    status = json.loads((result.job_dir / "vpipe-status.json").read_text())
    assert status["state"] == "completed"
    assert status["progress"] == 100
    assert not list((project / "runtime" / "job-registry").glob("*.json"))


@pytest.mark.skipif(platform.system() != "Darwin", reason="launch identity is macOS-only")
def test_worker_force_rerun_replaces_completed_video(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    output = project / "runtime/ComfyUI/output"
    output.mkdir(parents=True)
    work_dir = tmp_path / "vpipe-work-force"
    work_dir.mkdir()
    binary = tmp_path / "fake-vpipe-force"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        "counter = pathlib.Path(__file__).with_suffix('.count')\n"
        "count = int(counter.read_text()) + 1 if counter.exists() else 1\n"
        "counter.write_text(str(count))\n"
        "p = pathlib.Path(sys.argv[sys.argv.index('--launch') + 1])\n"
        "graph = json.loads(p.read_text())\n"
        "save = next(x for x in graph['stages'] if x['id'] == 'save-video')\n"
        "pathlib.Path(save['config']['output_url']).write_bytes(f'worker-mp4-{count}'.encode())\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    image = tmp_path / "cat-force.png"
    image.write_bytes(b"image")
    config = worker_config(binary, work_dir, project)
    bridge = BridgeConfig(
        project_root=project,
        h3_binary=tmp_path / "unused-h3",
        model_root=tmp_path / "unused-model",
    )
    worker = VPipeWorker(project, vpipe_config=config, bridge_config=bridge)
    monkeypatch.setattr(
        worker,
        "_valid_recovered_video",
        lambda path: path.is_file() and path.stat().st_size > 0,
    )
    monkeypatch.setattr(
        "h3_bridge.vpipe._valid_video_file",
        lambda path: path.is_file() and path.stat().st_size > 0,
    )
    worker.heartbeat()
    request = VPipeRequest(prompt="rerun this cat shot", first_frame=image)

    def submit(*, reuse_completed: bool) -> object:
        results: list[object] = []
        errors: list[BaseException] = []

        def client() -> None:
            try:
                results.append(
                    VPipeRunner(config).run(
                        request,
                        output_root=output,
                        reuse_completed=reuse_completed,
                    )
                )
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=client)
        thread.start()
        deadline = time.monotonic() + 5.0
        while not list(worker.queue_root.glob("vpipe-*.json")):
            if time.monotonic() >= deadline:
                raise AssertionError("client did not publish a worker ticket")
            time.sleep(0.02)
        ticket = json.loads(next(worker.queue_root.glob("vpipe-*.json")).read_text())
        assert ticket["force_rerun"] is (not reuse_completed)
        assert worker.serve(once=True) == 0
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert errors == []
        return results[0]

    first = submit(reuse_completed=True)
    assert first.output_path.read_bytes() == b"worker-mp4-1"
    second = submit(reuse_completed=False)
    assert second.output_path.read_bytes() == b"worker-mp4-2"
    assert binary.with_suffix(".count").read_text() == "2"


def test_worker_waits_for_consecutive_memory_recovery_samples(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    output = project / "runtime" / "ComfyUI" / "output" / "h3-jobs"
    job_dir = output / ("vpipe-" + "a" * 20)
    job_dir.mkdir(parents=True)
    binary, work_dir = make_fake_vpipe(tmp_path)
    config = worker_config(
        binary,
        work_dir,
        project,
        worker_memory_stable_samples=2,
        worker_min_memory_free_percent=20.0,
    )
    bridge = BridgeConfig(
        project_root=project,
        h3_binary=tmp_path / "unused-h3",
        model_root=tmp_path / "unused-model",
    )
    samples = iter(
        [
            ResourceHealth(10.0, 0, 0, 3 * 1024**3, 24 * 1024**3),
            ResourceHealth(35.0, 0, 0, 3 * 1024**3, 24 * 1024**3),
            ResourceHealth(35.0, 0, 0, 3 * 1024**3, 24 * 1024**3),
        ]
    )
    monkeypatch.setattr("h3_bridge.vpipe_worker.resource_health", lambda: next(samples))
    worker = VPipeWorker(project, vpipe_config=config, bridge_config=bridge)

    worker._wait_for_memory_recovery(job_dir)

    status = json.loads((job_dir / "vpipe-status.json").read_text())
    assert status["state"] == "queued"
    assert status["message"] == "Memory recovered; starting vpipe"
    assert status["memory_gate"]["estimated_reclaimable_mb"] == 8602


def test_paused_job_cannot_pass_prelaunch_memory_gate(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    job_dir = (
        project
        / "runtime/ComfyUI/output/h3-jobs"
        / ("vpipe-" + "b" * 20)
    )
    job_dir.mkdir(parents=True)
    (job_dir / "pause.request").touch()
    binary, work_dir = make_fake_vpipe(tmp_path)
    config = worker_config(binary, work_dir, project)
    bridge = BridgeConfig(
        project_root=project,
        h3_binary=tmp_path / "unused-h3",
        model_root=tmp_path / "unused-model",
    )
    sampled = threading.Event()

    def sample() -> ResourceHealth:
        sampled.set()
        return ResourceHealth(50.0, 0, 0, 2 * 1024**3, 24 * 1024**3)

    monkeypatch.setattr("h3_bridge.vpipe_worker.resource_health", sample)
    worker = VPipeWorker(project, vpipe_config=config, bridge_config=bridge)
    with pytest.raises(_PausedBeforeLaunch):
        worker._wait_for_memory_recovery(job_dir)
    assert not sampled.is_set()
    assert json.loads((job_dir / "vpipe-status.json").read_text())["state"] == "paused"

    (job_dir / "pause.request").unlink()
    worker._wait_for_memory_recovery(job_dir)
    assert sampled.is_set()


def test_pause_race_after_lock_acquisition_releases_global_lock(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    job_dir = (
        project
        / "runtime/ComfyUI/output/h3-jobs"
        / ("vpipe-" + "c" * 20)
    )
    job_dir.mkdir(parents=True)
    pipeline = job_dir / "pipeline.vpipeline"
    pipeline.write_text("{}", encoding="utf-8")
    binary, work_dir = make_fake_vpipe(tmp_path)
    config = worker_config(binary, work_dir, project)
    bridge = BridgeConfig(
        project_root=project,
        h3_binary=tmp_path / "unused-h3",
        model_root=tmp_path / "unused-model",
    )
    worker = VPipeWorker(project, vpipe_config=config, bridge_config=bridge)
    ticket = {
        "job_id": job_dir.name,
        "job_dir": str(job_dir),
        "pipeline_sha256": hashlib.sha256(pipeline.read_bytes()).hexdigest(),
        "resource_profile": "low",
    }
    monkeypatch.setattr(worker, "_wait_for_memory_recovery", lambda _job: None)
    original_wait = worker._wait_for_generation_lock

    def acquire_then_pause(selected_job: Path):
        lock = original_wait(selected_job)
        (selected_job / "pause.request").touch()
        return lock

    monkeypatch.setattr(worker, "_wait_for_generation_lock", acquire_then_pause)
    with pytest.raises(_PausedBeforeLaunch):
        worker._launch(ticket, job_dir)

    lock_path = project / "runtime/h3-generation.lock"
    with lock_path.open("a+") as probe:
        fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_cancelled_paused_ticket_is_selected_and_removed(tmp_path: Path):
    project = tmp_path / "project"
    job_dir = (
        project
        / "runtime/ComfyUI/output/h3-jobs"
        / ("vpipe-" + "d" * 20)
    )
    job_dir.mkdir(parents=True)
    pipeline = job_dir / "pipeline.vpipeline"
    pipeline.write_text("{}", encoding="utf-8")
    (job_dir / "pause.request").touch()
    (job_dir / "cancel.request").touch()
    binary, work_dir = make_fake_vpipe(tmp_path)
    config = worker_config(binary, work_dir, project)
    bridge = BridgeConfig(
        project_root=project,
        h3_binary=tmp_path / "unused-h3",
        model_root=tmp_path / "unused-model",
    )
    worker = VPipeWorker(project, vpipe_config=config, bridge_config=bridge)
    ticket_path = worker.queue_root / f"{job_dir.name}.json"
    ticket_path.write_text(
        json.dumps(
            {
                "job_id": job_dir.name,
                "job_dir": str(job_dir),
                "pipeline_sha256": hashlib.sha256(pipeline.read_bytes()).hexdigest(),
                "resource_profile": "low",
            }
        ),
        encoding="utf-8",
    )

    assert worker.serve(once=True) == 0

    assert not ticket_path.exists()
    status = json.loads((job_dir / "vpipe-status.json").read_text())
    assert status["state"] == "cancelled"


def test_cancel_during_asset_readiness_cannot_return_to_queued(
    tmp_path: Path, monkeypatch
):
    project = tmp_path / "project"
    job_dir = (
        project
        / "runtime/ComfyUI/output/h3-jobs"
        / ("vpipe-" + "e" * 20)
    )
    job_dir.mkdir(parents=True)
    pipeline = job_dir / "pipeline.vpipeline"
    pipeline.write_text("{}", encoding="utf-8")
    binary, work_dir = make_fake_vpipe(tmp_path)
    config = worker_config(binary, work_dir, project)
    bridge = BridgeConfig(
        project_root=project,
        h3_binary=tmp_path / "unused-h3",
        model_root=tmp_path / "unused-model",
    )
    worker = VPipeWorker(project, vpipe_config=config, bridge_config=bridge)
    ticket_path = worker.queue_root / f"{job_dir.name}.json"
    ticket_path.write_text(
        json.dumps(
            {
                "job_id": job_dir.name,
                "job_dir": str(job_dir),
                "pipeline_sha256": hashlib.sha256(pipeline.read_bytes()).hexdigest(),
                "resource_profile": "low",
            }
        ),
        encoding="utf-8",
    )
    entered = threading.Event()
    release = threading.Event()

    def slow_readiness(*, force: bool = False) -> str:
        entered.set()
        assert release.wait(timeout=2)
        return "assets are incomplete"

    monkeypatch.setattr(worker, "_readiness_error", slow_readiness)
    thread = threading.Thread(target=lambda: worker.serve(once=True))
    thread.start()
    assert entered.wait(timeout=2)
    (job_dir / "cancel.request").touch()
    ticket_path.unlink()
    worker._status(job_dir, state="cancelled", message="Cancelled from ComfyUI")
    release.set()
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert json.loads((job_dir / "vpipe-status.json").read_text())["state"] == "cancelled"


@pytest.mark.skipif(platform.system() != "Darwin", reason="launch identity is macOS-only")
def test_worker_retries_same_ticket_once_after_vpipe_memory_refusal(
    tmp_path: Path, monkeypatch
):
    project = tmp_path / "project"
    output = project / "runtime" / "ComfyUI" / "output"
    output.mkdir(parents=True)
    work_dir = tmp_path / "vpipe-work-retry"
    work_dir.mkdir()
    binary = tmp_path / "fake-vpipe-retry"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        "marker = pathlib.Path(__file__).with_suffix('.attempted')\n"
        "p = pathlib.Path(sys.argv[sys.argv.index('--launch') + 1])\n"
        "graph = json.loads(p.read_text())\n"
        "if not marker.exists():\n"
        "    marker.touch()\n"
        "    print('[ERROR] not enough memory for a forward; refusing rather than thrashing', flush=True)\n"
        "    raise SystemExit(0)\n"
        "save = next(x for x in graph['stages'] if x['id'] == 'save-video')\n"
        "pathlib.Path(save['config']['output_url']).write_bytes(b'retried-mp4')\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    image = tmp_path / "cat-retry.png"
    image.write_bytes(b"image")
    config = worker_config(
        binary,
        work_dir,
        project,
        worker_memory_retry_limit=1,
    )
    bridge = BridgeConfig(
        project_root=project,
        h3_binary=tmp_path / "unused-h3",
        model_root=tmp_path / "unused-model",
    )
    worker = VPipeWorker(project, vpipe_config=config, bridge_config=bridge)
    monkeypatch.setattr(
        worker,
        "_valid_recovered_video",
        lambda path: path.is_file() and path.stat().st_size > 0,
    )
    monkeypatch.setattr(
        "h3_bridge.vpipe._valid_video_file",
        lambda path: path.is_file() and path.stat().st_size > 0,
    )
    worker.heartbeat()
    results: list[object] = []
    errors: list[BaseException] = []

    def run_client() -> None:
        try:
            results.append(
                VPipeRunner(config).run(
                    VPipeRequest(
                        prompt="retry the same cat shot",
                        first_frame=image,
                        resource_profile="max",
                    ),
                    output_root=output,
                )
            )
        except BaseException as exc:
            errors.append(exc)

    client = threading.Thread(target=run_client)
    client.start()
    deadline = time.monotonic() + 5.0
    while not list(worker.queue_root.glob("vpipe-*.json")):
        if time.monotonic() >= deadline:
            raise AssertionError("client did not publish a worker ticket")
        time.sleep(0.02)

    assert worker.serve(once=True) == 0
    retry_ticket = next(worker.queue_root.glob("vpipe-*.json"))
    queued = json.loads(retry_ticket.read_text())
    assert queued["memory_retry_attempts"] == 1
    assert worker.serve(once=True) == 0
    client.join(timeout=5)

    assert not client.is_alive()
    assert errors == []
    assert results[0].output_path.read_bytes() == b"retried-mp4"
    assert not list(worker.queue_root.glob("vpipe-*.json"))
