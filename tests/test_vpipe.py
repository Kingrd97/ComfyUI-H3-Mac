from __future__ import annotations

import json
from pathlib import Path

import pytest

from h3_bridge.vpipe import VPipeConfig, VPipeRequest, VPipeRunner, _pipeline


def make_fake_vpipe(tmp_path: Path) -> tuple[Path, Path]:
    work_dir = tmp_path / "vpipe-work"
    work_dir.mkdir()
    binary = tmp_path / "fake-vpipe"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        "p = pathlib.Path(sys.argv[sys.argv.index('--launch') + 1])\n"
        "graph = json.loads(p.read_text())\n"
        "save = next(x for x in graph['stages'] if x['id'] == 'save-video')\n"
        "pathlib.Path(save['config']['output_url']).write_bytes(b'fake-vpipe-mp4')\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    return binary, work_dir


def test_vpipe_runner_materializes_silent_pipeline_and_reuses_result(tmp_path: Path):
    binary, work_dir = make_fake_vpipe(tmp_path)
    image = tmp_path / "cat.png"
    image.write_bytes(b"image")
    config = VPipeConfig(binary=binary, work_dir=work_dir)
    request = VPipeRequest(
        prompt="The same cat walks toward camera.",
        first_frame=image,
        resource_profile="max",
        enable_h3_audio=False,
    )
    updates: list[tuple[int, int]] = []
    runner = VPipeRunner(config)
    first = runner.run(
        request,
        output_root=tmp_path / "output",
        progress=lambda current, total, _line: updates.append((current, total)),
    )
    assert first.output_path.read_bytes() == b"fake-vpipe-mp4"
    assert updates == [(1, 100), (100, 100)]
    graph = json.loads((first.job_dir / "pipeline.vpipeline").read_text())
    stage_ids = {stage["id"] for stage in graph["stages"]}
    assert "audio-vae-decode" not in stage_ids
    save = next(stage for stage in graph["stages"] if stage["id"] == "save-video")
    assert save["config"]["enable_audio"] is False
    assert save["iports"][1]["src"] == ""

    second = runner.run(request, output_root=tmp_path / "output")
    assert second.reused is True
    assert second.elapsed_seconds == 0.0
    assert second.output_path == first.output_path


def test_vpipe_joint_audio_pipeline_has_audio_decoder(tmp_path: Path):
    binary, work_dir = make_fake_vpipe(tmp_path)
    image = tmp_path / "cat.png"
    image.write_bytes(b"image")
    config = VPipeConfig(binary=binary, work_dir=work_dir)
    request = VPipeRequest(prompt="cat", first_frame=image, enable_h3_audio=True)
    graph = _pipeline(config, request, tmp_path / "out.mp4")
    stage_ids = {stage["id"] for stage in graph["stages"]}
    assert "audio-vae-decode" in stage_ids
    save = next(stage for stage in graph["stages"] if stage["id"] == "save-video")
    assert save["config"]["enable_audio"] is True
    assert save["iports"][1]["src"] == "audio-vae-decode"


@pytest.mark.parametrize(
    ("request_changes", "message"),
    [
        ({"width": 650}, "multiples of 32"),
        ({"frames": 10}, "between 22 and 362"),
        ({"fps": 30}, "24 fps"),
        ({"resource_profile": "auto"}, "low or max"),
    ],
)
def test_vpipe_runner_validates_geometry_and_profile(
    tmp_path: Path, request_changes: dict, message: str
):
    binary, work_dir = make_fake_vpipe(tmp_path)
    image = tmp_path / "cat.png"
    image.write_bytes(b"image")
    request = VPipeRequest(prompt="cat", first_frame=image, **request_changes)
    with pytest.raises(ValueError, match=message):
        VPipeRunner(VPipeConfig(binary=binary, work_dir=work_dir)).validate(request)
