from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from h3_bridge.config import BridgeConfig
from h3_bridge.scheduler import (
    AdaptiveScheduler,
    GuardianSnapshot,
    NativeGuardian,
    ResourceHealth,
    SystemLoad,
    resource_health,
)


def config(tmp_path: Path, behavior: str = "adaptive") -> BridgeConfig:
    return BridgeConfig(
        project_root=tmp_path,
        h3_binary=tmp_path / "h3",
        model_root=tmp_path / "models",
        auto_idle_seconds=300,
        auto_poll_seconds=0.5,
        auto_metrics_poll_seconds=2,
        auto_max_external_cpu_percent=120,
        auto_active_behavior=behavior,
        auto_require_ac_power=True,
        auto_jank_interaction_seconds=5,
        auto_jank_pause_seconds=2,
        auto_jank_recover_seconds=15,
        auto_jank_probe_seconds=20,
        auto_jank_cpu_percent=300,
        auto_jank_window_server_percent=80,
        auto_jank_window_server_recover_percent=50,
        auto_jank_gpu_percent=92,
        auto_jank_gpu_recover_percent=70,
    )


def guardian(
    *,
    idle: float = 1.0,
    frame_age_ms: float | None = 12.0,
    display_link_p95_ms: float | None = 16.7,
    display_link_max_gap_ms: float | None = 16.7,
    display_link_callback_age_ms: float | None = 1.0,
    frame_stalled: bool = False,
    bundle_id: str | None = "com.example.Editor",
    thermal_state: str | None = "nominal",
    low_power_mode_enabled: bool | None = False,
) -> GuardianSnapshot:
    return GuardianSnapshot(
        input_idle_seconds=idle,
        frame_age_ms=frame_age_ms,
        maximum_refresh_interval_ms=16.7,
        display_link_p95_ms=display_link_p95_ms,
        frontmost_bundle_id=bundle_id,
        display_link_max_gap_ms=display_link_max_gap_ms,
        display_link_callback_age_ms=display_link_callback_age_ms,
        frame_stalled=frame_stalled,
        thermal_state=thermal_state,
        low_power_mode_enabled=low_power_mode_enabled,
    )


def test_native_guardian_uses_display_cadence_not_static_frame_age(tmp_path: Path):
    native = NativeGuardian(tmp_path / "missing", interaction_seconds=5.0)
    static_payload = {
        "input_idle_seconds": 0.2,
        "frame_age_ms": 5_000.0,
        "maximum_refresh_interval_ms": 41.7,
        "display_link_p95_ms": 16.7,
        "display_link_max_gap_ms": 16.7,
        "display_link_callback_age_ms": 2.0,
    }

    assert not native._snapshot_from_payload(static_payload).frame_stalled
    assert not native._snapshot_from_payload(static_payload).frame_stalled

    delayed_payload = dict(static_payload)
    delayed_payload["display_link_max_gap_ms"] = 150.0
    assert not native._snapshot_from_payload(delayed_payload).frame_stalled
    assert native._snapshot_from_payload(delayed_payload).frame_stalled


def test_native_guardian_honors_configured_interaction_window(tmp_path: Path):
    native = NativeGuardian(tmp_path / "missing", interaction_seconds=0.5)
    payload = {
        "input_idle_seconds": 1.0,
        "maximum_refresh_interval_ms": 16.7,
        "display_link_max_gap_ms": 200.0,
        "display_link_callback_age_ms": 200.0,
    }

    assert not native._snapshot_from_payload(payload).frame_stalled
    assert not native._snapshot_from_payload(payload).frame_stalled


def test_native_guardian_discards_backlogged_samples(tmp_path: Path):
    now = [100.0]
    native = NativeGuardian(
        tmp_path / "missing",
        interaction_seconds=5.0,
        clock=lambda: now[0],
    )
    stale = {
        "sample_uptime": 97.0,
        "input_idle_seconds": 0.1,
        "maximum_refresh_interval_ms": 16.7,
        "display_link_max_gap_ms": 200.0,
        "display_link_callback_age_ms": 200.0,
    }

    assert native._snapshot_from_payload(stale) is None
    future = dict(stale, sample_uptime=102.0)
    assert native._snapshot_from_payload(future) is None


