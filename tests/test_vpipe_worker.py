from __future__ import annotations

import json
import platform
import threading
import time
from pathlib import Path

import pytest

from h3_bridge.config import BridgeConfig
from h3_bridge.scheduler import ResourceHealth
from h3_bridge.vpipe import VPipeConfig, VPipeRequest, VPipeRunner
from h3_bridge.vpipe_worker import VPipeWorker


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


@pytest.mark.skipif(platform.system() != "Darwin", reason="launch identity is macOS-only")
def test_launchd_worker_completes_durable_ticket_and_runner_observes_it(tmp_path: Path):
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


@pytest.mark.skipif(platform.system() != "Darwin", reason="launch identity is macOS-only")
def test_worker_retries_same_ticket_once_after_vpipe_memory_refusal(tmp_path: Path):
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
