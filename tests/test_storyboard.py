from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from h3_bridge.storyboard import assemble_storyboard, build_shot_prompt


def test_build_shot_prompt_uses_beginner_fields_in_order():
    prompt = build_shot_prompt(
        "The same tabby cat from Picture 1",
        "0–2s: looks at the water. 2–5s: splashes with both front paws.",
        "A shallow forest stream",
        "Low medium tracking shot",
        "Natural sunlight and stream sound",
        "No frozen pose",
    )
    assert prompt.splitlines()[0].startswith("Subject and continuity:")
    assert "Action timeline:" in prompt
    assert prompt.splitlines()[-1] == "Avoid: No frozen pose"


def test_build_shot_prompt_requires_subject_and_action():
    with pytest.raises(ValueError, match="Subject"):
        build_shot_prompt("", "moves")
    with pytest.raises(ValueError, match="Action"):
        build_shot_prompt("cat", "")


def test_assemble_storyboard_preserves_order_and_reuses_result(tmp_path: Path):
    output_root = tmp_path / "output"
    jobs_root = output_root / "h3-jobs"
    first_dir = jobs_root / "first"
    second_dir = jobs_root / "second"
    first_dir.mkdir(parents=True)
    second_dir.mkdir(parents=True)
    (first_dir / "result.mp4").write_bytes(b"first")
    (second_dir / "result.mp4").write_bytes(b"second")

    def fake_ffmpeg(command, **_kwargs):
        Path(command[-1]).write_bytes(b"joined")
        return SimpleNamespace(returncode=0, stderr="")

    with (
        patch("h3_bridge.storyboard.shutil.which", return_value="/usr/bin/ffmpeg"),
        patch("h3_bridge.storyboard.subprocess.run", side_effect=fake_ffmpeg) as run,
    ):
        first = assemble_storyboard(
            [first_dir, second_dir],
            output_root=output_root,
            jobs_subdir="h3-jobs",
            title="Cat story",
        )
        second = assemble_storyboard(
            [first_dir, second_dir],
            output_root=output_root,
            jobs_subdir="h3-jobs",
            title="Cat story",
        )

    assert first.output_path.read_bytes() == b"joined"
    assert not first.reused
    assert second.reused
    assert run.call_count == 1
    metadata = json.loads((first.project_dir / "storyboard.json").read_text())
    assert metadata["shots"] == [
        str(first_dir / "result.mp4"),
        str(second_dir / "result.mp4"),
    ]


def test_assemble_storyboard_rejects_paths_outside_h3_jobs(tmp_path: Path):
    output_root = tmp_path / "output"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "result.mp4").write_bytes(b"video")
    with (
        patch("h3_bridge.storyboard.shutil.which", return_value="/usr/bin/ffmpeg"),
        pytest.raises(ValueError, match="must be inside"),
    ):
        assemble_storyboard(
            [outside, outside],
            output_root=output_root,
            jobs_subdir="h3-jobs",
        )
