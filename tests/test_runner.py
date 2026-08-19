from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from h3_bridge.config import BridgeConfig
from h3_bridge.models import H3Reference, H3Request
from h3_bridge.runner import H3Runner


def make_runner(tmp_path: Path) -> H3Runner:
    fake = Path(__file__).with_name("fake_h3.py")
    fake.chmod(0o755)
    model_root = tmp_path / "models"
    (model_root / "FL2VA").mkdir(parents=True)
    (model_root / "Ref2VA").mkdir(parents=True)
    config = BridgeConfig(
        project_root=tmp_path,
        h3_binary=fake,
        model_root=model_root,
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


@patch(
    "h3_bridge.runner.os.uname",
    return_value=SimpleNamespace(sysname="Darwin", machine="arm64"),
)
def test_runner_persists_job_and_reuses_completed_result(_uname, tmp_path: Path):
    runner = make_runner(tmp_path)
    request = H3Request(prompt="a cat playing in water", resource_profile="max")
    updates: list[tuple[int, int]] = []
    first = runner.run(
        request,
        tmp_path / "output",
        progress=lambda current, total, _line: updates.append((current, total)),
    )
    assert first.output_path.read_bytes() == b"fake-mp4-for-tests"
    assert (first.job_dir / "request.json").is_file()
    assert (first.job_dir / "engine.log").is_file()
    assert updates[-1] == (20, 20)

    second = runner.run(request, tmp_path / "output", reuse_completed=True)
    assert second.output_path == first.output_path
    assert second.elapsed_seconds == 0.0


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
    try:
        runner.validate(request)
    except ValueError as exc:
        assert "cannot be combined" in str(exc)
    else:
        raise AssertionError("Expected mixed conditioning modes to be rejected")