def test_native_guardian_silence_breaks_stall_consecutiveness(tmp_path: Path):
    native = NativeGuardian(tmp_path / "missing", interaction_seconds=5.0)
    delayed = {
        "input_idle_seconds": 0.1,
        "maximum_refresh_interval_ms": 16.7,
        "display_link_max_gap_ms": 200.0,
        "display_link_callback_age_ms": 200.0,
    }
    assert not native._snapshot_from_payload(delayed).frame_stalled

    process = MagicMock()
    process.poll.return_value = None
    process.stdout.fileno.return_value = 123
    native.process = process
    with patch("h3_bridge.scheduler.os.read", side_effect=BlockingIOError):
        assert native.poll() is None

    assert not native._snapshot_from_payload(delayed).frame_stalled


def test_native_guardian_parses_batched_ndjson(tmp_path: Path):
    native = NativeGuardian(tmp_path / "missing", interaction_seconds=5.0)
    delayed = {
        "input_idle_seconds": 0.1,
        "maximum_refresh_interval_ms": 16.7,
        "display_link_max_gap_ms": 200.0,
        "display_link_callback_age_ms": 200.0,
    }
    process = MagicMock()
    process.poll.return_value = None
    process.stdout.fileno.return_value = 123
    native.process = process
    payload = (json.dumps(delayed) + "\n" + json.dumps(delayed) + "\n").encode()
    with patch(
        "h3_bridge.scheduler.os.read",
        side_effect=[payload, BlockingIOError()],
    ):
        sample = native.poll()

    assert sample is not None
    assert sample.frame_stalled


def test_native_guardian_parses_thermal_and_low_power_state(tmp_path: Path):
    native = NativeGuardian(tmp_path / "missing")
    sample = native._snapshot_from_payload(
        {
            "input_idle_seconds": 1,
            "thermal_state": "serious",
            "low_power_mode_enabled": True,
        }
    )

    assert sample is not None
    assert sample.thermal_state == "serious"
    assert sample.low_power_mode_enabled is True


def test_native_guardian_restarts_after_helper_exit(tmp_path: Path):
    binary = tmp_path / "h3-guardian"
    binary.write_text("", encoding="utf-8")
    binary.chmod(0o755)
    native = NativeGuardian(binary, clock=lambda: 10.0)
    dead = MagicMock()
    dead.poll.return_value = 1
    replacement = MagicMock()
    replacement.poll.return_value = None
    replacement.stdout.fileno.return_value = 123
    native.process = dead

    with patch("h3_bridge.scheduler.platform.system", return_value="Darwin"), patch(
        "h3_bridge.scheduler.subprocess.Popen", return_value=replacement
    ) as launch, patch("h3_bridge.scheduler.os.set_blocking"):
        assert native.poll() is None

    launch.assert_called_once()
    assert native.process is replacement


def test_resource_health_parses_partial_public_tool_output():
    outputs = {
        "/usr/bin/memory_pressure": (
            "The system has 1 pages.\nSystem-wide memory free percentage: 7%\n"
        ),
        "/usr/sbin/sysctl": "total = 2048.00M  used = 1536.50M  free = 511.50M\n",
        "/usr/bin/vm_stat": (
            "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
            "Pageouts: 1024.\n"
        ),
    }

    def run(command, **_kwargs):
        return subprocess.CompletedProcess(command, 0, outputs[command[0]], "")

    with patch("h3_bridge.scheduler.platform.system", return_value="Darwin"), patch(
        "h3_bridge.scheduler.subprocess.run", side_effect=run
    ):
        health = resource_health()

    assert health.memory_free_percent == 7.0
    assert health.swap_used_bytes == int(1536.5 * 1024 * 1024)
    assert health.pageout_bytes == 1024 * 16384


def adaptive_scheduler(
    tmp_path: Path,
    now: list[float],
    load: list[SystemLoad],
    gpu: list[float | None],
    native: list[GuardianSnapshot | None],
) -> AdaptiveScheduler:
    return AdaptiveScheduler(
        tmp_path,
        4242,
        "auto",
        config(tmp_path),
        clock=lambda: now[0],
        idle_probe=lambda: 1.0,
        load_probe=lambda _pgid: load[0],
        gpu_probe=lambda: gpu[0],
        power_probe=lambda: True,
        health_probe=lambda: ResourceHealth(
            memory_free_percent=50.0,
            swap_used_bytes=0,
            pageout_bytes=0,
        ),
        jank_probe=lambda: native[0],
    )


def test_auto_backgrounds_for_user_then_boosts_at_idle(tmp_path: Path):
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
        idle[0] = 360.0
        now[0] = 3.0
        decision = scheduler.tick()

    assert decision.reason == "idle-boost"
    assert signals == []
    assert policies == [True, False]
    status = json.loads((tmp_path / "process.json").read_text(encoding="utf-8"))
    assert status["state"] == "running"
    assert status["background"] is False


