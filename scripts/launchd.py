#!/usr/bin/env python3
"""Install and control the per-user ComfyUI/vpipe launchd services."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
UID = os.getuid()
DOMAIN = f"gui/{UID}"
AGENTS = Path.home() / "Library" / "LaunchAgents"
COMFY_LABEL = "com.kingrd97.comfyui-h3-mac"
WORKER_LABEL = "com.kingrd97.comfyui-h3-mac.vpipe-worker"
REQUIRED_COMFY_NODES = ("H3GenerateVideo", "H3GenerateVideoVPipe")


def _service_specs(project_root: Path) -> dict[str, dict[str, object]]:
    runtime = project_root / "runtime"
    python = runtime / ".venv" / "bin" / "python"
    if not python.is_file():
        raise RuntimeError("尚未安装 Python 环境，请先运行 Install.command。")
    environment = {
        "HOME": str(Path.home()),
        "PATH": (
            f"{runtime / 'bin'}:{Path.home() / '.local' / 'bin'}:"
            "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        ),
        "PYTHONUNBUFFERED": "1",
    }
    common: dict[str, object] = {
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 5,
        "ProcessType": "Background",
        "AbandonProcessGroup": True,
        "EnvironmentVariables": environment,
    }
    return {
        COMFY_LABEL: {
            **common,
            "Label": COMFY_LABEL,
            "ProgramArguments": [
                "/usr/bin/env",
                "H3_NO_OPEN=1",
                str(project_root / "scripts" / "start.sh"),
            ],
            "WorkingDirectory": str(project_root),
            "StandardOutPath": str(runtime / "comfyui-server.log"),
            "StandardErrorPath": str(runtime / "comfyui-server.log"),
        },
        WORKER_LABEL: {
            **common,
            "Label": WORKER_LABEL,
            "ProgramArguments": [
                str(python),
                "-m",
                "h3_bridge.vpipe_worker",
                "--project-root",
                str(project_root),
            ],
            "WorkingDirectory": str(project_root),
            "StandardOutPath": str(runtime / "vpipe-worker.log"),
            "StandardErrorPath": str(runtime / "vpipe-worker.log"),
        },
    }


def _path(label: str) -> Path:
    return AGENTS / f"{label}.plist"


def _loaded(label: str) -> bool:
    result = subprocess.run(
        ["launchctl", "print", f"{DOMAIN}/{label}"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def _comfy_ready() -> tuple[bool, str]:
    for node_id in REQUIRED_COMFY_NODES:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:8188/object_info/{node_id}", timeout=1.5
            ) as response:
                if response.status != 200:
                    return False, f"{node_id}: HTTP {response.status}"
                payload = json.load(response)
                if not isinstance(payload, dict) or node_id not in payload:
                    return False, f"ComfyUI is reachable but the {node_id} node is missing"
        except (OSError, ValueError, TypeError, urllib.error.URLError) as exc:
            return False, f"{node_id}: {exc}"
    return True, "H3 BF16 and vpipe nodes ready"


def _worker_ready(project_root: Path = PROJECT_ROOT) -> tuple[bool, str]:
    heartbeat_path = project_root / "runtime/vpipe-worker/heartbeat.json"
    try:
        heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
        age = max(0.0, time.time() - float(heartbeat.get("updated_at", 0)))
        state = str(heartbeat.get("state", "unknown"))
        pid = int(heartbeat.get("pid", 0))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return False, f"heartbeat unavailable: {exc}"
    if age > 20.0:
        return False, f"heartbeat stale ({age:.1f}s)"
    if state == "starting":
        return False, f"worker still starting (pid {pid})"
    if pid <= 1:
        return False, "heartbeat has no valid worker pid"
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False, f"heartbeat worker pid {pid} is not alive"
    return True, f"heartbeat {state}, pid {pid} ({age:.1f}s)"


def _control_ready(label: str) -> tuple[bool, str]:
    if not _loaded(label):
        return False, "launchd label not loaded"
    return _comfy_ready() if label == COMFY_LABEL else _worker_ready(PROJECT_ROOT)


def _wait_ready(labels: list[str], timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    details: dict[str, str] = {}
    while time.monotonic() < deadline:
        pending = []
        for label in labels:
            ready, detail = _control_ready(label)
            details[label] = detail
            if not ready:
                pending.append(label)
        if not pending:
            for label in labels:
                print(f"[ok] {label}: {details[label]}")
            return
        time.sleep(0.5)
    summary = "; ".join(f"{label}: {details.get(label, 'unknown')}" for label in labels)
    raise RuntimeError(f"launchd control plane did not become ready: {summary}")


def _write_plist(path: Path, value: dict[str, object]) -> bool:
    encoded = plistlib.dumps(value, fmt=plistlib.FMT_XML, sort_keys=True)
    old = path.read_bytes() if path.exists() else b""
    if old == encoded:
        return False
    temporary = path.with_suffix(".plist.tmp")
    temporary.write_bytes(encoded)
    temporary.replace(path)
    return True


def _bootout(label: str) -> None:
    if _loaded(label):
        subprocess.run(
            ["launchctl", "bootout", f"{DOMAIN}/{label}"],
            check=True,
        )


def _bootstrap(label: str) -> None:
    command = ["launchctl", "bootstrap", DOMAIN, str(_path(label))]
    # A legacy `launchctl submit` job can remain in launchd's bookkeeping for
    # a short time after bootout.  During that window bootstrap returns EIO
    # (exit 5), even though the old label disappears immediately afterwards.
    # Retry only that transient result; preserve every other launchctl error.
    for attempt in range(3):
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode == 0:
            return
        if result.returncode == 5 and attempt < 2:
            time.sleep(attempt + 1)
            continue
        detail = (result.stderr or result.stdout).strip() or "no launchctl details"
        raise RuntimeError(
            f"launchctl bootstrap failed ({result.returncode}): {detail}"
        )


def install(*, worker_only: bool = False, restart: bool = False) -> int:
    AGENTS.mkdir(parents=True, exist_ok=True)
    specs = _service_specs(PROJECT_ROOT)
    labels = [WORKER_LABEL] if worker_only else [WORKER_LABEL, COMFY_LABEL]
    for label in labels:
        changed = _write_plist(_path(label), specs[label])
        loaded = _loaded(label)
        if loaded and (restart or changed):
            _bootout(label)
            loaded = False
        if not loaded:
            if label == WORKER_LABEL:
                (PROJECT_ROOT / "runtime/vpipe-worker/heartbeat.json").unlink(
                    missing_ok=True
                )
            _bootstrap(label)
        detail = "restarted" if restart or changed else "ready"
        print(f"[ok] {label}: {detail}")
    _wait_ready(labels)
    return 0


def status(*, worker_only: bool = False) -> int:
    labels = [WORKER_LABEL] if worker_only else [COMFY_LABEL, WORKER_LABEL]
    failed = False
    for label in labels:
        ready, detail = _control_ready(label)
        print(f"{'[ok]' if ready else '[--]'} {label}: {detail}")
        failed = failed or not ready
    return 1 if failed else 0


def stop(*, worker_only: bool = False) -> int:
    labels = [WORKER_LABEL] if worker_only else [COMFY_LABEL, WORKER_LABEL]
    for label in labels:
        _bootout(label)
        print(f"[ok] {label}: stopped")
    return 0


def uninstall() -> int:
    for label in (COMFY_LABEL, WORKER_LABEL):
        _bootout(label)
        _path(label).unlink(missing_ok=True)
        print(f"[ok] {label}: uninstalled")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ComfyUI-H3-Mac launchd services")
    parser.add_argument("action", choices=["install", "restart", "status", "stop", "uninstall"])
    parser.add_argument("--worker-only", action="store_true")
    args = parser.parse_args(argv)
    if args.action == "install":
        return install(worker_only=args.worker_only)
    if args.action == "restart":
        return install(worker_only=args.worker_only, restart=True)
    if args.action == "status":
        return status(worker_only=args.worker_only)
    if args.action == "stop":
        return stop(worker_only=args.worker_only)
    return uninstall()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"service error: {exc}", file=sys.stderr)
        raise SystemExit(1)
