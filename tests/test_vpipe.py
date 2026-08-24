from __future__ import annotations

import json
from pathlib import Path

import pytest

from h3_bridge.vpipe import (
    VPipeConfig,
    VPipeRequest,
    VPipeRunner,
    _pipeline,
    _progress_from_line,
    load_vpipe_config,
)


def make_fake_vpipe(tmp_path: Path) -> tuple[Path, Path]:
    work_dir = tmp_path / "vpipe-work"
    work_dir.mkdir()
    binary = tmp_path / "fake-vpipe"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        "p = pathlib.Path(sys.argv[sys.argv.index('--launch') + 1])\n"
        "graph = json.loads(p.read_text())\n"
        "print(\"[INFO] ImageResampleStage('first-frame'): ready\", flush=True)\n"
        "print(\"[NORMAL] [h3-dit] first forward at 123 rows\", flush=True)\n"
        "print(\"[INFO] GenerateVideoStage('generate-video'): emitted MiniMax-H3 latents #1\", flush=True)\n"
        "save = next(x for x in graph['stages'] if x['id'] == 'save-video')\n"
        "pathlib.Path(save['config']['output_url']).write_bytes(b'fake-vpipe-mp4')\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    return binary, work_dir


def test_vpipe_config_finds_user_local_bin_under_launchd_path(
    tmp_path: Path, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    (project / "config.example.json").write_text(
        json.dumps({"vpipe_binary": "vpipe", "vpipe_work_dir": "vpipe-work"}),
        encoding="utf-8",
    )
    local_binary = tmp_path / "home" / ".local" / "bin" / "vpipe"
    local_binary.parent.mkdir(parents=True)
    local_binary.write_text("", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr("h3_bridge.vpipe.shutil.which", lambda _: None)

    config = load_vpipe_config(project)

    assert config.binary == local_binary.resolve()


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
    assert updates == [(1, 100), (5, 100), (25, 100), (75, 100), (100, 100)]
    graph = json.loads((first.job_dir / "pipeline.vpipeline").read_text())
    stage_ids = {stage["id"] for stage in graph["stages"]}
    assert "audio-vae-decode" not in stage_ids
    save = next(stage for stage in graph["stages"] if stage["id"] == "save-video")
    assert save["config"]["enable_audio"] is False
    assert save["iports"] == [{"src": "rgb-to-video", "oport": 0}]

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
    assert save["iports"] == [
        {"src": "rgb-to-video", "oport": 0},
        {"src": "audio-vae-decode", "oport": 0},
    ]


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (
            "[INFO] ImageResampleStage('first-frame'): -> 640x384, fit=1, metal ok",
            (5, "Preparing reference frame"),
        ),
        (
            "[NORMAL] [h3-dit] first forward at 123 rows",
            (25, "Generating video frames"),
        ),
        (
            "[INFO] GenerateVideoStage('generate-video'): emitted MiniMax-H3 latents #1",
            (75, "Video latents complete"),
        ),
        ("unrelated log line", None),
    ],
)
def test_vpipe_progress_markers(line: str, expected: tuple[int, str] | None):
    assert _progress_from_line(line) == expected


def test_vpipe_highres_profile_selects_matching_adapter_and_shift(tmp_path: Path):
    binary, work_dir = make_fake_vpipe(tmp_path)
    image = tmp_path / "cat.png"
    image.write_bytes(b"image")
    config = VPipeConfig(binary=binary, work_dir=work_dir)
    request = VPipeRequest(
        prompt="cat",
        first_frame=image,
        width=1152,
        height=640,
        steps=4,
        adapter_profile="turbo_highres_4step",
    )
    graph = _pipeline(config, request, tmp_path / "out.mp4")
    model_config = next(
        stage for stage in graph["stages"] if stage["id"] == "minimax-h3-model-config"
    )
    assert model_config["config"]["lora"] == config.lora_768p
    assert model_config["config"]["video_shift"] == 6.0


@pytest.mark.parametrize(
    ("request_changes", "message"),
    [
        ({"width": 650}, "multiples of 32"),
        ({"frames": 10}, "between 22 and 362"),
        ({"steps": 1}, "between 2 and 60"),
        ({"fps": 30}, "24 fps"),
        ({"resource_profile": "eco"}, "low, auto, or max"),
        ({"adapter_profile": "unknown"}, "Adapter profile"),
        ({"adapter_profile": "turbo_highres_4step"}, "starts at 1152x640"),
        (
            {
                "adapter_profile": "turbo_highres_4step",
                "width": 1152,
                "height": 640,
                "steps": 6,
            },
            "exactly 4 steps",
        ),
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