def test_auto_backgrounds_for_external_cpu_or_battery(tmp_path: Path):
    scheduler = AdaptiveScheduler(
        tmp_path,
        4242,
        "auto",
        config(tmp_path),
        idle_probe=lambda: 360.0,
        cpu_probe=lambda _pgid: 180.0,
        power_probe=lambda: True,
    )
    with patch("h3_bridge.scheduler.signal_process_group", return_value=True):
        scheduler.start()
    cpu_decision = scheduler.tick()
    assert cpu_decision.reason == "foreground-cpu"
    assert not cpu_decision.paused
    assert cpu_decision.background

    battery_dir = tmp_path / "battery"
    battery_dir.mkdir()
    battery = AdaptiveScheduler(
        battery_dir,
        4343,
        "auto",
        config(tmp_path),
        idle_probe=lambda: 360.0,
        cpu_probe=lambda _pgid: 0.0,
        power_probe=lambda: False,
    )
    with patch("h3_bridge.scheduler.signal_process_group", return_value=True):
        battery.start()
    battery_decision = battery.tick()
    assert battery_decision.reason == "battery"
    assert not battery_decision.paused
    assert battery_decision.background


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


def test_process_birth_fingerprint_retries_after_transient_ps_failure(tmp_path: Path):
    engine_signatures = iter(["", "birth-fingerprint"])

    def signature(pid: int) -> str:
        return next(engine_signatures) if pid == 4242 else "controller-birth"

    with patch(
        "h3_bridge.scheduler.process_start_signature",
        side_effect=signature,
    ), patch("h3_bridge.scheduler.signal_process_group"), patch(
        "h3_bridge.scheduler.set_process_group_background"
    ):
        scheduler = AdaptiveScheduler(tmp_path, 4242, "max", config(tmp_path))
        scheduler.start()

    status = json.loads((tmp_path / "process.json").read_text(encoding="utf-8"))
    assert status["process_start_signature"] == "birth-fingerprint"


def test_manual_pause_skips_heavy_system_probes(tmp_path: Path):
    now = [0.0]
    scheduler = AdaptiveScheduler(
        tmp_path,
        4242,
        "auto",
        config(tmp_path),
        clock=lambda: now[0],
        idle_probe=lambda: 1.0,
        load_probe=lambda _pgid: SystemLoad(10.0, 10.0),
        gpu_probe=lambda: 10.0,
        power_probe=lambda: True,
        jank_probe=lambda: guardian(),
    )
    with patch("h3_bridge.scheduler.signal_process_group", return_value=True), patch(
        "h3_bridge.scheduler.set_process_group_background"
    ):
        scheduler.start()
        control = json.loads((tmp_path / "control.json").read_text(encoding="utf-8"))
        control["paused"] = True
        (tmp_path / "control.json").write_text(json.dumps(control), encoding="utf-8")
        scheduler.load_probe = load_probe_mock = MagicMock()
        scheduler.gpu_probe = gpu_probe_mock = MagicMock()
        scheduler.power_probe = power_probe_mock = MagicMock()
        now[0] = 0.5
        decision = scheduler.tick(force=True)

    assert decision.paused
    load_probe_mock.assert_not_called()
    gpu_probe_mock.assert_not_called()
    power_probe_mock.assert_not_called()


def test_missing_guardian_caches_ioreg_idle_fallback(tmp_path: Path):
    now = [0.0]
    idle_probe = MagicMock(return_value=1.0)
    scheduler = AdaptiveScheduler(
        tmp_path,
        4242,
        "auto",
        config(tmp_path),
        clock=lambda: now[0],
        idle_probe=idle_probe,
        load_probe=lambda _pgid: SystemLoad(10.0, 10.0),
        gpu_probe=lambda: 10.0,
        power_probe=lambda: True,
        jank_probe=lambda: None,
    )
    with patch("h3_bridge.scheduler.signal_process_group"), patch(
        "h3_bridge.scheduler.set_process_group_background"
    ):
        scheduler.start()
        now[0] = 0.5
        scheduler.tick()
        now[0] = 2.0
        scheduler.tick()

    assert idle_probe.call_count == 2


