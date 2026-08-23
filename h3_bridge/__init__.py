"""Backend-neutral bridge used by the ComfyUI nodes."""

from .config import BridgeConfig, load_config
from .models import H3Reference, H3Request, H3Result
from .narration import (
    NarrationCue,
    NarrationResult,
    add_fixed_narration,
    parse_timed_script,
)
from .runner import H3Runner
from .storyboard import StoryboardResult, assemble_storyboard, build_shot_prompt
from .vpipe import (
    VPipeConfig,
    VPipeRequest,
    VPipeResult,
    VPipeRunner,
    load_vpipe_config,
)

__all__ = [
    "BridgeConfig",
    "H3Reference",
    "H3Request",
    "H3Result",
    "H3Runner",
    "NarrationCue",
    "NarrationResult",
    "StoryboardResult",
    "assemble_storyboard",
    "build_shot_prompt",
    "load_config",
    "add_fixed_narration",
    "parse_timed_script",
    "VPipeConfig",
    "VPipeRequest",
    "VPipeResult",
    "VPipeRunner",
    "load_vpipe_config",
]
