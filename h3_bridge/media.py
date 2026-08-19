from __future__ import annotations

import hashlib
import wave
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:20]


def save_image_tensor(image: Any, asset_dir: Path) -> Path:
    """Persist the first ComfyUI IMAGE tensor as a stable PNG asset."""
    asset_dir.mkdir(parents=True, exist_ok=True)
    array = image.detach().cpu().numpy() if hasattr(image, "detach") else np.asarray(image)
    if array.ndim == 4:
        array = array[0]
    array = np.clip(array * 255.0, 0, 255).astype(np.uint8)
    payload = array.tobytes()
    path = asset_dir / f"image-{_digest(payload)}.png"
    if not path.exists():
        Image.fromarray(array).save(path, format="PNG")
    return path


def save_audio(audio: dict[str, Any], asset_dir: Path) -> Path:
    """Persist a ComfyUI AUDIO value as PCM16 WAV."""
    asset_dir.mkdir(parents=True, exist_ok=True)
    waveform = audio["waveform"]
    sample_rate = int(audio["sample_rate"])
    array = waveform.detach().cpu().numpy() if hasattr(waveform, "detach") else np.asarray(waveform)
    if array.ndim == 3:
        array = array[0]
    if array.ndim == 1:
        array = array[np.newaxis, :]
    pcm = (np.clip(array, -1.0, 1.0).T * 32767.0).astype("<i2")
    payload = pcm.tobytes()
    path = asset_dir / f"audio-{_digest(payload)}.wav"
    if not path.exists():
        with wave.open(str(path), "wb") as stream:
            stream.setnchannels(pcm.shape[1])
            stream.setsampwidth(2)
            stream.setframerate(sample_rate)
            stream.writeframes(payload)
    return path


def validated_media_path(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Reference file not found: {path}")
    return path