def test_external_resume_is_reconciled_before_adaptive_repause(tmp_path: Path):
    now = [0.0]
    load = [SystemLoad(310.0, 10.0)]
    native = [guardian()]
    signals: list[signal.Signals] = []
    scheduler = adaptive_scheduler(tmp_path, now, load, [10.0], native)

    with patch(
        "h3_bridge.scheduler.signal_process_group",
        side_effect=lambda _pgid, selected: signals.append(selected) or True,
    ), patch("h3_bridge.scheduler.set_process_group_background"), patch(
        "h3_bridge.scheduler.process_group_stopped",
        side_effect=[None, False],
    ):
        scheduler.start()
        now[0] = 2.0
        assert scheduler.tick(force=True).paused

        control = json.loads((tmp_path / "control.json").read_text(encoding="utf-8"))
        control["control_generation"] = 123
        control["paused"] = False
        (tmp_path / "control.json").write_text(json.dumps(control), encoding="utf-8")
        now[0] = 2.5
        decision = scheduler.tick(force=True)

    assert decision.paused
    # The CLI's direct CONT made the process runnable; auto immediately
    # observes that state and reapplies its still-active pressure pause.
    assert signals == [signal.SIGSTOP, signal.SIGSTOP]


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


@pytest.mark.parametrize(
    ("spike_load", "spike_gpu"),
    [
        (SystemLoad(10.0, 90.0), 95.0),
        (SystemLoad(310.0, 10.0), 10.0),
    ],
)
def test_adaptive_short_contention_spikes_are_debounced(
    tmp_path: Path,
    spike_load: SystemLoad,
    spike_gpu: float,
):
    now = [0.0]
    load = [spike_load]
    gpu = [spike_gpu]
    native = [guardian()]
    signals: list[signal.Signals] = []
    scheduler = adaptive_scheduler(tmp_path, now, load, gpu, native)

    with patch(
        "h3_bridge.scheduler.signal_process_group",
        side_effect=lambda _pgid, selected: signals.append(selected) or True,
    ), patch("h3_bridge.scheduler.set_process_group_background"):
        scheduler.start()
        now[0] = 1.0
        assert not scheduler.tick(force=True).paused

        load[0] = SystemLoad(10.0, 20.0)
        gpu[0] = 20.0
        now[0] = 1.1
        assert not scheduler.tick(force=True).paused

        load[0] = spike_load
        gpu[0] = spike_gpu
        now[0] = 2.0
        assert not scheduler.tick(force=True).paused
        now[0] = 3.9
        decision = scheduler.tick(force=True)

    assert not decision.paused
    assert decision.adaptive_phase == "background"
    assert signal.SIGSTOP not in signals


def test_pause_debounce_waits_for_a_fresh_metrics_sample(tmp_path: Path):
    now = [0.0]
    load = [SystemLoad(310.0, 10.0)]
    gpu = [10.0]
    native = [guardian()]
    selected_config = replace(config(tmp_path), auto_metrics_poll_seconds=10.0)
    scheduler = AdaptiveScheduler(
        tmp_path,
        4242,
        "auto",
        selected_config,
        clock=lambda: now[0],
        load_probe=lambda _pgid: load[0],
        gpu_probe=lambda: gpu[0],
        power_probe=lambda: True,
        jank_probe=lambda: native[0],
    )
    with patch("h3_bridge.scheduler.signal_process_group", return_value=True), patch(
        "h3_bridge.scheduler.set_process_group_background"
    ):
        scheduler.start()
        now[0] = 2.0
        assert not scheduler.tick().paused
        now[0] = 10.0
        assert scheduler.tick().paused


def test_recovered_load_clears_pressure_on_next_fresh_sample(tmp_path: Path):
    now = [0.0]
    load = [SystemLoad(310.0, 10.0)]
    selected_config = replace(config(tmp_path), auto_metrics_poll_seconds=10.0)
    scheduler = AdaptiveScheduler(
        tmp_path,
        4242,
        "auto",
        selected_config,
        clock=lambda: now[0],
        load_probe=lambda _pgid: load[0],
        gpu_probe=lambda: 10.0,
        power_probe=lambda: True,
        jank_probe=lambda: guardian(),
    )
    with patch("h3_bridge.scheduler.signal_process_group", return_value=True), patch(
        "h3_bridge.scheduler.set_process_group_background"
    ):
        scheduler.start()
        load[0] = SystemLoad(10.0, 10.0)
        now[0] = 2.0
        assert not scheduler.tick().paused
        now[0] = 10.0
        decision = scheduler.tick()

    assert not decision.paused
    assert decision.adaptive_phase == "background"


