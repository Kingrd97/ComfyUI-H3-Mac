from __future__ import annotations

import json
import os
import platform
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import BridgeConfig
from .models import ResourceProfile


_IDLE_RE = re.compile(r'"HIDIdleTime"\s*=\s*(\d+)')
_VALID_POLICIES = {"low", "auto", "max"}


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


def external_cpu_percent(pgid: int) -> float:
    if platform.system() != "Darwin":
        return 0.0
    try:
        result = subprocess.run(
            ["/bin/ps", "-A", "-o", "pgid=,%cpu="],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0.0
    total = 0.0
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        try:
            candidate_pgid = int(fields[0])
            usage = float(fields[1].replace(",", "."))
        except ValueError:
            continue
        if candidate_pgid != pgid:
            total += max(0.0, usage)
    return total


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
    ac_power: bool


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
        cpu_probe: Callable[[int], float] = external_cpu_percent,
        power_probe: Callable[[], bool] = on_ac_power,
    ):
        self.job_dir = job_dir
        self.pgid = pgid
        self.engine_profile = engine_profile
        self.config = config
        self.clock = clock
        self.idle_probe = idle_probe
        self.cpu_probe = cpu_probe
        self.power_probe = power_probe
        self.control_path = job_dir / "control.json"
        self.status_path = job_dir / "process.json"
        self._last_check = float("-inf")
        self._stopped = False
        self._background: bool | None = None
        self._last_decision: SchedulerDecision | None = None

    def start(self) -> None:
        _atomic_json(
            self.control_path,
            {"paused": False, "policy": self.engine_profile, "updated_at": time.time()},
        )
        self.tick(force=True)

    def _control(self) -> tuple[bool, ResourceProfile]:
        value = read_json(self.control_path)
        paused = bool(value.get("paused", False))
        raw_policy = str(value.get("policy", self.engine_profile))
        policy: ResourceProfile = (
            raw_policy if raw_policy in _VALID_POLICIES else self.engine_profile
        )  # type: ignore[assignment]
        return paused, policy

    def _decide(self, manual_pause: bool, policy: ResourceProfile) -> SchedulerDecision:
        idle = self.idle_probe() if policy == "auto" else 0.0
        ac_power = self.power_probe() if policy == "auto" else True
        cpu = 0.0
        if policy == "auto" and idle >= self.config.auto_idle_seconds:
            cpu = self.cpu_probe(self.pgid)

        if manual_pause:
            return SchedulerDecision(True, True, "manual", policy, idle, cpu, ac_power)
        if policy == "low":
            return SchedulerDecision(False, True, "low", policy, idle, cpu, ac_power)
        if policy == "max":
            return SchedulerDecision(False, False, "max", policy, idle, cpu, ac_power)

        active_reason = ""
        if idle < self.config.auto_idle_seconds:
            active_reason = "user-active"
        elif self.config.auto_require_ac_power and not ac_power:
            active_reason = "battery"
        elif cpu >= self.config.auto_max_external_cpu_percent:
            active_reason = "foreground-cpu"

        if active_reason and self.config.auto_active_behavior == "pause":
            return SchedulerDecision(True, True, active_reason, policy, idle, cpu, ac_power)
        if active_reason:
            return SchedulerDecision(False, True, active_reason, policy, idle, cpu, ac_power)
        return SchedulerDecision(False, False, "idle-boost", policy, idle, cpu, ac_power)

    def _write_status(self, state: str, decision: SchedulerDecision, error: str = "") -> None:
        _atomic_json(
            self.status_path,
            {
                "pid": self.pgid,
                "pgid": self.pgid,
                "state": state,
                "engine_profile": self.engine_profile,
                "scheduler_policy": decision.policy,
                "paused": decision.paused,
                "background": decision.background,
                "reason": decision.reason,
                "idle_seconds": round(decision.idle_seconds, 1),
                "external_cpu_percent": round(decision.external_cpu_percent, 1),
                "ac_power": decision.ac_power,
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
        manual_pause, policy = self._control()
        decision = self._decide(manual_pause, policy)

        if decision.paused and not self._stopped:
            self._stopped = signal_process_group(self.pgid, signal.SIGSTOP)
        elif not decision.paused and self._stopped:
            signal_process_group(self.pgid, signal.SIGCONT)
            self._stopped = False

        if not decision.paused and self._background != decision.background:
            set_process_group_background(self.pgid, decision.background)
            self._background = decision.background

        self._last_decision = decision
        state = "paused" if decision.paused else "running"
        self._write_status(state, decision)
        return decision

    def prepare_termination(self) -> None:
        if self._stopped:
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
            True,
        )
        self._write_status(state, decision, error)
