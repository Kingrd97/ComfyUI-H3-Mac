"""Backend-neutral bridge used by the ComfyUI nodes."""

from .config import BridgeConfig, load_config
from .models import H3Reference, H3Request, H3Result
from .runner import H3Runner
from .storyboard import StoryboardResult, assemble_storyboard, build_shot_prompt

__all__ = [
    "BridgeConfig",
    "H3Reference",
    "H3Request",
    "H3Result",
    "H3Runner",
    "StoryboardResult",
    "assemble_storyboard",
    "build_shot_prompt",
    "load_config",
]
