from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from h3_bridge.comfy_jobs import VPipeJobManager


def write_config(project: Path) -> None:
    (project / "config.json").write_text(
        json.dumps(
            {
                "vpipe_binary": "/usr/bin/true",
                "vpipe_work_dir": str(project),
                "vpipe_worker_enabled": True,
                "output_subdir": "h3-jobs",
            }
        ),
        encoding="utf-8",
    )


def write_job(project: Path, output: Path, state: str = "queued") -> Path:
    job = output / "h3-jobs" / "vpipe-0123456789abcdefabcd"
    job.mkdir(parents=True)
    (job / "pipeline.vpipeline").write_text("{}", encoding="utf-8")
    (job / "request.json").write_text(
        json.dumps(
            {
                "prompt": "a travelling tabby cat",
                "seed": 2404,
                "width": 1152,
                "height": 640,
                "frames": 124,
                "fps": 24,
                "resource_profile": "low",
            }
        ),
        encoding="utf-8",
    )
    (job / "vpipe-status.json").write_text(
        json.dumps({"job_id": job.name, "state": state, "progress": 25}),
        encoding="utf-8",
    )
    queue = project / "runtime" / "vpipe-worker" / "queue"
    queue.mkdir(parents=True)
    (queue / f"{job.name}.json").write_text(
        json.dumps({"job_id": job.name, "job_dir": str(job)}), encoding="utf-8"
    )
    return job


def test_snapshot_exposes_durable_worker_job(tmp_path: Path):
    project = tmp_path / "project"
    output = project / "runtime" / "ComfyUI" / "output"
    output.mkdir(parents=True)
    write_config(project)
    job = write_job(project, output)
    worker = project / "runtime" / "vpipe-worker"
    (worker / "heartbeat.json").write_text(
        json.dumps(
            {
                "state": "running",
                "pid": os.getpid(),
                "message": "Generating video frames",
                "active_job": job.name,
                "updated_at": 10**20,
            }
        ),
        encoding="utf-8",
    )

    before = time.time()
    snapshot = VPipeJobManager(project, output).snapshot()

    assert snapshot["snapshot_at"] >= before
    assert snapshot["worker"]["online"] is True
    assert snapshot["worker"]["message"] == "Generating video frames"
    assert snapshot["jobs"][0]["job_id"] == job.name
    assert snapshot["jobs"][0]["progress"] == 25
    assert snapshot["jobs"][0]["fps"] == 24
    assert snapshot["jobs"][0]["queue_position"] == 1


def test_completed_corrupt_result_is_failed_and_retryable(
    tmp_path: Path, monkeypatch
):
    project = tmp_path / "project"
    output = project / "runtime/ComfyUI/output"
    output.mkdir(parents=True)
    write_config(project)
    job = write_job(project, output, state="completed")
    (project / "runtime/vpipe-worker/queue" / f"{job.name}.json").unlink()
    (job / "result.mp4").write_bytes(b"truncated-cache")
    monkeypatch.setattr("h3_bridge.comfy_jobs._valid_video_file", lambda _path: False)
    manager = VPipeJobManager(project, output)

    snapshot = manager.snapshot()

    assert snapshot["jobs"][0]["state"] == "failed"
    assert snapshot["jobs"][0]["video_url"] == ""
    assert "playable" in snapshot["jobs"][0]["error"]
    manager.act(job.name, "retry")
    assert (manager.queue_root / f"{job.name}.json").is_file()


def test_force_rerun_with_old_result_stays_visible_as_queued(tmp_path: Path):
    project = tmp_path / "project"
    output = project / "runtime/ComfyUI/output"
    output.mkdir(parents=True)
    write_config(project)
    job = write_job(project, output)
    (job / "result.mp4").write_bytes(b"previous-completed-video")
    ticket_path = project / "runtime/vpipe-worker/queue" / f"{job.name}.json"
    ticket = json.loads(ticket_path.read_text())
    ticket["force_rerun"] = True
    ticket_path.write_text(json.dumps(ticket), encoding="utf-8")

    snapshot = VPipeJobManager(project, output).snapshot()

    assert snapshot["jobs"][0]["state"] == "queued"
    assert snapshot["jobs"][0]["progress"] == 25