def test_idle_boost_requires_fresh_low_pressure_metrics(tmp_path: Path):
    now = [0.0]
    native = [guardian(idle=299.0)]
    load = [SystemLoad(10.0, 10.0)]
    selected_config = replace(config(tmp_path), auto_metrics_poll_seconds=10.0)
    scheduler = AdaptiveScheduler(
        tmp_path,
        4242,
        "auto",
        selected_config,
        clock=lambda: now[0],
        load_probe=lambda _pgid: load[0],
        gpu_probe=lambda: 10.0,
        power_probe=lambda: True,
        jank_probe=lambda: native[0],
    )
    with patch("h3_bridge.scheduler.signal_process_group"), patch(
        "h3_bridge.scheduler.set_process_group_background"
    ):
        scheduler.start()
        native[0] = guardian(idle=360.0)
        load[0] = SystemLoad(310.0, 90.0)
        now[0] = 1.0
        cached = scheduler.tick()
        now[0] = 10.0
        fresh = scheduler.tick()

    assert cached.background
    assert cached.adaptive_phase == "background"
    assert fresh.background
    assert fresh.adaptive_phase == "background"


def test_idle_boost_is_blocked_by_display_link_delay(tmp_path: Path):
    now = [0.0]
    native = [guardian(idle=360.0, display_link_p95_ms=130.0)]
    scheduler = adaptive_scheduler(
        tmp_path,
        now,
        [SystemLoad(10.0, 10.0)],
        [10.0],
        native,
    )
    with patch("h3_bridge.scheduler.signal_process_group"), patch(
        "h3_bridge.scheduler.set_process_group_background"
    ):
        scheduler.start()

    assert scheduler.tick().background
    assert scheduler.tick().adaptive_phase == "background"


def test_idle_boost_waits_for_window_server_to_settle(tmp_path: Path):
    now = [0.0]
    load = [SystemLoad(10.0, 90.0)]
    gpu = [95.0]
    native = [guardian(idle=360.0)]
    scheduler = adaptive_scheduler(tmp_path, now, load, gpu, native)

    with patch("h3_bridge.scheduler.signal_process_group"), patch(
        "h3_bridge.scheduler.set_process_group_background"
    ):
        scheduler.start()

    assert scheduler.tick().background
    assert scheduler.tick().adaptive_phase == "background"


@pytest.mark.parametrize(
    ("initial_load", "initial_gpu", "expected_reason"),
    [
        (SystemLoad(10.0, 90.0), 95.0, "display-contention"),
        (SystemLoad(310.0, 10.0), 10.0, "external-cpu-jank"),
    ],
)
def test_sustained_contention_pauses_after_two_seconds(
    tmp_path: Path,
    initial_load: SystemLoad,
    initial_gpu: float,
    expected_reason: str,
):
    now = [0.0]
    load = [initial_load]
    gpu = [initial_gpu]
    native = [guardian()]
    signals: list[signal.Signals] = []
    scheduler = adaptive_scheduler(tmp_path, now, load, gpu, native)

    with patch(
        "h3_bridge.scheduler.signal_process_group",
        side_effect=lambda _pgid, selected: signals.append(selected) or True,
    ), patch("h3_bridge.scheduler.set_process_group_background"):
        scheduler.start()
        now[0] = 1.9
        assert not scheduler.tick(force=True).paused
        now[0] = 2.0
        decision = scheduler.tick(force=True)
        now[0] = 3.0
        repeated = scheduler.tick(force=True)

    assert decision.paused
    assert repeated.paused
    assert decision.reason == expected_reason
    assert decision.adaptive_phase == "paused"
    assert signals == [signal.SIGSTOP]


def test_native_frame_stall_pauses_without_waiting_for_debounce(tmp_path: Path):
    now = [0.0]
    load = [SystemLoad(10.0, 10.0)]
    gpu = [10.0]
    native = [guardian()]
    signals: list[signal.Signals] = []
    scheduler = adaptive_scheduler(tmp_path, now, load, gpu, native)

    with patch(
        "h3_bridge.scheduler.signal_process_group",
        side_effect=lambda _pgid, selected: signals.append(selected) or True,
    ), patch("h3_bridge.scheduler.set_process_group_background"):
        scheduler.start()
        scheduler.load_probe = load_probe = MagicMock()
        scheduler.gpu_probe = gpu_probe = MagicMock()
        scheduler.power_probe = power_probe = MagicMock()
        native[0] = guardian(
            frame_age_ms=180.0,
            display_link_p95_ms=130.0,
            frame_stalled=True,
        )
        now[0] = 0.5
        decision = scheduler.tick(force=True)

    assert decision.paused
    assert decision.reason == "frame-stall"
    assert decision.adaptive_phase == "paused"
    assert signals == [signal.SIGSTOP]
    load_probe.assert_not_called()
    gpu_probe.assert_not_called()
    power_probe.assert_not_called()


