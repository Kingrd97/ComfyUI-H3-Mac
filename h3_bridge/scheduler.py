from __future__ import annotations

import fcntl
import json
import math
import os
import platform
import re
import signal
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import BridgeConfig
from .models import ResourceProfile


_IDLE_RE = re.compile(r'"HIDIdleTime"\s*=\s*(\d+)')
_GPU_RE = re.compile(r'"Device Utilization %"\s*=\s*(\d+(?:\.\d+)?)')
_VALID_POLICIES = {"low", "auto", "max"}


@dataclass(frozen=True)
class SystemLoad:
    external_cpu_percent: float = 0.0
    window_server_cpu_percent: float = 0.0


@dataclass(frozen=True)
class GuardianSnapshot:
    input_idle_seconds: float
    frame_age_ms: float | None
    maximum_refresh_interval_ms: float | None
    display_link_p95_ms: float | None
    frontmost_bundle_id: str | None
    display_link_max_gap_ms: float | None = None
    display_link_callback_age_ms: float | None = None
    frame_stalled: bool = False


def _optional_float(value: object) -> float | None:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


class NativeGuardian:
    """Read responsive-display samples from the optional native helper."""

    def __init__(
        self,
        binary: Path,
        *,
        interaction_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.binary = binary
        self.interaction_seconds = interaction_seconds
        self.clock = clock
        self.process: subprocess.Popen[bytes] | None = None
        self._buffer = b""
        self._frame_stall_samples = 0

    def _snapshot_from_payload(
        self, value: object
    ) -> GuardianSnapshot | None:
        """Parse one helper sample and debounce display-cadence stalls.

        ``frame_age_ms`` is intentionally diagnostic-only: an unchanged
        framebuffer is normal for a static window or hardware cursor.  The
        strong signal is a delayed display-link callback while the user is
        actively providing input.
        """

        if not isinstance(value, dict):
            return None
        sample_uptime = _optional_float(value.get("sample_uptime"))
        if sample_uptime is not None:
            sample_age = self.clock() - sample_uptime
            if sample_age > 2.0 or sample_age < -1.0:
                self._frame_stall_samples = 0
                return None
        input_idle = _optional_float(value.get("input_idle_seconds"))
        if input_idle is None:
            return None
        refresh = _optional_float(value.get("maximum_refresh_interval_ms"))
        maximum_gap = _optional_float(value.get("display_link_max_gap_ms"))
        callback_age = _optional_float(value.get("display_link_callback_age_ms"))
        display_delay = max(
            (delay for delay in (maximum_gap, callback_age) if delay is not None),
            default=0.0,
        )
        threshold = max(100.0, 2.5 * refresh) if refresh is not None else 100.0
        raw_stall = (
            input_idle <= self.interaction_seconds
            and display_delay > threshold
        )
        self._frame_stall_samples = self._frame_stall_samples + 1 if raw_stall else 0
        bundle = value.get("frontmost_bundle_id")
        return GuardianSnapshot(
            input_idle_seconds=max(0.0, input_idle),
            frame_age_ms=_optional_float(value.get("frame_age_ms")),
            maximum_refresh_interval_ms=refresh,
            display_link_p95_ms=_optional_float(value.get("display_link_p95_ms")),
            frontmost_bundle_id=bundle if isinstance(bundle, str) else None,
            display_link_max_gap_ms=maximum_gap,
            display_link_callback_age_ms=callback_age,
            frame_stalled=self._frame_stall_samples >= 2,
        )

    def start(self) -> None:
        if (
            platform.system() != "Darwin"
            or not self.binary.is_file()
            or not os.access(self.binary, os.X_OK)
        ):
            return
        try:
            self.process = subprocess.Popen(
                [str(self.binary)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError:
            self.process = None
            return
        assert self.process.stdout is not None
        os.set_blocking(self.process.stdout.fileno(), False)

    def poll(self) -> GuardianSnapshot | None:
        if self.process is None or self.process.stdout is None:
            self._frame_stall_samples = 0
            return None
        if self.process.poll() is not None:
            self._frame_stall_samples = 0
            return None

        saw_stall = False
        newest: GuardianSnapshot | None = None
        while True:
            try:
                chunk = os.read(self.process.stdout.fileno(), 65_536)
            except BlockingIOError:
                break
            except OSError:
                return None
            if not chunk:
                break
            self._buffer += chunk

        lines = self._buffer.split(b"\n")
        self._buffer = lines.pop()
        for raw_line in lines:
            try:
                value = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            sample = self._snapshot_from_payload(value)
            if sample is None:
                continue
            newest = sample
            saw_stall = saw_stall or sample.frame_stalled

        if newest is not None:
            if saw_stall and not newest.frame_stalled:
                newest = GuardianSnapshot(
                    input_idle_seconds=newest.input_idle_seconds,
                    frame_age_ms=newest.frame_age_ms,
                    maximum_refresh_interval_ms=newest.maximum_refresh_interval_ms,
                    display_link_p95_ms=newest.display_link_p95_ms,
                    frontmost_bundle_id=newest.frontmost_bundle_id,
                    display_link_max_gap_ms=newest.display_link_max_gap_ms,
                    display_link_callback_age_ms=(
                        newest.display_link_callback_age_ms
                    ),
                    frame_stalled=True,
                )
            return newest
        # Never reuse input/display telemetry after a silent helper poll. Doing
        # so could let an old 2-second sample satisfy a 2-second pause debounce.
        self._frame_stall_samples = 0
        return None

    def close(self) -> None:
        process = self.process
        self.process = None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        if process.stdout is not None:
            process.stdout.close()


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def process_group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def process_start_signature(pid: int) -> str:
    """Return a stable birth-time fingerprint for PID-reuse protection."""

    if platform.system() != "Darwin":
        return ""
    try:
        result = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "lstart="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def process_group_stopped(pgid: int) -> bool | None:
    """Inspect the real process-group state after an external control signal."""

    if platform.system() != "Darwin":
        return None
    try:
        result = subprocess.run(
            ["/bin/ps", "-g", str(pgid), "-o", "state="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    states = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if result.returncode != 0 or not states:
        return None
    return all(state.startswith("T") for state in states)


def signal_process_group(pgid: int, selected: signal.Signals) -> bool:
    try:
        os.killpg(pgid, selected)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def process_group_pids(pgid: int) -> list[int]:
    if platform.system() != "Darwin":
        return [pgid]
    try:
        result = subprocess.run(
            ["/bin/ps", "-A", "-o", "pid=,pgid="],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return [pgid]
    selected: list[int] = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        try:
            pid, candidate_pgid = (int(item) for item in fields)
        except ValueError:
            continue
        if candidate_pgid == pgid:
            selected.append(pid)
    return selected or [pgid]


def set_process_group_background(pgid: int, background: bool) -> None:
    if platform.system() != "Darwin":
        return
    option = "-b" if background else "-B"
    for pid in process_group_pids(pgid):
        try:
            subprocess.run(
                ["/usr/sbin/taskpolicy", option, "-p", str(pid)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue


def hid_idle_seconds() -> float:
    if platform.system() != "Darwin":
        return float("inf")
    try:
        result = subprocess.run(
            [
                "/usr/sbin/ioreg",
                "-r",
                "-c",
                "IOHIDSystem",
                "-d",
                "1",
                "-k",
                "HIDIdleTime",
                "-w",
                "0",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0.0
    match = _IDLE_RE.search(result.stdout)
    return int(match.group(1)) / 1_000_000_000 if match else 0.0


def system_load(pgid: int) -> SystemLoad:
    if platform.system() != "Darwin":
        return SystemLoad()
    try:
        result = subprocess.run(
            ["/bin/ps", "-A", "-o", "pgid=,%cpu=,comm="],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return SystemLoad()
    total = 0.0
    window_server = 0.0
    for line in result.stdout.splitlines():
        fields = line.split(maxsplit=2)
        if len(fields) != 3:
            continue
        try:
            candidate_pgid = int(fields[0])
            usage = float(fields[1].replace(",", "."))
        except ValueError:
            continue
        if candidate_pgid != pgid:
            total += max(0.0, usage)
            if Path(fields[2]).name == "WindowServer":
                window_server += max(0.0, usage)
    return SystemLoad(total, window_server)


def external_cpu_percent(pgid: int) -> float:
    """Backward-compatible scalar probe used by older integrations."""

    return system_load(pgid).external_cpu_percent


def gpu_utilization_percent() -> float | None:
    """Return the best-effort Apple GPU utilization reported by IOKit.

    This AGX driver field is intentionally treated as optional because it is
    not a stable public API and may be absent on a future macOS release.
    """

    if platform.system() != "Darwin":
        return None
    try:
        result = subprocess.run(
            [
                "/usr/sbin/ioreg",
                "-r",
                "-c",
                "AGXAccelerator",
                "-d",
                "1",
                "-w",
                "0",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    values = [float(value) for value in _GPU_RE.findall(result.stdout)]
    return max(values) if values else None


def on_ac_power() -> bool:
    if platform.system() != "Darwin":
        return True
    try:
        result = subprocess.run(
            ["/usr/bin/pmset", "-g", "batt"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "AC Power" in result.stdout


@dataclass(frozen=True)
class SchedulerDecision:
    paused: bool
    background: bool
    reason: str
    policy: ResourceProfile
    idle_seconds: float
    external_cpu_percent: float
    window_server_cpu_percent: float
    gpu_percent: float | None
    ac_power: bool
    adaptive_phase: str
    guardian: GuardianSnapshot | None


class AdaptiveScheduler:
    """Control one H3 process group without changing inference parameters.

    SIGSTOP/SIGCONT preserves the exact in-memory computation. It is not a
    serialized checkpoint and therefore cannot survive process exit or reboot.
    """

    def __init__(
        self,
        job_dir: Path,
        pgid: int,
        engine_profile: ResourceProfile,
        config: BridgeConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
        idle_probe: Callable[[], float] = hid_idle_seconds,
        cpu_probe: Callable[[int], float] | None = None,
        load_probe: Callable[[int], SystemLoad] = system_load,
        gpu_probe: Callable[[], float | None] = gpu_utilization_percent,
        power_probe: Callable[[], bool] = on_ac_power,
        jank_probe: Callable[[], GuardianSnapshot | None] | None = None,
    ):
        self.job_dir = job_dir
        self.pgid = pgid
        self.engine_profile = engine_profile
        self.config = config
        self.clock = clock
        self.idle_probe = idle_probe
        if cpu_probe is None:
            self.load_probe = load_probe
        else:
            self.load_probe = lambda selected_pgid: SystemLoad(
                max(0.0, cpu_probe(selected_pgid)), 0.0
            )
        self.gpu_probe = gpu_probe
        self.power_probe = power_probe
        self._guardian: NativeGuardian | None = None
        if jank_probe is None:
            self._guardian = NativeGuardian(
                config.project_root / "runtime" / "bin" / "h3-guardian",
                interaction_seconds=config.auto_jank_interaction_seconds,
                clock=clock,
            )
            self.jank_probe = self._guardian.poll
        else:
            self.jank_probe = jank_probe
        self.control_path = job_dir / "control.json"
        self.control_lock_path = job_dir / "control.lock"
        self.status_path = job_dir / "process.json"
        self._last_check = float("-inf")
        self._stopped = False
        self._background: bool | None = None
        self._last_decision: SchedulerDecision | None = None
        self._last_status_write = float("-inf")
        self._last_status_signature: tuple[object, ...] | None = None
        self._last_metrics_check = float("-inf")
        self._last_idle_check = float("-inf")
        self._cached_idle = 0.0
        self._cached_load = SystemLoad()
        self._cached_gpu: float | None = None
        self._cached_ac_power = True
        self._auto_phase = "background"
        self._pressure_since: float | None = None
        self._healthy_since: float | None = None
        self._probe_started: float | None = None
        self._pause_reason = "contention-shield"
        self._last_policy: ResourceProfile | None = None
        self._last_manual_pause = False
        self._last_control_generation = 0
        self._control_reconcile = False
        self._process_start_signature = process_start_signature(pgid)
        self._process_signature_attempts = 1

    def start(self) -> None:
        if self._guardian is not None:
            self._guardian.start()
        _atomic_json(
            self.control_path,
            {
                "paused": False,
                "policy": self.engine_profile,
                "control_generation": 0,
                "updated_at": time.time(),
            },
        )
        self.tick(force=True)

    def _parse_control(
        self, value: dict[str, object]
    ) -> tuple[bool, ResourceProfile, int]:
        paused = bool(value.get("paused", False))
        raw_policy = str(value.get("policy", self.engine_profile))
        policy: ResourceProfile = (
            raw_policy if raw_policy in _VALID_POLICIES else self.engine_profile
        )  # type: ignore[assignment]
        try:
            generation = max(0, int(value.get("control_generation", 0)))
        except (TypeError, ValueError):
            generation = 0
        return paused, policy, generation

    def _control(self) -> tuple[bool, ResourceProfile, int]:
        with self.control_lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
            value = read_json(self.control_path)
            paused, policy, generation = self._parse_control(value)
            if generation != self._last_control_generation:
                observed = process_group_stopped(self.pgid)
                if observed is not None:
                    self._stopped = observed
                self._last_control_generation = generation
                self._control_reconcile = True
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return paused, policy, generation

    def _apply_signal_if_current(
        self, generation: int, decision: SchedulerDecision
    ) -> bool:
        """Serialize scheduler signals against CLI control transactions."""

        with self.control_lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            current = self._parse_control(read_json(self.control_path))
            if current[2] != generation:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                return False
            if decision.paused and (not self._stopped or self._control_reconcile):
                changed = signal_process_group(self.pgid, signal.SIGSTOP)
                self._stopped = changed or self._stopped
                self._control_reconcile = not changed
            elif not decision.paused and (self._stopped or self._control_reconcile):
                changed = signal_process_group(self.pgid, signal.SIGCONT)
                if changed:
                    self._stopped = False
                self._control_reconcile = not changed
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return True

    def _reset_adaptive(self, phase: str = "background") -> None:
        self._auto_phase = phase
        self._pressure_since = None
        self._healthy_since = None
        self._probe_started = None
        self._pause_reason = "contention-shield"

    def _sample_metrics(self, now: float, force: bool) -> tuple[SystemLoad, float | None, bool]:
        if force or now - self._last_metrics_check >= self.config.auto_metrics_poll_seconds:
            self._last_metrics_check = now
            # These probes are independent and each has its own timeout. Run
            # them together so a degraded system costs one timeout, not three.
            with ThreadPoolExecutor(max_workers=3, thread_name_prefix="h3-metrics") as pool:
                load_future = pool.submit(self.load_probe, self.pgid)
                gpu_future = pool.submit(self.gpu_probe)
                power_future = pool.submit(self.power_probe)
                self._cached_load = load_future.result()
                self._cached_gpu = gpu_future.result()
                self._cached_ac_power = power_future.result()
        return self._cached_load, self._cached_gpu, self._cached_ac_power

    def _sample_fallback_idle(self, now: float, force: bool) -> float:
        if force or now - self._last_idle_check >= self.config.auto_metrics_poll_seconds:
            self._last_idle_check = now
            self._cached_idle = self.idle_probe()
        return self._cached_idle

    def _decision(
        self,
        paused: bool,
        background: bool,
        reason: str,
        policy: ResourceProfile,
        idle: float,
        load: SystemLoad,
        gpu: float | None,
        ac_power: bool,
        phase: str,
        guardian: GuardianSnapshot | None = None,
    ) -> SchedulerDecision:
        return SchedulerDecision(
            paused,
            background,
            reason,
            policy,
            idle,
            load.external_cpu_percent,
            load.window_server_cpu_percent,
            gpu,
            ac_power,
            phase,
            guardian,
        )

    def _active_reason(self, idle: float, load: SystemLoad, ac_power: bool) -> str:
        if idle < self.config.auto_idle_seconds:
            return "user-active"
        if self.config.auto_require_ac_power and not ac_power:
            return "battery"
        if load.external_cpu_percent >= self.config.auto_max_external_cpu_percent:
            return "foreground-cpu"
        return "active-background"

    def _legacy_auto_decision(
        self,
        policy: ResourceProfile,
        idle: float,
        load: SystemLoad,
        gpu: float | None,
        ac_power: bool,
        guardian: GuardianSnapshot | None,
    ) -> SchedulerDecision:
        active_reason = ""
        if idle < self.config.auto_idle_seconds:
            active_reason = "user-active"
        elif self.config.auto_require_ac_power and not ac_power:
            active_reason = "battery"
        elif load.external_cpu_percent >= self.config.auto_max_external_cpu_percent:
            active_reason = "foreground-cpu"

        if active_reason and self.config.auto_active_behavior == "pause":
            return self._decision(
                True,
                True,
                active_reason,
                policy,
                idle,
                load,
                gpu,
                ac_power,
                "legacy-pause",
                guardian,
            )
        if active_reason:
            return self._decision(
                False,
                True,
                active_reason,
                policy,
                idle,
                load,
                gpu,
                ac_power,
                "background",
                guardian,
            )
        return self._decision(
            False,
            False,
            "idle-boost",
            policy,
            idle,
            load,
            gpu,
            ac_power,
            "idle-max",
            guardian,
        )

    def _adaptive_auto_decision(
        self,
        now: float,
        policy: ResourceProfile,
        idle: float,
        load: SystemLoad,
        gpu: float | None,
        ac_power: bool,
        guardian: GuardianSnapshot | None,
    ) -> SchedulerDecision:
        idle_boost = (
            idle >= self.config.auto_idle_seconds
            and (not self.config.auto_require_ac_power or ac_power)
            and load.external_cpu_percent < self.config.auto_max_external_cpu_percent
            and load.window_server_cpu_percent
            <= self.config.auto_jank_window_server_recover_percent
            and not bool(guardian and guardian.frame_stalled)
            and not bool(
                guardian
                and guardian.display_link_p95_ms is not None
                and guardian.display_link_p95_ms >= 100.0
            )
            and (
                self._auto_phase == "idle-max"
                or self._last_metrics_check == now
            )
        )
        if idle_boost:
            self._reset_adaptive("idle-max")
            return self._decision(
                False,
                False,
                "idle-boost",
                policy,
                idle,
                load,
                gpu,
                ac_power,
                self._auto_phase,
                guardian,
            )

        if self._auto_phase == "idle-max":
            self._reset_adaptive()

        interactive = idle <= self.config.auto_jank_interaction_seconds
        cpu_severe = load.external_cpu_percent >= self.config.auto_jank_cpu_percent
        window_server_high = (
            load.window_server_cpu_percent
            >= self.config.auto_jank_window_server_percent
        )
        gpu_high = gpu is not None and gpu >= self.config.auto_jank_gpu_percent
        display_link_stalled = (
            interactive
            and guardian is not None
            and guardian.display_link_p95_ms is not None
            and guardian.display_link_p95_ms >= 100.0
        )
        display_contention = interactive and (
            (window_server_high and gpu_high)
            or (display_link_stalled and (window_server_high or gpu_high))
        )
        frame_stalled = guardian is not None and guardian.frame_stalled
        severe = frame_stalled or cpu_severe or display_contention
        severe_reason = (
            "frame-stall"
            if frame_stalled
            else "external-cpu-jank"
            if cpu_severe
            else "display-contention"
        )
        healthy = (
            not frame_stalled
            and not display_link_stalled
            and load.external_cpu_percent
            <= self.config.auto_max_external_cpu_percent
            and (
                not interactive
                or load.window_server_cpu_percent
                <= self.config.auto_jank_window_server_recover_percent
            )
            and (
                not interactive
                or gpu is None
                or gpu <= self.config.auto_jank_gpu_recover_percent
            )
        )

        if self._auto_phase == "paused":
            if healthy:
                if self._healthy_since is None:
                    self._healthy_since = now
                if now - self._healthy_since >= self.config.auto_jank_recover_seconds:
                    self._auto_phase = "probe"
                    self._probe_started = now
                    self._healthy_since = None
                    return self._decision(
                        False,
                        True,
                        "probe-low",
                        policy,
                        idle,
                        load,
                        gpu,
                        ac_power,
                        self._auto_phase,
                        guardian,
                    )
            else:
                self._healthy_since = None
            return self._decision(
                True,
                True,
                self._pause_reason,
                policy,
                idle,
                load,
                gpu,
                ac_power,
                self._auto_phase,
                guardian,
            )

        if self._auto_phase == "probe":
            if severe:
                self._auto_phase = "paused"
                self._pause_reason = severe_reason
                self._healthy_since = None
                self._probe_started = None
                return self._decision(
                    True,
                    True,
                    severe_reason,
                    policy,
                    idle,
                    load,
                    gpu,
                    ac_power,
                    self._auto_phase,
                    guardian,
                )
            if self._probe_started is None:
                self._probe_started = now
            if now - self._probe_started >= self.config.auto_jank_probe_seconds:
                self._reset_adaptive()
                return self._decision(
                    False,
                    True,
                    self._active_reason(idle, load, ac_power),
                    policy,
                    idle,
                    load,
                    gpu,
                    ac_power,
                    self._auto_phase,
                    guardian,
                )
            return self._decision(
                False,
                True,
                "probe-low",
                policy,
                idle,
                load,
                gpu,
                ac_power,
                self._auto_phase,
                guardian,
            )

        if severe:
            if self._pressure_since is None:
                self._pressure_since = now
            if frame_stalled or (
                now - self._pressure_since
                >= max(
                    self.config.auto_jank_pause_seconds,
                    self.config.auto_metrics_poll_seconds,
                )
            ):
                self._auto_phase = "paused"
                self._pause_reason = severe_reason
                self._healthy_since = None
                self._pressure_since = None
                return self._decision(
                    True,
                    True,
                    severe_reason,
                    policy,
                    idle,
                    load,
                    gpu,
                    ac_power,
                    self._auto_phase,
                    guardian,
                )
        else:
            self._pressure_since = None
        return self._decision(
            False,
            True,
            self._active_reason(idle, load, ac_power),
            policy,
            idle,
            load,
            gpu,
            ac_power,
            self._auto_phase,
            guardian,
        )

    def _decide(
        self,
        now: float,
        manual_pause: bool,
        policy: ResourceProfile,
        force_metrics: bool,
    ) -> SchedulerDecision:
        if policy != self._last_policy or manual_pause != self._last_manual_pause:
            self._reset_adaptive()
        self._last_policy = policy
        self._last_manual_pause = manual_pause

        if manual_pause:
            previous_guardian = (
                self._last_decision.guardian if self._last_decision is not None else None
            )
            return self._decision(
                True,
                True,
                "manual",
                policy,
                previous_guardian.input_idle_seconds
                if previous_guardian is not None
                else 0.0,
                self._cached_load,
                self._cached_gpu,
                self._cached_ac_power,
                "manual",
                previous_guardian,
            )
        if policy == "low":
            return self._decision(
                False,
                True,
                "low",
                policy,
                0.0,
                SystemLoad(),
                None,
                True,
                "disabled",
            )
        if policy == "max":
            return self._decision(
                False,
                False,
                "max",
                policy,
                0.0,
                SystemLoad(),
                None,
                True,
                "disabled",
            )
        guardian = self.jank_probe()
        idle = (
            guardian.input_idle_seconds
            if guardian is not None
            else self._sample_fallback_idle(now, force_metrics)
        )
        if (
            self.config.auto_active_behavior == "adaptive"
            and guardian is not None
            and guardian.frame_stalled
        ):
            # The permission-free native cadence signal is deliberately the
            # fast path. Do not wait behind slower ps/ioreg/pmset fallbacks.
            return self._adaptive_auto_decision(
                now,
                policy,
                idle,
                self._cached_load,
                self._cached_gpu,
                self._cached_ac_power,
                guardian,
            )
        load, gpu, ac_power = self._sample_metrics(now, force_metrics)
        if self.config.auto_active_behavior != "adaptive":
            return self._legacy_auto_decision(
                policy, idle, load, gpu, ac_power, guardian
            )
        return self._adaptive_auto_decision(
            now, policy, idle, load, gpu, ac_power, guardian
        )

    def _write_status(self, state: str, decision: SchedulerDecision, error: str = "") -> None:
        if not self._process_start_signature and self._process_signature_attempts < 3:
            self._process_signature_attempts += 1
            self._process_start_signature = process_start_signature(self.pgid)
        _atomic_json(
            self.status_path,
            {
                "pid": self.pgid,
                "pgid": self.pgid,
                "process_start_signature": self._process_start_signature,
                "state": state,
                "engine_profile": self.engine_profile,
                "scheduler_policy": decision.policy,
                "paused": decision.paused,
                "background": decision.background,
                "reason": decision.reason,
                "adaptive_phase": decision.adaptive_phase,
                "idle_seconds": round(decision.idle_seconds, 1),
                "external_cpu_percent": round(decision.external_cpu_percent, 1),
                "window_server_cpu_percent": round(
                    decision.window_server_cpu_percent, 1
                ),
                "gpu_percent": (
                    round(decision.gpu_percent, 1)
                    if decision.gpu_percent is not None
                    else None
                ),
                "ac_power": decision.ac_power,
                "guardian_available": decision.guardian is not None,
                "frame_stalled": bool(
                    decision.guardian and decision.guardian.frame_stalled
                ),
                "frame_age_ms": (
                    round(decision.guardian.frame_age_ms, 1)
                    if decision.guardian is not None
                    and decision.guardian.frame_age_ms is not None
                    else None
                ),
                "display_link_p95_ms": (
                    round(decision.guardian.display_link_p95_ms, 1)
                    if decision.guardian is not None
                    and decision.guardian.display_link_p95_ms is not None
                    else None
                ),
                "display_link_max_gap_ms": (
                    round(decision.guardian.display_link_max_gap_ms, 1)
                    if decision.guardian is not None
                    and decision.guardian.display_link_max_gap_ms is not None
                    else None
                ),
                "display_link_callback_age_ms": (
                    round(decision.guardian.display_link_callback_age_ms, 1)
                    if decision.guardian is not None
                    and decision.guardian.display_link_callback_age_ms is not None
                    else None
                ),
                "frontmost_bundle_id": (
                    decision.guardian.frontmost_bundle_id
                    if decision.guardian is not None
                    else None
                ),
                "error": error,
                "updated_at": time.time(),
            },
        )

    def tick(self, force: bool = False) -> SchedulerDecision:
        now = self.clock()
        if not force and now - self._last_check < self.config.auto_poll_seconds:
            if self._last_decision is not None:
                return self._last_decision
        self._last_check = now
        manual_pause, policy, generation = self._control()
        decision = self._decide(now, manual_pause, policy, force)

        if not self._apply_signal_if_current(generation, decision):
            self._last_check = float("-inf")
            return self._last_decision or decision

        if not decision.paused and self._background != decision.background:
            set_process_group_background(self.pgid, decision.background)
            self._background = decision.background

        if manual_pause:
            # Keep the native helper pipe drained without running ps/ioreg/
            # pmset.  Manual pause itself was handled before every heavy probe.
            self.jank_probe()

        self._last_decision = decision
        state = "paused" if decision.paused else "running"
        status_signature = (
            state,
            decision.policy,
            decision.paused,
            decision.background,
            decision.reason,
            decision.adaptive_phase,
        )
        if (
            status_signature != self._last_status_signature
            or now - self._last_status_write >= self.config.auto_metrics_poll_seconds
        ):
            self._write_status(state, decision)
            self._last_status_signature = status_signature
            self._last_status_write = now
        return decision

    def prepare_termination(self) -> None:
        # A CLI may have stopped the group just before the scheduler observed
        # its control generation. CONT is harmless for a running group and
        # ensures the following TERM is not left pending on a stopped process.
        signal_process_group(self.pgid, signal.SIGCONT)
        self._stopped = False

    def finish(self, state: str, error: str = "") -> None:
        decision = self._last_decision or SchedulerDecision(
            False,
            self.engine_profile != "max",
            state,
            self.engine_profile,
            0.0,
            0.0,
            0.0,
            None,
            True,
            "finished",
            None,
        )
        self._write_status(state, decision, error)
        self._last_status_signature = None
        if self._guardian is not None:
            self._guardian.close()
