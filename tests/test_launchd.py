from __future__ import annotations

from pathlib import Path

from scripts.launchd import COMFY_LABEL, WORKER_LABEL, _service_specs


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
