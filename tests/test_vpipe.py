from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from h3_bridge.models import H3Reference
from h3_bridge.vpipe import (
    VPipeConfig,
    VPipeRequest,
    VPipeRunner,
    _pipeline,
    _progress_from_line,
    _valid_video_file,
    _wait_for_valid_video,
    load_vpipe_config,
    validate_vpipe_installation,
)


def test_video_cache_requires_video_stream_and_positive_duration(
    tmp_path: Path, monkeypatch
):
    video = tmp_path / "result.mp4"
    video.write_bytes(b"container")
    monkeypatch.setattr("h3_bridge.vpipe.shutil.which", lambda _name: "/ffprobe")
    payload = {"streams": [{"codec_type": "video"}], "format": {"duration": "1.5"}}
    monkeypatch.setattr(
        "h3_bridge.vpipe.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, json.dumps(payload), ""
        ),
    )

    assert _valid_video_file(video) is True
    payload["format"]["duration"] = "0"
    assert _valid_video_file(video) is False


def test_video_cache_falls_back_to_homebrew_ffprobe_under_launchd(
    tmp_path: Path, monkeypatch
):
    video = tmp_path / "result.mp4"
    video.write_bytes(b"container")
    monkeypatch.setattr("h3_bridge.vpipe.shutil.which", lambda _name: None)
    monkeypatch.setattr(
        "h3_bridge.vpipe.Path.is_file",
        lambda path: str(path) == "/opt/homebrew/bin/ffprobe"
        or path == video,
    )
    payload = {"streams": [{"codec_type": "video"}], "format": {"duration": "1"}}
    observed: dict[str, str] = {}

    def fake_run(args, **_kwargs):
        observed["binary"] = args[0]
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

    monkeypatch.setattr("h3_bridge.vpipe.subprocess.run", fake_run)

    assert _valid_video_file(video) is True
    assert observed["binary"] == "/opt/homebrew/bin/ffprobe"


def test_completed_worker_video_gets_a_short_settling_window(
    tmp_path: Path, monkeypatch
):
    attempts = iter([False, False, True])
    monkeypatch.setattr(
        "h3_bridge.vpipe._valid_video_file", lambda _path: next(attempts)
    )

    assert _wait_for_valid_video(
        tmp_path / "result.mp4", timeout_seconds=1.0, poll_seconds=0.0
    ) is True


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


def test_vpipe_runner_materializes_silent_pipeline_and_reuses_result(
    tmp_path: Path, monkeypatch
):
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
    monkeypatch.setattr(
        "h3_bridge.vpipe._valid_video_file",
        lambda path: path.is_file() and path.read_bytes() == b"fake-vpipe-mp4",
    )
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
    generate = next(
        stage for stage in graph["stages"] if stage["id"] == "generate-video"
    )
    assert generate["config"]["steps"] == request.steps + 1
    assert save["config"]["enable_audio"] is False
    assert save["config"]["video_bitrate"] == 10_000_000
    assert save["iports"] == [{"src": "rgb-to-video", "oport": 0}]

    second = runner.run(request, output_root=tmp_path / "output")
    assert second.reused is True
    assert second.elapsed_seconds == 0.0
    assert second.output_path == first.output_path

    second.output_path.write_bytes(b"truncated-cache")
    third = runner.run(request, output_root=tmp_path / "output")
    assert third.reused is False
    assert third.output_path.read_bytes() == b"fake-vpipe-mp4"


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


def test_vpipe_ref2va_pipeline_uses_ordered_files_and_reference_rows(tmp_path: Path):
    binary, work_dir = make_fake_vpipe(tmp_path)
    cat = tmp_path / "cat.png"
    song = tmp_path / "song.wav"
    cat.write_bytes(b"image")
    song.write_bytes(b"audio")
    config = VPipeConfig(binary=binary, work_dir=work_dir)
    request = VPipeRequest(
        prompt="The same cat sings the supplied song.",
        references=(
            H3Reference("image", cat),
            H3Reference("audio", song),
        ),
        task="Ref2VA",
        width=640,
        height=1152,
        steps=4,
        adapter_profile="ref2va_turbo_4step",
        enable_h3_audio=True,
    )

    graph = _pipeline(config, request, tmp_path / "out.mp4")
    by_id = {stage["id"]: stage for stage in graph["stages"]}

    assert graph["id"] == "comfyui-h3-vpipe-reference-shot"
    assert by_id["model-select"]["config"]["hf_dir"] == config.ref_model
    assert by_id["minimax-h3-model-config"]["config"]["lora"] == config.ref_lora
    assert by_id["minimax-h3-model-config"]["config"]["video_shift"] == 12.0
    assert by_id["video-ref-encoder"]["config"]["references"] == [
        str(cat),
        str(song),
    ]
    assert by_id["video-ref-encoder"]["config"]["frames"] == request.frames
    assert by_id["video-ref-encoder"]["config"][
        "reference_image_short_edge"
    ] == 1024
    generate_inputs = by_id["generate-video"]["iports"]
    assert generate_inputs[0] == {"src": "video-ref-encoder", "oport": 0}
    assert generate_inputs[7] == {"src": "video-ref-encoder", "oport": 1}
    assert generate_inputs[8] == {"src": "video-ref-encoder", "oport": 2}
    assert by_id["generate-video"]["config"]["steps"] == request.steps + 1
    assert by_id["save-video"]["config"]["enable_audio"] is True


