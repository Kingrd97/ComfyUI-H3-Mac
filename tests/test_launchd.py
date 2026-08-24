from __future__ import annotations

import subprocess
from pathlib import Path

from scripts import launchd
from scripts.launchd import COMFY_LABEL, WORKER_LABEL, _bootstrap, _service_specs


def test_launchd_keeps_control_plane_and_worker_alive(tmp_path: Path):
    python = tmp_path / "runtime" / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")

    specs = _service_specs(tmp_path)
    assert set(specs) == {COMFY_LABEL, WORKER_LABEL}
    for value in specs.values():
        assert value["KeepAlive"] is True
        assert value["RunAtLoad"] is True
        assert value["AbandonProcessGroup"] is True
        assert value["ProcessType"] == "Background"

    worker_args = specs[WORKER_LABEL]["ProgramArguments"]
    assert worker_args[:3] == [str(python), "-m", "h3_bridge.vpipe_worker"]
    assert specs[COMFY_LABEL]["ProgramArguments"][-1] == str(
        tmp_path / "scripts" / "start.sh"
    )


def test_bootstrap_retries_transient_launchctl_eio(monkeypatch):
    calls = 0
    sleeps: list[int] = []

    def fake_run(command, *, check):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise subprocess.CalledProcessError(5, command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(launchd.subprocess, "run", fake_run)
    monkeypatch.setattr(launchd.time, "sleep", sleeps.append)

    _bootstrap(COMFY_LABEL)

    assert calls == 2
    assert sleeps == [1]


def test_bootstrap_does_not_retry_permanent_error(monkeypatch):
    def fake_run(command, *, check):
        raise subprocess.CalledProcessError(78, command)

    monkeypatch.setattr(launchd.subprocess, "run", fake_run)
    monkeypatch.setattr(
        launchd.time,
        "sleep",
        lambda _: (_ for _ in ()).throw(AssertionError("unexpected retry")),
    )

    try:
        _bootstrap(COMFY_LABEL)
    except subprocess.CalledProcessError as exc:
        assert exc.returncode == 78
    else:
        raise AssertionError("expected launchctl failure")