def test_paused_job_recovers_through_probe_then_returns_to_background(tmp_path: Path):
    now = [0.0]
    load = [SystemLoad(10.0, 90.0)]
    gpu = [95.0]
    native = [guardian()]
    signals: list[signal.Signals] = []
    scheduler = adaptive_scheduler(tmp_path, now, load, gpu, native)

    with patch(
        "h3_bridge.scheduler.signal_process_group",
        side_effect=lambda _pgid, selected: signals.append(selected) or True,
    ), patch("h3_bridge.scheduler.set_process_group_background"):
        scheduler.start()
        now[0] = 2.0
        assert scheduler.tick(force=True).paused

        load[0] = SystemLoad(10.0, 20.0)
        gpu[0] = 20.0
        native[0] = guardian()
        now[0] = 2.5
        assert scheduler.tick(force=True).adaptive_phase == "paused"
        now[0] = 17.4
        assert scheduler.tick(force=True).adaptive_phase == "paused"
        now[0] = 17.5
        probe = scheduler.tick(force=True)

        now[0] = 37.4
        assert scheduler.tick(force=True).adaptive_phase == "probe"
        now[0] = 37.5
        background = scheduler.tick(force=True)

    assert not probe.paused
    assert probe.background
    assert probe.reason == "probe-low"
    assert probe.adaptive_phase == "probe"
    assert not background.paused
    assert background.background
    assert background.adaptive_phase == "background"
    assert signals == [signal.SIGSTOP, signal.SIGCONT]


def test_probe_relapse_repauses_immediately(tmp_path: Path):
    now = [0.0]
    load = [SystemLoad(10.0, 90.0)]
    gpu = [95.0]
    native = [guardian()]
    signals: list[signal.Signals] = []
    scheduler = adaptive_scheduler(tmp_path, now, load, gpu, native)

    with patch(
        "h3_bridge.scheduler.signal_process_group",
        side_effect=lambda _pgid, selected: signals.append(selected) or True,
    ), patch("h3_bridge.scheduler.set_process_group_background"):
        scheduler.start()
        now[0] = 2.0
        assert scheduler.tick(force=True).paused

        load[0] = SystemLoad(10.0, 20.0)
        gpu[0] = 20.0
        now[0] = 2.5
        scheduler.tick(force=True)
        now[0] = 17.5
        assert scheduler.tick(force=True).adaptive_phase == "probe"

        load[0] = SystemLoad(10.0, 90.0)
        gpu[0] = 95.0
        now[0] = 18.0
        relapse = scheduler.tick(force=True)

    assert relapse.paused
    assert relapse.reason == "display-contention"
    assert relapse.adaptive_phase == "paused"
    assert signals == [signal.SIGSTOP, signal.SIGCONT, signal.SIGSTOP]


def test_idle_boost_resumes_adaptive_pause_but_manual_pause_still_wins(
    tmp_path: Path,
):
    now = [0.0]
    load = [SystemLoad(10.0, 90.0)]
    gpu = [95.0]
    native = [guardian()]
    signals: list[signal.Signals] = []
    policies: list[bool] = []
    scheduler = adaptive_scheduler(tmp_path, now, load, gpu, native)

    with patch(
        "h3_bridge.scheduler.signal_process_group",
        side_effect=lambda _pgid, selected: signals.append(selected) or True,
    ), patch(
        "h3_bridge.scheduler.set_process_group_background",
        side_effect=lambda _pgid, background: policies.append(background),
    ):
        scheduler.start()
        now[0] = 2.0
        assert scheduler.tick(force=True).paused

        native[0] = guardian(idle=360.0)
        load[0] = SystemLoad(10.0, 10.0)
        gpu[0] = 95.0
        now[0] = 2.5
        boosted = scheduler.tick(force=True)

        control = json.loads((tmp_path / "control.json").read_text(encoding="utf-8"))
        control["paused"] = True
        (tmp_path / "control.json").write_text(json.dumps(control), encoding="utf-8")
        now[0] = 3.0
        manual = scheduler.tick(force=True)

    assert not boosted.paused
    assert not boosted.background
    assert boosted.reason == "idle-boost"
    assert boosted.adaptive_phase == "idle-max"
    assert manual.paused
    assert manual.reason == "manual"
    assert manual.adaptive_phase == "manual"
    assert signals == [
        signal.SIGSTOP,
        signal.SIGCONT,
        signal.SIGSTOP,
    ]
    assert policies == [True, False]


