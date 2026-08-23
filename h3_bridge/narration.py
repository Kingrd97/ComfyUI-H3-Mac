from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class NarrationCue:
    start_seconds: float
    text: str


@dataclass(frozen=True)
class NarrationResult:
    narration_id: str
    output_path: Path
    project_dir: Path
    reused: bool


def parse_timed_script(script: str) -> tuple[NarrationCue, ...]:
    """Parse one `seconds|dialogue` cue per non-empty line."""

    cues: list[NarrationCue] = []
    for line_number, raw in enumerate(script.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "|" not in line:
            raise ValueError(f"Narration line {line_number} must use seconds|dialogue.")
        start_raw, text = line.split("|", 1)
        try:
            start = float(start_raw.strip())
        except ValueError as exc:
            raise ValueError(
                f"Narration line {line_number} has an invalid start time."
            ) from exc
        if start < 0:
            raise ValueError(f"Narration line {line_number} cannot start before zero.")
        if not text.strip():
            raise ValueError(f"Narration line {line_number} has no dialogue.")
        cues.append(NarrationCue(start, text.strip()))
    if not cues:
        raise ValueError("Narration script has no cues.")
    if any(right.start_seconds < left.start_seconds for left, right in zip(cues, cues[1:])):
        raise ValueError("Narration cues must be ordered by start time.")
    return tuple(cues)


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _video_duration(ffprobe: str, video: Path) -> float:
    completed = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(video),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"FFprobe could not read storyboard video: {video}")
    return float(completed.stdout.strip())


def _has_audio(ffprobe: str, video: Path) -> bool:
    completed = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=index",
            "-of",
            "csv=p=0",
            str(video),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0 and bool(completed.stdout.strip())


def add_fixed_narration(
    storyboard_dir: str | Path,
    *,
    timed_script: str,
    voice: str,
    rate: int,
    keep_centre_cancelled_ambience: bool,
    output_root: Path,
) -> NarrationResult:
    """Replace per-shot voices with one macOS TTS voice over a full storyboard."""

    say = shutil.which("say")
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not say or not ffmpeg or not ffprobe:
        raise FileNotFoundError("macOS say, FFmpeg, and FFprobe are required.")
    if not 80 <= rate <= 320:
        raise ValueError("Speech rate must be between 80 and 320 words per minute.")

    output_root = output_root.resolve()
    source_dir = Path(storyboard_dir).expanduser().resolve()
    allowed = (output_root / "h3-storyboards").resolve()
    if not _inside(source_dir, allowed):
        raise ValueError(f"Storyboard directory must be inside {allowed}: {source_dir}")
    video = source_dir / "result.mp4"
    if not video.is_file() or video.stat().st_size == 0:
        raise FileNotFoundError(f"Storyboard result not found: {video}")
    cues = parse_timed_script(timed_script)
    duration = _video_duration(ffprobe, video)
    if cues[-1].start_seconds >= duration:
        raise ValueError("The last narration cue starts after the video ends.")

    stat = video.stat()
    payload = {
        "video": str(video),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "script": timed_script,
        "voice": voice,
        "rate": rate,
        "ambience": keep_centre_cancelled_ambience,
    }
    narration_id = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:20]
    project_dir = output_root / "h3-narration" / narration_id
    project_dir.mkdir(parents=True, exist_ok=True)
    output = project_dir / "result.mp4"
    if output.is_file() and output.stat().st_size > 0:
        return NarrationResult(narration_id, output, project_dir, True)

    voice_files: list[Path] = []
    for index, cue in enumerate(cues, start=1):
        path = project_dir / f"voice-{index:02d}.aiff"
        completed = subprocess.run(
            [say, "-v", voice, "-r", str(rate), "-o", str(path), cue.text],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0 or not path.is_file() or path.stat().st_size <= 4096:
            raise RuntimeError(
                f"macOS could not generate narration cue {index}: {completed.stderr.strip()}"
            )
        voice_files.append(path)

    filter_parts: list[str] = []
    if keep_centre_cancelled_ambience and _has_audio(ffprobe, video):
        filter_parts.append(
            "[0:a]aresample=48000,"
            "pan=stereo|c0=0.5*c0-0.5*c1|c1=0.5*c1-0.5*c0,"
            "volume=0.32,highpass=f=100,lowpass=f=10000[amb]"
        )
    else:
        filter_parts.append(f"anullsrc=r=48000:cl=stereo:d={duration:.6f}[amb]")
    mix_inputs = ["[amb]"]
    for index, cue in enumerate(cues, start=1):
        delay_ms = round(cue.start_seconds * 1000)
        filter_parts.append(
            f"[{index}:a]aresample=48000,aformat=channel_layouts=stereo,"
            f"loudnorm=I=-17:TP=-2:LRA=7,adelay={delay_ms}|{delay_ms},"
            f"apad,atrim=0:{duration:.6f}[voice{index}]"
        )
        mix_inputs.append(f"[voice{index}]")
    filter_parts.append(
        "".join(mix_inputs)
        + f"amix=inputs={len(mix_inputs)}:normalize=0,"
        + f"alimiter=limit=0.95,atrim=0:{duration:.6f}[aout]"
    )

    partial = project_dir / "result.partial.mp4"
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video)]
    for voice_file in voice_files:
        command.extend(["-i", str(voice_file)])
    command.extend(
        [
            "-filter_complex",
            ";".join(filter_parts),
            "-map",
            "0:v:0",
            "-map",
            "[aout]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(partial),
        ]
    )
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    (project_dir / "ffmpeg.log").write_text(completed.stderr, encoding="utf-8")
    (project_dir / "narration.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if completed.returncode != 0 or not partial.is_file() or partial.stat().st_size == 0:
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"FFmpeg narration mix failed. See {project_dir / 'ffmpeg.log'}")
    partial.replace(output)
    return NarrationResult(narration_id, output, project_dir, False)