def test_vpipe_ref2va_cache_changes_with_reference_file(tmp_path: Path):
    binary, work_dir = make_fake_vpipe(tmp_path)
    cat = tmp_path / "cat.png"
    song = tmp_path / "song.wav"
    cat.write_bytes(b"image")
    song.write_bytes(b"audio-v1")
    runner = VPipeRunner(VPipeConfig(binary=binary, work_dir=work_dir))
    request = VPipeRequest(
        prompt="cat sings",
        references=(H3Reference("image", cat), H3Reference("audio", song)),
        task="Ref2VA",
        adapter_profile="ref2va_8step",
    )

    first_id = runner._job_id(request)
    song.write_bytes(b"audio-version-two")

    assert runner._job_id(request) != first_id


@pytest.mark.parametrize(
    ("references", "message"),
    [
        ((), "at least one"),
        (("audio",), "cannot be the only"),
        (("image",) * 10, "at most 9 image"),
    ],
)
def test_vpipe_ref2va_validates_reference_contract(
    tmp_path: Path, references: tuple[str, ...], message: str
):
    binary, work_dir = make_fake_vpipe(tmp_path)
    files: list[H3Reference] = []
    for index, kind in enumerate(references):
        path = tmp_path / f"reference-{index}.bin"
        path.write_bytes(b"reference")
        files.append(H3Reference(kind, path))
    request = VPipeRequest(
        prompt="cat sings",
        references=tuple(files),
        task="Ref2VA",
        adapter_profile="ref2va_8step",
    )

    with pytest.raises(ValueError, match=message):
        VPipeRunner(VPipeConfig(binary=binary, work_dir=work_dir)).validate(request)


def test_vpipe_ref2va_turbo_validates_user_facing_nfe(tmp_path: Path):
    binary, work_dir = make_fake_vpipe(tmp_path)
    cat = tmp_path / "cat.png"
    cat.write_bytes(b"image")
    request = VPipeRequest(
        prompt="cat sings",
        references=(H3Reference("image", cat),),
        task="Ref2VA",
        steps=5,
        adapter_profile="ref2va_turbo_4step",
    )

    with pytest.raises(ValueError, match="exactly 4 steps"):
        VPipeRunner(VPipeConfig(binary=binary, work_dir=work_dir)).validate(request)


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
            "[PROGRESS] 40% of 'denoise' completed at 00:52:14 (640/1600)",
            (45, "Generating video frames (40%)"),
        ),
        (
            "[PROGRESS] 100% of 'denoise' completed at 00:52:14 (1600/1600)",
            (75, "Generating video frames (100%)"),
        ),
        (
            "[PROGRESS] 40% of 'vae decode' completed at 00:52:14 (40/100)",
            None,
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
    generate = next(
        stage for stage in graph["stages"] if stage["id"] == "generate-video"
    )
    assert generate["config"]["steps"] == 5


def test_vpipe_pipeline_uses_configured_video_bitrate(tmp_path: Path):
    binary, work_dir = make_fake_vpipe(tmp_path)
    image = tmp_path / "cat.png"
    image.write_bytes(b"image")
    config = VPipeConfig(
        binary=binary,
        work_dir=work_dir,
        video_bitrate=14_000_000,
    )
    request = VPipeRequest(prompt="cat", first_frame=image)

    graph = _pipeline(config, request, tmp_path / "out.mp4")
    save = next(stage for stage in graph["stages"] if stage["id"] == "save-video")

    assert save["config"]["video_bitrate"] == 14_000_000


def test_vpipe_job_cache_isolated_by_engine_generation(tmp_path: Path):
    binary, work_dir = make_fake_vpipe(tmp_path)
    image = tmp_path / "cat.png"
    image.write_bytes(b"image")
    request = VPipeRequest(prompt="cat", first_frame=image)
    old_runner = VPipeRunner(
        VPipeConfig(
            binary=binary,
            work_dir=work_dir,
            engine_generation="vpipe-v0.1.30",
        )
    )
    new_runner = VPipeRunner(
        VPipeConfig(
            binary=binary,
            work_dir=work_dir,
            engine_generation="vpipe-v0.1.37",
        )
    )

    assert old_runner._job_id(request) != new_runner._job_id(request)


def test_vpipe_job_cache_isolated_by_pipeline_generation(
    tmp_path: Path, monkeypatch
):
    binary, work_dir = make_fake_vpipe(tmp_path)
    image = tmp_path / "cat.png"
    image.write_bytes(b"image")
    runner = VPipeRunner(VPipeConfig(binary=binary, work_dir=work_dir))
    request = VPipeRequest(prompt="cat", first_frame=image)

    current_id = runner._job_id(request)
    monkeypatch.setattr("h3_bridge.vpipe._PIPELINE_GENERATION", 4)

    assert runner._job_id(request) != current_id


def test_vpipe_installation_rejects_unverified_build(tmp_path: Path):
    binary, work_dir = make_fake_vpipe(tmp_path)
    with pytest.raises(RuntimeError, match="version mismatch"):
        validate_vpipe_installation(
            VPipeConfig(
                binary=binary,
                work_dir=work_dir,
                expected_ref="e843a7dd44f9988499f4f17d18f6c24940c670ac",
            )
        )


@pytest.mark.parametrize(
    ("request_changes", "message"),
    [
        ({"width": 650}, "multiples of 32"),
        ({"width": 1344, "height": 1344}, "Canvas area"),
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