def test_process_status_contains_guardian_and_contention_diagnostics(tmp_path: Path):
    now = [0.0]
    load = [SystemLoad(42.5, 31.5)]
    gpu = [67.5]
    native = [
        guardian(
            frame_age_ms=23.4,
            display_link_p95_ms=18.7,
            display_link_max_gap_ms=21.2,
            display_link_callback_age_ms=2.3,
            bundle_id="com.example.VideoEditor",
        )
    ]
    scheduler = adaptive_scheduler(tmp_path, now, load, gpu, native)

    with patch("h3_bridge.scheduler.signal_process_group"), patch(
        "h3_bridge.scheduler.set_process_group_background"
    ):
        scheduler.start()

    status = json.loads((tmp_path / "process.json").read_text(encoding="utf-8"))
    assert status["adaptive_phase"] == "background"
    assert status["guardian_available"] is True
    assert status["frame_stalled"] is False
    assert status["frame_age_ms"] == 23.4
    assert status["display_link_p95_ms"] == 18.7
    assert status["display_link_max_gap_ms"] == 21.2
    assert status["display_link_callback_age_ms"] == 2.3
    assert status["frontmost_bundle_id"] == "com.example.VideoEditor"
    assert status["external_cpu_percent"] == 42.5
    assert status["window_server_cpu_percent"] == 31.5
    assert status["gpu_percent"] == 67.5


@pytest.mark.parametrize(
    ("behavior", "expected_paused", "expected_phase"),
    [
        ("background", False, "background"),
        ("pause", True, "legacy-pause"),
    ],
)
def test_legacy_auto_behaviors_ignore_adaptive_guardian_state(
    tmp_path: Path,
    behavior: str,
    expected_paused: bool,
    expected_phase: str,
):
    scheduler = AdaptiveScheduler(
        tmp_path,
        4242,
        "auto",
        config(tmp_path, behavior),
        idle_probe=lambda: 1.0,
        load_probe=lambda _pgid: SystemLoad(500.0, 100.0),
        gpu_probe=lambda: 100.0,
        power_probe=lambda: True,
        jank_probe=lambda: guardian(frame_stalled=True),
    )
    signals: list[signal.Signals] = []

    with patch(
        "h3_bridge.scheduler.signal_process_group",
        side_effect=lambda _pgid, selected: signals.append(selected) or True,
    ), patch("h3_bridge.scheduler.set_process_group_background"):
        scheduler.start()

    decision = scheduler.tick()
    assert decision.paused is expected_paused
    assert decision.adaptive_phase == expected_phase
    assert signals == ([signal.SIGSTOP] if expected_paused else [])


def test_memory_pressure_pauses_immediately_then_recovers_through_probe(
    tmp_path: Path,
):
    now = [0.0]
    health = [ResourceHealth(memory_free_percent=7.0, swap_used_bytes=0, pageout_bytes=0)]
    signals: list[signal.Signals] = []
    scheduler = AdaptiveScheduler(
        tmp_path,
        4242,
        "auto",
        config(tmp_path),
        clock=lambda: now[0],
        load_probe=lambda _pgid: SystemLoad(0.0, 0.0),
        gpu_probe=lambda: 0.0,
        power_probe=lambda: True,
        health_probe=lambda: health[0],
        jank_probe=lambda: guardian(),
        controller_pid=999,
        controller_start_signature="controller-birth",
    )
    with patch(
        "h3_bridge.scheduler.signal_process_group",
        side_effect=lambda _pgid, selected: signals.append(selected) or True,
    ), patch("h3_bridge.scheduler.set_process_group_background", return_value=True):
        scheduler.start()
        assert scheduler.tick().reason == "memory-pressure"
        health[0] = ResourceHealth(
            memory_free_percent=25.0, swap_used_bytes=0, pageout_bytes=0
        )
        now[0] = 10.0
        assert scheduler.tick(force=True).adaptive_phase == "paused"
        now[0] = 25.0
        probe = scheduler.tick(force=True)

    assert probe.adaptive_phase == "probe"
    assert signals == [signal.SIGSTOP, signal.SIGCONT]