def test_failed_force_rerun_does_not_show_old_result_as_completed(tmp_path: Path):
    project = tmp_path / "project"
    output = project / "runtime/ComfyUI/output"
    output.mkdir(parents=True)
    write_config(project)
    job = write_job(project, output, state="failed")
    (job / "result.mp4").write_bytes(b"previous-completed-video")
    (project / "runtime/vpipe-worker/queue" / f"{job.name}.json").unlink()
    status_path = job / "vpipe-status.json"
    status = json.loads(status_path.read_text())
    status.update({"force_rerun": True, "error": "replacement failed"})
    status_path.write_text(json.dumps(status), encoding="utf-8")

    manager = VPipeJobManager(project, output)
    snapshot = manager.snapshot()
    assert snapshot["jobs"][0]["state"] == "failed"
    assert snapshot["jobs"][0]["video_url"] == ""

    manager.act(job.name, "retry")
    ticket = json.loads(
        (manager.queue_root / f"{job.name}.json").read_text(encoding="utf-8")
    )
    assert ticket["force_rerun"] is True


def test_pause_and_resume_queued_job(tmp_path: Path):
    project = tmp_path / "project"
    output = project / "runtime" / "ComfyUI" / "output"
    output.mkdir(parents=True)
    write_config(project)
    job = write_job(project, output)
    manager = VPipeJobManager(project, output)

    manager.act(job.name, "pause")
    assert (job / "pause.request").is_file()
    assert json.loads((job / "vpipe-status.json").read_text())["state"] == "paused"

    manager.act(job.name, "resume")
    assert not (job / "pause.request").exists()
    assert json.loads((job / "vpipe-status.json").read_text())["state"] == "queued"


def test_cancel_and_retry_queued_job(tmp_path: Path):
    project = tmp_path / "project"
    output = project / "runtime" / "ComfyUI" / "output"
    output.mkdir(parents=True)
    write_config(project)
    job = write_job(project, output)
    manager = VPipeJobManager(project, output)

    manager.act(job.name, "cancel")
    assert json.loads((job / "vpipe-status.json").read_text())["state"] == "cancelled"
    assert not (manager.queue_root / f"{job.name}.json").exists()

    manager.act(job.name, "retry")
    ticket = json.loads((manager.queue_root / f"{job.name}.json").read_text())
    assert ticket["pipeline_sha256"]
    assert json.loads((job / "vpipe-status.json").read_text())["state"] == "queued"
    assert not (job / "cancel.request").exists()


def test_retry_preserves_latest_selected_profile(tmp_path: Path):
    project = tmp_path / "project"
    output = project / "runtime/ComfyUI/output"
    output.mkdir(parents=True)
    write_config(project)
    job = write_job(project, output, state="failed")
    (project / "runtime/vpipe-worker/queue" / f"{job.name}.json").unlink()
    (job / "control.json").write_text(json.dumps({"policy": "auto"}))
    manager = VPipeJobManager(project, output)

    manager.act(job.name, "retry")

    ticket = json.loads((manager.queue_root / f"{job.name}.json").read_text())
    assert ticket["resource_profile"] == "auto"


def test_profile_change_is_rejected_after_launch_caps_freeze(tmp_path: Path):
    project = tmp_path / "project"
    output = project / "runtime/ComfyUI/output"
    output.mkdir(parents=True)
    write_config(project)
    job = write_job(project, output, state="launching")
    manager = VPipeJobManager(project, output)

    with pytest.raises(ValueError, match="already frozen"):
        manager.act(job.name, "max")


def test_incomplete_legacy_job_is_retryable(tmp_path: Path):
    project = tmp_path / "project"
    output = project / "runtime" / "ComfyUI" / "output"
    output.mkdir(parents=True)
    write_config(project)
    job = write_job(project, output)
    (job / "vpipe-status.json").unlink()
    (project / "runtime" / "vpipe-worker" / "queue" / f"{job.name}.json").unlink()
    manager = VPipeJobManager(project, output)

    snapshot = manager.snapshot()
    assert snapshot["jobs"][0]["state"] == "failed"

    manager.act(job.name, "retry")
    assert (manager.queue_root / f"{job.name}.json").is_file()
