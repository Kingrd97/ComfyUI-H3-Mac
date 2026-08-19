from __future__ import annotations

import json

from h3_bridge.config import load_config


def test_adaptive_defaults_keep_progressing_in_background(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "h3_binary": "runtime/h3.c/h3",
                "model_root": "runtime/models/MiniMax-H3",
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.auto_active_behavior == "background"
    assert config.auto_idle_seconds == 300.0
    assert config.auto_ssd_streaming_ram_gib == 64
