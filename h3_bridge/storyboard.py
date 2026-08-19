from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class StoryboardResult:
    storyboard_id: str
    output_path: Path
    project_dir: Path
    reused: bool


def build_shot_prompt(
    subject: str,
    action_timeline: str,
    environment: str = "",
    camera: str = "",
    look_and_sound: str = "",
    avoid: str = "",
) -> str:
    """Build one readable H3 shot prompt from beginner-friendly fields."""
    if not subject.strip():
        raise ValueError("Subject and continuity must not be empty.")
    if not action_timeline.strip():
        raise ValueError("Action timeline must not be empty.")

    sections = [
        ("Subject and continuity", subject),
        ("Action timeline", action_timeline),
        ("Environment and physical interaction", environment),
        ("Camera and framing", camera),
        ("Look, lighting, and sound", look_and_sound),
        ("Avoid", avoid),
    ]
    return "\n".join(
        f"{label}: {value.strip()}" for label, value in sections if value.strip()
    )


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _ffconcat_quote(path: Path) -> str:
    # ffconcat accepts shell-like single-quoted strings. A literal quote is
    # represented by ending the string, escaping it, and opening it again.
    return str(path).replace("'", "'\\''")


def assemble_storyboard(
    job_dirs: Iterable[str | Path],
    *,
    output_root: Path,
    jobs_subdir: str,
    title: str = "storyboard",
) -> StoryboardResult:
    """Join completed H3 jobs in order without re-encoding their MP4 streams."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise FileNotFoundError("FFmpeg not found. Run Install.command first.")

    output_root = output_root.resolve()
    jobs_root = (output_root / jobs_subdir).resolve()
    videos: list[Path] = []
    for raw in job_dirs:
        if not str(raw).strip():
            continue
        job_dir = Path(raw).expanduser().resolve()
        if not _inside(job_dir, jobs_root):
            raise ValueError(f"Shot job must be inside {jobs_root}: {job_dir}")
        video = job_dir / "result.mp4"
        if not video.is_file() or video.stat().st_size == 0:
            raise FileNotFoundError(f"Completed shot video not found: {video}")
        videos.append(video)

    if len(videos) < 2:
        raise ValueError("Connect at least two completed H3 shot job directories.")

    digest_payload = []
    for video in videos:
        stat = video.stat()
        digest_payload.append(
            {"path": str(video), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        )
    normalized_title = title.strip() or "storyboard"
    encoded = json.dumps(
        {"title": normalized_title, "shots": digest_payload},
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    storyboard_id = hashlib.sha256(encoded).hexdigest()[:20]
    project_dir = output_root / "h3-storyboards" / storyboard_id
    project_dir.mkdir(parents=True, exist_ok=True)
    output_path = project_dir / "result.mp4"
    if output_path.is_file() and output_path.stat().st_size > 0:
        return StoryboardResult(storyboard_id, output_path, project_dir, True)

    concat_path = project_dir / "shots.ffconcat"
    partial_path = project_dir / "result.partial.mp4"
    log_path = project_dir / "ffmpeg.log"
    metadata_path = project_dir / "storyboard.json"
    concat_path.write_text(
        "ffconcat version 1.0\n"
        + "".join(f"file '{_ffconcat_quote(video)}'\n" for video in videos),
        encoding="utf-8",
    )
    metadata_path.write_text(
        json.dumps(
            {"title": normalized_title, "shots": [str(item) for item in videos]},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_path),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(partial_path),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    log_path.write_text(
        "COMMAND " + json.dumps(command, ensure_ascii=False) + "\n" + completed.stderr,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        partial_path.unlink(missing_ok=True)
        raise RuntimeError(
            "FFmpeg could not join the shots. Keep every shot at the same size, FPS, "
            f"and codec, then retry. See {log_path}"
        )
    if not partial_path.is_file() or partial_path.stat().st_size == 0:
        raise RuntimeError(f"FFmpeg finished without an MP4. See {log_path}")
    partial_path.replace(output_path)
    return StoryboardResult(storyboard_id, output_path, project_dir, False)
