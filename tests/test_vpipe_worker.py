from __future__ import annotations

import json
import platform
import threading
import time
from pathlib import Path

import pytest

from h3_bridge.config import BridgeConfig
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


@pytest.mark.skipif(platform.system() != "Darwin", reason="launch identity is macOS-only")
def test_launchd_worker_completes_durable_ticket_and_runner_observes_it(tmp_path: Path):
    project = tmp_path / "project"
    output = project / "runtime" / "ComfyUI" / "output"
    output.mkdir(parents=True)
    binary, work_dir = make_fake_vpipe(tmp_path)
    image = tmp_path / "cat.png"
    image.write_bytes(b"image")
    vpipe_config = VPipeConfig(
        binary=binary,
        work_dir=work_dir,
        project_root=project,
        worker_enabled=True,
        worker_heartbeat_timeout_seconds=5.0,
    )
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
