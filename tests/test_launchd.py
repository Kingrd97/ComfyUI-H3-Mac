from __future__ import annotations

import os
import io
import json
import subprocess
import time
from pathlib import Path

from scripts import launchd
from scripts.launchd import COMFY_LABEL, WORKER_LABEL, _bootstrap, _service_specs


class FakeHTTPResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def test_comfy_readiness_requires_our_bf16_node(monkeypatch):
    monkeypatch.setattr(
        launchd.urllib.request,
        "urlopen",
        lambda *_a, **_k: FakeHTTPResponse(
            json.dumps({"SomeOtherNode": {}}).encode()
        ),
    )

    ready, detail = launchd._comfy_ready()

    assert ready is False
    assert "H3GenerateVideo node is missing" in detail


def test_comfy_readiness_requires_our_vpipe_node(monkeypatch):
    def response(url, **_kwargs):
        node_id = str(url).rsplit("/", 1)[-1]
        payload = (
            {node_id: {"display_name": "H3"}}
            if node_id == "H3GenerateVideo"
            else {"SomeOtherNode": {}}
        )
        return FakeHTTPResponse(json.dumps(payload).encode())

    monkeypatch.setattr(launchd.urllib.request, "urlopen", response)

    ready, detail = launchd._comfy_ready()

    assert ready is False
    assert "H3GenerateVideoVPipe node is missing" in detail


def test_comfy_readiness_accepts_our_vpipe_node(monkeypatch):
    def response(url, **_kwargs):
        node_id = str(url).rsplit("/", 1)[-1]
        return FakeHTTPResponse(
            json.dumps({node_id: {"display_name": "H3"}}).encode()
        )

    monkeypatch.setattr(launchd.urllib.request, "urlopen", response)

    ready, detail = launchd._comfy_ready()

    assert ready is True
    assert detail == "H3 BF16 and vpipe nodes ready"


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
    assert specs[WORKER_LABEL]["EnvironmentVariables"]["PATH"].startswith(
        str(tmp_path / "runtime" / "bin") + ":"
    )
    assert specs[COMFY_LABEL]["ProgramArguments"][-1] == str(
        tmp_path / "scripts" / "start.sh"
    )


def test_bootstrap_retries_transient_launchctl_eio(monkeypatch):
    calls = 0
    sleeps: list[int] = []

    def fake_run(command, **_kwargs):
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(
            command,
            5 if calls == 1 else 0,
            stdout="",
            stderr="transient" if calls == 1 else "",
        )

    monkeypatch.setattr(launchd.subprocess, "run", fake_run)
    monkeypatch.setattr(launchd.time, "sleep", sleeps.append)

    _bootstrap(COMFY_LABEL)

    assert calls == 2
    assert sleeps == [1]


def test_bootstrap_does_not_retry_permanent_error(monkeypatch):
    def fake_run(command, **_kwargs):
        return subprocess.CompletedProcess(
            command, 78, stdout="", stderr="invalid launch agent"
        )

    monkeypatch.setattr(launchd.subprocess, "run", fake_run)
    monkeypatch.setattr(
        launchd.time,
        "sleep",
        lambda _: (_ for _ in ()).throw(AssertionError("unexpected retry")),
    )

    try:
        _bootstrap(COMFY_LABEL)
    except RuntimeError as exc:
        assert "(78)" in str(exc)
        assert "invalid launch agent" in str(exc)
    else:
        raise AssertionError("expected launchctl failure")


def test_changed_launchd_plist_reloads_loaded_service(tmp_path: Path, monkeypatch):
    python = tmp_path / "runtime/.venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    monkeypatch.setattr(launchd, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(launchd, "AGENTS", tmp_path / "agents")
    monkeypatch.setattr(launchd, "_loaded", lambda _label: True)
    monkeypatch.setattr(launchd, "_write_plist", lambda _path, _value: True)
    stopped: list[str] = []
    started: list[str] = []
    monkeypatch.setattr(launchd, "_bootout", stopped.append)
    monkeypatch.setattr(launchd, "_bootstrap", started.append)
    waited: list[list[str]] = []
    monkeypatch.setattr(launchd, "_wait_ready", lambda labels: waited.append(labels))

    assert launchd.install(worker_only=True) == 0

    assert stopped == [WORKER_LABEL]
    assert started == [WORKER_LABEL]
    assert waited == [[WORKER_LABEL]]


def test_worker_readiness_rejects_stale_heartbeat(tmp_path: Path):
    heartbeat = tmp_path / "runtime/vpipe-worker/heartbeat.json"
    heartbeat.parent.mkdir(parents=True)
    heartbeat.write_text(
        f'{{"pid":{os.getpid()},"state":"idle","updated_at":1}}', encoding="utf-8"
    )

    ready, detail = launchd._worker_ready(tmp_path)

    assert ready is False
    assert "stale" in detail


def test_worker_readiness_rejects_starting_crash_loop(tmp_path: Path):
    heartbeat = tmp_path / "runtime/vpipe-worker/heartbeat.json"
    heartbeat.parent.mkdir(parents=True)
    heartbeat.write_text(
        f'{{"pid":{os.getpid()},"state":"starting","updated_at":{time.time()}}}',
        encoding="utf-8",
    )

    ready, detail = launchd._worker_ready(tmp_path)

    assert ready is False
    assert "starting" in detail
