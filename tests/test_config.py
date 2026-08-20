from __future__ import annotations

import json

import pytest

from h3_bridge.config import load_config


def write_config(tmp_path, **overrides):
    config_path = tmp_path / "config.json"
    payload = {
        "h3_binary": "runtime/h3.c/h3",
        "model_root": "runtime/models/MiniMax-H3",
    }
    payload.update(overrides)
    config_path.write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    return config_path


def test_adaptive_defaults_include_debounced_jank_thresholds(tmp_path):
    config_path = write_config(tmp_path)

    config = load_config(config_path)

    assert config.auto_active_behavior == "adaptive"
    assert config.auto_idle_seconds == 300.0
    assert config.auto_poll_seconds == 0.5
    assert config.auto_metrics_poll_seconds == 2.0
    assert config.auto_health_poll_seconds == 10.0
    assert config.auto_status_interval_seconds == 15.0
    assert config.auto_max_external_cpu_percent == 120.0
    assert config.auto_jank_interaction_seconds == 5.0
    assert config.auto_jank_pause_seconds == 2.0
    assert config.auto_jank_recover_seconds == 15.0
    assert config.auto_jank_probe_seconds == 20.0
    assert config.auto_jank_cpu_percent == 300.0
    assert config.auto_jank_window_server_percent == 80.0
    assert config.auto_jank_window_server_recover_percent == 50.0
    assert config.auto_jank_gpu_percent == 92.0
    assert config.auto_jank_gpu_recover_percent == 70.0
    assert config.auto_memory_pause_percent == 8.0
    assert config.auto_memory_recover_percent == 15.0
    assert config.auto_swap_growth_pause_mib_per_minute == 512.0
    assert config.auto_pageout_pause_mib_per_minute == 256.0
    assert config.auto_ssd_streaming_ram_gib == 64
    assert config.expected_model_revision == "42ed227ee7df40d41602854ae760620d6eb651fe"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"auto_active_behavior": "unknown"}, "auto_active_behavior"),
        ({"auto_poll_seconds": 0}, "polling intervals"),
        ({"auto_metrics_poll_seconds": 0}, "polling intervals"),
        ({"auto_health_poll_seconds": 0}, "polling intervals"),
        ({"auto_status_interval_seconds": 0}, "polling intervals"),
        ({"auto_jank_pause_seconds": -0.1}, "timing values"),
        ({"auto_jank_recover_seconds": -0.1}, "timing values"),
        ({"auto_jank_probe_seconds": -0.1}, "timing values"),
        (
            {
                "auto_max_external_cpu_percent": 120,
                "auto_jank_cpu_percent": 120,
            },
            "must exceed",
        ),
        (
            {
                "auto_jank_window_server_percent": 80,
                "auto_jank_window_server_recover_percent": 80,
            },
            "recovery threshold",
        ),
        ({"auto_jank_window_server_percent": 1001}, "between 0 and 1000"),
        (
            {
                "auto_jank_gpu_percent": 92,
                "auto_jank_gpu_recover_percent": 92,
            },
            "GPU recovery threshold",
        ),
        ({"auto_jank_gpu_percent": 101}, "GPU recovery threshold"),
        (
            {
                "auto_memory_pause_percent": 15,
                "auto_memory_recover_percent": 15,
            },
            "Memory recovery threshold",
        ),
        ({"auto_swap_growth_pause_mib_per_minute": 0}, "must be positive"),
        ({"auto_pageout_pause_mib_per_minute": 0}, "must be positive"),
        ({"output_subdir": "../../outside"}, "safe path component"),
        ({"auto_poll_seconds": float("nan")}, "finite numbers"),
        ({"auto_jank_cpu_percent": float("inf")}, "finite numbers"),
    ],
)
def test_invalid_adaptive_thresholds_are_rejected(tmp_path, overrides, message):
    config_path = write_config(tmp_path, **overrides)

    with pytest.raises(ValueError, match=message):
        load_config(config_path)
