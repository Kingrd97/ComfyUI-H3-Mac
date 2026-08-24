#!/usr/bin/env python3
"""Install and control the per-user ComfyUI/vpipe launchd services."""

from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
UID = os.getuid()
DOMAIN = f"gui/{UID}"
AGENTS = Path.home() / "Library" / "LaunchAgents"
COMFY_LABEL = "com.kingrd97.comfyui-h3-mac"
WORKER_LABEL = "com.kingrd97.comfyui-h3-mac.vpipe-worker"


def _service_specs(project_root: Path) -> dict[str, dict[str, object]]:
    runtime = project_root / "runtime"
    python = runtime / ".venv" / "bin" / "python"
    if not python.is_file():
        raise RuntimeError("尚未安装 Python 环境，请先运行 Install.command。")
    environment = {
        "HOME": str(Path.home()),
        "PATH": (
            f"{Path.home() / '.local' / 'bin'}:"
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
        try:
            subprocess.run(command, check=True)
            return
        except subprocess.CalledProcessError as exc:
            if exc.returncode != 5 or attempt == 2:
                raise
            time.sleep(attempt + 1)


def install(*, worker_only: bool = False, restart: bool = False) -> int:
    AGENTS.mkdir(parents=True, exist_ok=True)
    specs = _service_specs(PROJECT_ROOT)
    labels = [WORKER_LABEL] if worker_only else [WORKER_LABEL, COMFY_LABEL]
    for label in labels:
        changed = _write_plist(_path(label), specs[label])
        loaded = _loaded(label)
        if loaded and restart:
            _bootout(label)
            loaded = False
        if not loaded:
            _bootstrap(label)
        detail = "restarted" if restart else "installed" if changed else "ready"
        print(f"[ok] {label}: {detail}")
    return 0


def status(*, worker_only: bool = False) -> int:
    labels = [WORKER_LABEL] if worker_only else [COMFY_LABEL, WORKER_LABEL]
    failed = False
    for label in labels:
        loaded = _loaded(label)
        print(f"{'[ok]' if loaded else '[--]'} {label}: {'running' if loaded else 'not loaded'}")
        failed = failed or not loaded
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
