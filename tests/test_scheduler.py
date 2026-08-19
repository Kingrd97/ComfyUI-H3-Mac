from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from h3_bridge.config import BridgeConfig
from h3_bridge.scheduler import AdaptiveScheduler


def config(tmp_path: Path, behavior: str = "pause") -> BridgeConfig:
    return BridgeConfig(
        project_root=tmp_path,
        h3_binary=tmp_path / "h3",
        model_root=tmp_path / "models",
        auto_idle_seconds=60,
        auto_poll_seconds=2,
        auto_max_external_cpu_percent=120,
        auto_active_behavior=behavior,
        auto_require_ac_power=True,
    )


def test_auto_pauses_for_user_then_resumes_at_full_policy(tmp_path: Path):
    idle = [3.0]
    now = [0.0]
    signals: list[signal.Signals] = []
    policies: list[bool] = []
    scheduler = AdaptiveScheduler(
        tmp_path,
        4242,
        "auto",
        config(tmp_path),
        clock=lambda: now[0],
        idle_probe=lambda: idle[0],
        cpu_probe=lambda _pgid: 10.0,
        power_probe=lambda: True,
    )
    with patch(
        "h3_bridge.scheduler.signal_process_group",
        side_effect=lambda _pgid, selected: signals.append(selected) or True,
    ), patch(
        "h3_bridge.scheduler.set_process_group_background",
        side_effect=lambda _pgid, background: policies.append(background),
    ):
        scheduler.start()
        assert scheduler.tick().reason == "user-active"
        idle[0] = 90.0
        now[0] = 3.0
        decision = scheduler.tick()

    assert decision.reason == "idle-boost"
    assert signals == [signal.SIGSTOP, signal.SIGCONT]
    assert policies == [False]
    status = json.loads((tmp_path / "process.json").read_text(encoding="utf-8"))
    assert status["state"] == "running"
    assert status["background"] is False


def test_auto_stays_paused_for_external_cpu_or_battery(tmp_path: Path):
    scheduler = AdaptiveScheduler(
        tmp_path,
        4242,
        "auto",
        config(tmp_path),
        idle_probe=lambda: 90.0,
        cpu_probe=lambda _pgid: 180.0,
        power_probe=lambda: True,
    )
    with patch("h3_bridge.scheduler.signal_process_group", return_value=True):
        scheduler.start()
    assert scheduler.tick().reason == "foreground-cpu"

    battery_dir = tmp_path / "battery"
    battery_dir.mkdir()
    battery = AdaptiveScheduler(
        battery_dir,
        4343,
        "auto",
        config(tmp_path),
        idle_probe=lambda: 90.0,
        cpu_probe=lambda _pgid: 0.0,
        power_probe=lambda: False,
    )
    with patch("h3_bridge.scheduler.signal_process_group", return_value=True):
        battery.start()
    assert battery.tick().reason == "battery"


def test_manual_pause_overrides_max_and_can_resume(tmp_path: Path):
    signals: list[signal.Signals] = []
    scheduler = AdaptiveScheduler(tmp_path, 4242, "max", config(tmp_path))
    with patch(
        "h3_bridge.scheduler.signal_process_group",
        side_effect=lambda _pgid, selected: signals.append(selected) or True,
    ), patch("h3_bridge.scheduler.set_process_group_background"):
        scheduler.start()
        control = json.loads((tmp_path / "control.json").read_text(encoding="utf-8"))
        control["paused"] = True
        (tmp_path / "control.json").write_text(json.dumps(control), encoding="utf-8")
        scheduler.tick(force=True)
        control["paused"] = False
        (tmp_path / "control.json").write_text(json.dumps(control), encoding="utf-8")
        scheduler.tick(force=True)

    assert signals == [signal.SIGSTOP, signal.SIGCONT]


def test_background_behavior_keeps_working_while_user_is_active(tmp_path: Path):
    scheduler = AdaptiveScheduler(
        tmp_path,
        4242,
        "auto",
        config(tmp_path, "background"),
        idle_probe=lambda: 1.0,
        cpu_probe=lambda _pgid: 0.0,
        power_probe=lambda: True,
    )
    with patch("h3_bridge.scheduler.signal_process_group") as send_signal, patch(
        "h3_bridge.scheduler.set_process_group_background"
    ) as set_background:
        scheduler.start()
    send_signal.assert_not_called()
    set_background.assert_called_once_with(4242, True)


def test_real_process_group_can_pause_and_continue(tmp_path: Path):
    idle = [1.0]
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    scheduler = AdaptiveScheduler(
        tmp_path,
        child.pid,
        "auto",
        config(tmp_path),
        idle_probe=lambda: idle[0],
        cpu_probe=lambda _pgid: 0.0,
        power_probe=lambda: True,
    )
    try:
        with patch("h3_bridge.scheduler.set_process_group_background"):
            scheduler.start()
            paused = json.loads((tmp_path / "process.json").read_text(encoding="utf-8"))
            assert paused["state"] == "paused"
            assert child.poll() is None

            idle[0] = 90.0
            scheduler.tick(force=True)
            resumed = json.loads((tmp_path / "process.json").read_text(encoding="utf-8"))
            assert resumed["state"] == "running"
            assert child.poll() is None
    finally:
        os.killpg(child.pid, signal.SIGCONT)
        os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=5)