def test_thermal_pressure_uses_guardian_fast_pause_path(tmp_path: Path):
    health_probe = MagicMock()
    scheduler = AdaptiveScheduler(
        tmp_path,
        4242,
        "auto",
        config(tmp_path),
        load_probe=MagicMock(),
        gpu_probe=MagicMock(),
        power_probe=MagicMock(),
        health_probe=health_probe,
        jank_probe=lambda: guardian(thermal_state="critical"),
        controller_pid=999,
        controller_start_signature="controller-birth",
    )
    with patch("h3_bridge.scheduler.signal_process_group", return_value=True), patch(
        "h3_bridge.scheduler.set_process_group_background", return_value=True
    ):
        scheduler.start()

    assert scheduler.tick().paused
    assert scheduler.tick().reason == "thermal-pressure"
    health_probe.assert_not_called()


def test_swap_growth_rate_pauses_auto(tmp_path: Path):
    now = [0.0]
    health = [ResourceHealth(50.0, 0, 0)]
    scheduler = AdaptiveScheduler(
        tmp_path,
        4242,
        "auto",
        config(tmp_path),
        clock=lambda: now[0],
        load_probe=lambda _pgid: SystemLoad(),
        gpu_probe=lambda: 0.0,
        power_probe=lambda: True,
        health_probe=lambda: health[0],
        jank_probe=lambda: guardian(),
        controller_pid=999,
        controller_start_signature="controller-birth",
    )
    with patch("h3_bridge.scheduler.signal_process_group", return_value=True), patch(
        "h3_bridge.scheduler.set_process_group_background", return_value=True
    ):
        scheduler.start()
        health[0] = ResourceHealth(50.0, 512 * 1024 * 1024, 0)
        now[0] = 10.0
        decision = scheduler.tick(force=True)

    assert decision.paused
    assert decision.reason == "swap-thrashing"


def test_taskpolicy_failure_is_retried_without_claiming_success(tmp_path: Path):
    now = [0.0]
    scheduler = AdaptiveScheduler(
        tmp_path,
        4242,
        "low",
        config(tmp_path),
        clock=lambda: now[0],
        controller_pid=999,
        controller_start_signature="controller-birth",
    )
    with patch("h3_bridge.scheduler.signal_process_group", return_value=True), patch(
        "h3_bridge.scheduler.set_process_group_background",
        side_effect=[False, True],
    ) as apply_policy:
        scheduler.start()
        now[0] = 1.0
        scheduler.tick(force=True)
        now[0] = 5.0
        scheduler.tick(force=True)

    assert apply_policy.call_count == 2
    status = json.loads((tmp_path / "process.json").read_text(encoding="utf-8"))
    assert status["background_policy_applied"] is True


def test_health_and_status_sampling_use_slow_independent_cadences(tmp_path: Path):
    now = [0.0]
    health_probe = MagicMock(return_value=ResourceHealth(50.0, 0, 0))
    scheduler = AdaptiveScheduler(
        tmp_path,
        4242,
        "auto",
        config(tmp_path),
        clock=lambda: now[0],
        load_probe=lambda _pgid: SystemLoad(),
        gpu_probe=lambda: 0.0,
        power_probe=lambda: True,
        health_probe=health_probe,
        jank_probe=lambda: guardian(),
        controller_pid=999,
        controller_start_signature="controller-birth",
    )
    with patch("h3_bridge.scheduler.signal_process_group", return_value=True), patch(
        "h3_bridge.scheduler.set_process_group_background", return_value=True
    ):
        scheduler.start()
        scheduler._write_status = status_writer = MagicMock()
        now[0] = 2.0
        scheduler.tick()
        now[0] = 10.0
        scheduler.tick()
        now[0] = 15.0
        scheduler.tick()

    assert health_probe.call_count == 2
    status_writer.assert_called_once()


def test_native_guardian_is_not_run_for_low_or_max_policy(tmp_path: Path):
    with patch.object(NativeGuardian, "poll") as poll, patch.object(
        NativeGuardian, "close"
    ) as close, patch("h3_bridge.scheduler.signal_process_group"), patch(
        "h3_bridge.scheduler.set_process_group_background", return_value=True
    ):
        low = AdaptiveScheduler(
            tmp_path,
            4242,
            "low",
            config(tmp_path),
            controller_pid=999,
            controller_start_signature="controller-birth",
        )
        low.start()

    poll.assert_not_called()
    close.assert_called()


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
        config(tmp_path, "pause"),
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

            idle[0] = 360.0
            scheduler.tick(force=True)
            resumed = json.loads((tmp_path / "process.json").read_text(encoding="utf-8"))
            assert resumed["state"] == "running"
            assert child.poll() is None
    finally:
        os.killpg(child.pid, signal.SIGCONT)
        os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=5)
