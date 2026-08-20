import ast
import fcntl
import json
import os
import platform
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts import h3_control
from h3_bridge.job_registry import (
    activate_job,
    finish_job,
    register_starting_job,
    registered_jobs,
)
from h3_bridge import runner as runner_module
from h3_bridge.locking import publication_control_guard
from h3_bridge.scheduler import process_group_stopped, process_start_signature


ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def held_generation_lock(
    project_root: Path,
    *,
    token: str,
    job_id: str,
    controller_pid: int = 777,
    controller_birth: str = "controller-birth",
):
    lock_path = project_root / "runtime" / "h3-generation.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = lock_path.open("a+", encoding="utf-8")
    lock.write(
        json.dumps(
            {
                "schema_version": 1,
                "controller_pid": controller_pid,
                "controller_start_signature": controller_birth,
                "registration_token": token,
                "job_id": job_id,
            }
        )
    )
    lock.flush()
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        yield lock
    finally:
        if not lock.closed:
            lock.close()


def test_every_shell_script_parses():
    scripts = [
        *sorted(ROOT.glob("*.command")),
        *sorted((ROOT / "scripts").glob("*.sh")),
    ]

    for script in scripts:
        subprocess.run(["bash", "-n", str(script)], check=True)


def test_guardian_rebinds_display_link_after_screen_reconfiguration():
    source = (ROOT / "native" / "H3Guardian.swift").read_text(encoding="utf-8")

    assert "func rebuildDisplayLink()" in source
    assert "link.invalidate()" in source
    assert "CVDisplayLinkStop(link)" in source
    assert (
        "selector: #selector(GuardianProbe.rebuildDisplayLink)" in source
    )


def test_installer_is_pinned_and_model_tooling_is_isolated():
    install = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    download = (ROOT / "scripts" / "download_model.sh").read_text(encoding="utf-8")
    doctor = (ROOT / "scripts" / "doctor.sh").read_text(encoding="utf-8")
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    versions = (ROOT / "versions.env").read_text(encoding="utf-8")

    assert "runtime/model-tools-venv" in download
    assert 'huggingface_hub==$HUGGINGFACE_HUB_VERSION' in install
    assert "H3_MODEL_REF=" in versions
    assert "HUGGINGFACE_HUB_VERSION=1.26.0" in versions
    assert "MACOSX_DEPLOYMENT_TARGET=15.0" in install
    assert "xcrun --sdk macosx --show-sdk-version" in install
    assert '"$sdk_major" -ge 26' in install
    assert "xcrun --sdk macosx --show-sdk-version" in doctor
    assert '"$sdk_major" -ge 26' in doctor
    assert install.count('-m venv --clear "$') == 2
    assert "huggingface_hub==" not in requirements


def test_h3_control_uses_only_the_locked_shared_signal_helper():
    """Direct controls are serialized; raw killpg calls remain forbidden."""

    control_path = ROOT / "scripts" / "h3_control.py"
    tree = ast.parse(control_path.read_text(encoding="utf-8"), filename=str(control_path))
    shared_signal_calls = 0

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "signal_process_group":
                shared_signal_calls += 1
            if isinstance(node.func, ast.Attribute) and node.func.attr == "killpg":
                raise AssertionError("h3_control must use the shared signal helper")

    assert shared_signal_calls == 1


def test_pause_serializes_intent_and_immediate_stop(tmp_path: Path):
    updates: list[dict[str, object]] = []
    status = {"pgid": 4242, "state": "running"}
    selected_config = SimpleNamespace()
    with patch.object(
        h3_control, "PROJECT_ROOT", tmp_path
    ), patch.object(
        h3_control, "load_config", return_value=selected_config
    ), patch.object(
        h3_control,
        "_active_job_candidates",
        return_value=[(tmp_path, status, None)],
    ), patch.object(
        h3_control, "_legacy_control_authorized", return_value=True
    ), patch.object(
        h3_control,
        "update_control",
        side_effect=lambda _job, **values: updates.append(values) or True,
    ):
        assert h3_control.control("pause") == 0

    assert updates == [
        {"pgid": 4242, "selected_signal": signal.SIGSTOP, "paused": True}
    ]


def test_process_match_searches_whole_group_for_h3(tmp_path: Path):
    binary = tmp_path / "h3"
    output = f"/usr/bin/caffeinate -i\n{binary} --prompt cat\n"
    completed = subprocess.CompletedProcess([], 0, stdout=output, stderr="")
    with patch.object(h3_control.subprocess, "run", return_value=completed):
        assert h3_control.process_matches_h3(4242, binary)


def test_process_birth_signature_is_locale_independent():
    calls: list[dict[str, str]] = []

    def run(*_args, **kwargs):
        calls.append(kwargs["env"])
        return subprocess.CompletedProcess([], 0, stdout="Mon Jan  1 00:00:00 2024\n")

    with patch("h3_bridge.scheduler.platform.system", return_value="Darwin"), patch(
        "h3_bridge.scheduler.subprocess.run", side_effect=run
    ):
        with patch.dict(os.environ, {"LC_ALL": "zh_CN.UTF-8", "LANG": "zh_CN.UTF-8"}):
            first = process_start_signature(4242)
        with patch.dict(os.environ, {"LC_ALL": "fr_FR.UTF-8", "LANG": "fr_FR.UTF-8"}):
            second = process_start_signature(4242)

    assert first == second == "Mon Jan  1 00:00:00 2024"
    assert all(call["LC_ALL"] == "C" and call["LANG"] == "C" for call in calls)


def test_control_updates_are_serialized_without_lost_fields(tmp_path: Path):
    (tmp_path / "control.json").write_text(
        json.dumps({"paused": False, "policy": "auto"}),
        encoding="utf-8",
    )
    ready = threading.Barrier(3)

    def update(**values: object) -> None:
        ready.wait()
        h3_control.update_control(tmp_path, **values)

    first = threading.Thread(target=update, kwargs={"paused": True})
    second = threading.Thread(target=update, kwargs={"policy": "low"})
    first.start()
    second.start()
    ready.wait()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    value = json.loads((tmp_path / "control.json").read_text(encoding="utf-8"))
    assert value["paused"] is True
    assert value["policy"] == "low"


def write_job_status(tmp_path: Path, **overrides: object) -> Path:
    job_dir = tmp_path / "runtime" / "ComfyUI" / "output" / "h3-jobs" / "job"
    job_dir.mkdir(parents=True)
    value: dict[str, object] = {
        "pgid": 4242,
        "state": "running",
        "updated_at": time.time(),
        "process_start_signature": "expected-birth",
    }
    value.update(overrides)
    (job_dir / "process.json").write_text(json.dumps(value), encoding="utf-8")
    return job_dir


def test_active_jobs_rejects_reused_pid_with_birth_mismatch(tmp_path: Path):
    write_job_status(tmp_path)
    selected_config = SimpleNamespace(
        output_subdir="h3-jobs",
        h3_binary=tmp_path / "h3",
        auto_metrics_poll_seconds=2.0,
    )
    with patch.object(h3_control, "PROJECT_ROOT", tmp_path), patch.object(
        h3_control, "load_config", return_value=selected_config
    ), patch.object(
        h3_control, "process_start_signature", return_value="different-birth"
    ), patch.object(h3_control, "process_group_alive", return_value=True), patch.object(
        h3_control, "process_matches_h3", return_value=True
    ):
        assert h3_control.active_jobs() == []


def test_active_jobs_accepts_stale_status_with_matching_birth(tmp_path: Path):
    job_dir = write_job_status(tmp_path, updated_at=0)
    selected_config = SimpleNamespace(
        output_subdir="h3-jobs",
        h3_binary=tmp_path / "h3",
        auto_metrics_poll_seconds=2.0,
    )
    with patch.object(h3_control, "PROJECT_ROOT", tmp_path), patch.object(
        h3_control, "load_config", return_value=selected_config
    ), patch.object(
        h3_control, "process_start_signature", return_value="expected-birth"
    ), patch.object(h3_control, "process_group_alive", return_value=True), patch.object(
        h3_control, "process_matches_h3", return_value=True
    ):
        selected = h3_control.active_jobs()

    assert selected[0][0] == job_dir


def test_active_jobs_discovers_registered_custom_output(tmp_path: Path):
    job_id = "a" * 20
    job_dir = (tmp_path / "elsewhere" / "h3-jobs" / job_id).resolve()
    job_dir.mkdir(parents=True)
    registration = register_starting_job(
        tmp_path,
        job_dir,
        job_id,
        "h3-jobs",
        controller_pid=777,
        controller_start_signature="controller-birth",
    )
    activate_job(
        tmp_path,
        registration.entry_path,
        registration.token,
        pgid=4242,
        process_start_signature="engine-birth",
    )
    status = {
        "pgid": 4242,
        "controller_pid": 777,
        "state": "running",
        "updated_at": 0,
        "process_start_signature": "engine-birth",
        "controller_start_signature": "controller-birth",
    }
    (job_dir / "process.json").write_text(json.dumps(status), encoding="utf-8")
    selected_config = SimpleNamespace(
        output_subdir="h3-jobs",
        h3_binary=tmp_path / "h3",
        auto_metrics_poll_seconds=2.0,
    )
    try:
        with patch.object(h3_control, "PROJECT_ROOT", tmp_path), patch.object(
            h3_control, "load_config", return_value=selected_config
        ), patch.object(
            h3_control, "process_start_signature", return_value="engine-birth"
        ), patch.object(h3_control, "process_group_alive", return_value=True), patch.object(
            h3_control, "process_matches_h3"
        ) as process_match:
            # A stale registry/status pair without its inherited generation
            # lock is never displayed as controllable.
            assert h3_control.active_jobs() == []
            with held_generation_lock(
                tmp_path, token=registration.token, job_id=job_id
            ):
                assert h3_control.active_jobs() == [(job_dir, status)]
        process_match.assert_not_called()
    finally:
        finish_job(registration, "test-cleanup")


@pytest.mark.parametrize(
    ("lock_token", "process_birth", "leader_exists", "expected"),
    [
        ("0" * 32, "engine-birth", False, False),
        ("registration", "replacement-birth", False, False),
        ("registration", "", True, False),
        ("registration", "", False, True),
    ],
    ids=["lock-mismatch", "pgid-reused", "ps-failed", "leaderless-valid"],
)
def test_registered_active_control_requires_lock_and_process_identity(
    tmp_path: Path,
    lock_token: str,
    process_birth: str,
    leader_exists: bool,
    expected: bool,
):
    job_id = "b" * 20
    job_dir = (tmp_path / "elsewhere" / "h3-jobs" / job_id).resolve()
    job_dir.mkdir(parents=True)
    registration = register_starting_job(
        tmp_path,
        job_dir,
        job_id,
        "h3-jobs",
        controller_pid=777,
        controller_start_signature="controller-birth",
    )
    activate_job(
        tmp_path,
        registration.entry_path,
        registration.token,
        pgid=4242,
        process_start_signature="engine-birth",
    )
    status = {
        "pgid": 4242,
        "controller_pid": 777,
        "state": "running",
        "updated_at": time.time(),
        "process_start_signature": "engine-birth",
        "controller_start_signature": "controller-birth",
    }
    (job_dir / "process.json").write_text(json.dumps(status), encoding="utf-8")
    selected_config = SimpleNamespace(
        output_subdir="h3-jobs",
        h3_binary=tmp_path / "h3",
        auto_metrics_poll_seconds=2.0,
    )
    selected_token = registration.token if lock_token == "registration" else lock_token
    try:
        with held_generation_lock(
            tmp_path, token=selected_token, job_id=job_id
        ), patch.object(h3_control, "PROJECT_ROOT", tmp_path), patch.object(
            h3_control, "load_config", return_value=selected_config
        ), patch.object(
            h3_control, "process_start_signature", return_value=process_birth
        ), patch.object(
            h3_control, "process_alive", return_value=leader_exists
        ), patch.object(
            h3_control, "process_group_alive", return_value=True
        ), patch.object(h3_control, "process_matches_h3") as process_match:
            selected = h3_control.active_jobs()

        assert bool(selected) is expected
        process_match.assert_not_called()
    finally:
        finish_job(registration, "test-cleanup")


def test_control_reauthorizes_registered_job_immediately_before_signal(
    tmp_path: Path,
):
    job_id = "4" * 20
    job_dir = (tmp_path / "custom" / "h3-jobs" / job_id).resolve()
    job_dir.mkdir(parents=True)
    registration = register_starting_job(
        tmp_path,
        job_dir,
        job_id,
        "h3-jobs",
        controller_pid=777,
        controller_start_signature="controller-birth",
    )
    activate_job(
        tmp_path,
        registration.entry_path,
        registration.token,
        pgid=4242,
        process_start_signature="engine-birth",
    )
    status = {
        "pgid": 4242,
        "controller_pid": 777,
        "state": "running",
        "updated_at": time.time(),
        "process_start_signature": "engine-birth",
        "controller_start_signature": "controller-birth",
    }
    (job_dir / "process.json").write_text(json.dumps(status), encoding="utf-8")
    selected_config = SimpleNamespace(
        output_subdir="h3-jobs",
        h3_binary=tmp_path / "h3",
        auto_metrics_poll_seconds=2.0,
    )
    try:
        with patch.object(h3_control, "PROJECT_ROOT", tmp_path), patch.object(
            h3_control, "load_config", return_value=selected_config
        ), patch.object(
            h3_control,
            "_registered_control_authorized",
            return_value=True,
        ) as list_authorize, patch.object(
            h3_control,
            "_registered_control_authorized_guarded",
            return_value=False,
        ) as signal_authorize, patch.object(h3_control, "update_control") as update:
            assert h3_control.control("pause") == 1

        list_authorize.assert_called_once()
        signal_authorize.assert_called_once()
        update.assert_not_called()
    finally:
        finish_job(registration, "test-cleanup")


def test_registered_custom_output_orphan_requires_matching_identities(tmp_path: Path):
    job_id = "c" * 20
    job_dir = (tmp_path / "custom" / "h3-jobs" / job_id).resolve()
    job_dir.mkdir(parents=True)
    registration = register_starting_job(
        tmp_path,
        job_dir,
        job_id,
        "h3-jobs",
        controller_pid=777,
        controller_start_signature="controller-birth",
    )
    activate_job(
        tmp_path,
        registration.entry_path,
        registration.token,
        pgid=4242,
        process_start_signature="engine-birth",
    )
    status = {
        "pgid": 4242,
        "controller_pid": 777,
        "state": "running",
        "updated_at": time.time(),
        "process_start_signature": "engine-birth",
        "controller_start_signature": "controller-birth",
    }
    selected_config = SimpleNamespace(
        output_subdir="h3-jobs",
        h3_binary=tmp_path / "h3",
        auto_metrics_poll_seconds=2.0,
    )

    def signature(pid: int) -> str:
        return "engine-birth" if pid == 4242 else ""

    try:
        # The registry identity is sufficient during the child-before-exec to
        # first-process.json window; a dead controller cannot strand the lock.
        with patch.object(h3_control, "PROJECT_ROOT", tmp_path), patch.object(
            h3_control, "load_config", return_value=selected_config
        ), patch.object(
            h3_control, "process_start_signature", side_effect=signature
        ), patch.object(h3_control, "process_group_alive", return_value=True), patch.object(
            h3_control, "process_matches_h3", return_value=True
        ), patch.object(h3_control, "process_alive", return_value=False):
            pre_status_orphans = h3_control.orphan_jobs()
        assert pre_status_orphans[0][0] == job_dir
        assert pre_status_orphans[0][1]["process_start_signature"] == "engine-birth"

        (job_dir / "process.json").write_text(json.dumps(status), encoding="utf-8")
        with patch.object(h3_control, "PROJECT_ROOT", tmp_path), patch.object(
            h3_control, "load_config", return_value=selected_config
        ), patch.object(
            h3_control, "process_start_signature", side_effect=signature
        ), patch.object(h3_control, "process_group_alive", return_value=True), patch.object(
            h3_control, "process_matches_h3", return_value=True
        ), patch.object(h3_control, "process_alive", return_value=False):
            selected = h3_control.orphan_jobs()
            assert selected[0][0] == job_dir
            assert all(selected[0][1][key] == value for key, value in status.items())
            assert selected[0][1]["_registry_trusted"] is True
            assert selected[0][1]["_registration_token"] == registration.token

        # A stale/corrupt status cannot redirect the registry to another PID;
        # the current two-sided registry remains the cleanup authority.
        status["pgid"] = 9999
        (job_dir / "process.json").write_text(json.dumps(status), encoding="utf-8")
        with patch.object(h3_control, "PROJECT_ROOT", tmp_path), patch.object(
            h3_control, "load_config", return_value=selected_config
        ), patch.object(
            h3_control, "process_group_alive", return_value=True
        ), patch.object(
            h3_control, "process_start_signature", side_effect=signature
        ), patch.object(h3_control, "process_alive", return_value=False):
            selected = h3_control.orphan_jobs()
        assert selected[0][0] == job_dir
        assert selected[0][1]["pgid"] == 4242
        assert selected[0][1]["_registry_only"] is True
    finally:
        finish_job(registration, "test-cleanup")


def test_orphan_detection_requires_complete_exact_birth_fingerprints(tmp_path: Path):
    job_dir = write_job_status(
        tmp_path,
        controller_pid=777,
        controller_start_signature="old-controller-birth",
    )
    selected_config = SimpleNamespace(
        output_subdir="h3-jobs",
        h3_binary=tmp_path / "h3",
        auto_metrics_poll_seconds=2.0,
    )

    def signature(pid: int) -> str:
        return "expected-birth" if pid == 4242 else "reused-controller-birth"

    with patch.object(h3_control, "PROJECT_ROOT", tmp_path), patch.object(
        h3_control, "load_config", return_value=selected_config
    ), patch.object(
        h3_control, "process_start_signature", side_effect=signature
    ), patch.object(h3_control, "process_group_alive", return_value=True), patch.object(
        h3_control, "process_matches_h3", return_value=True
    ), patch.object(h3_control, "process_alive", return_value=True):
        selected = h3_control.orphan_jobs()

    assert selected == [(job_dir, json.loads((job_dir / "process.json").read_text()))]

    status = json.loads((job_dir / "process.json").read_text())
    status.pop("controller_start_signature")
    (job_dir / "process.json").write_text(json.dumps(status))
    with patch.object(h3_control, "PROJECT_ROOT", tmp_path), patch.object(
        h3_control, "load_config", return_value=selected_config
    ), patch.object(h3_control, "process_group_alive") as group_alive:
        assert h3_control.orphan_jobs() == []
    group_alive.assert_not_called()


def test_cleanup_orphan_continues_before_terminating_stopped_group(tmp_path: Path):
    status = {
        "pgid": 4242,
        "state": "paused",
        "process_start_signature": "engine-birth",
    }
    (tmp_path / "process.json").write_text(json.dumps(status), encoding="utf-8")
    updates: list[dict[str, object]] = []
    with patch.object(h3_control, "PROJECT_ROOT", tmp_path), patch.object(
        h3_control, "orphan_jobs", return_value=[(tmp_path, status)]
    ), patch.object(
        h3_control,
        "update_control",
        side_effect=lambda _job, **values: updates.append(values) or True,
    ), patch.object(
        h3_control, "_legacy_control_authorized", return_value=True
    ), patch.object(h3_control, "process_group_alive", return_value=False):
        assert h3_control.cleanup_orphans() == 1

    assert updates == [
        {"pgid": 4242, "selected_signal": signal.SIGCONT, "paused": False},
        {"pgid": 4242, "selected_signal": signal.SIGTERM},
    ]
    final = json.loads((tmp_path / "process.json").read_text(encoding="utf-8"))
    assert final["state"] == "orphan-terminated"


def test_registered_orphan_with_free_lock_never_signals_reused_pgid(
    tmp_path: Path,
):
    job_id = "f" * 20
    job_dir = (tmp_path / "custom" / "h3-jobs" / job_id).resolve()
    job_dir.mkdir(parents=True)
    registration = register_starting_job(
        tmp_path,
        job_dir,
        job_id,
        "h3-jobs",
        controller_pid=777,
        controller_start_signature="controller-birth",
    )
    activate_job(
        tmp_path,
        registration.entry_path,
        registration.token,
        pgid=4242,
        process_start_signature="engine-birth",
    )
    status = {
        "pgid": 4242,
        "controller_pid": 777,
        "state": "running",
        "process_start_signature": "engine-birth",
        "controller_start_signature": "controller-birth",
    }
    (job_dir / "process.json").write_text(json.dumps(status), encoding="utf-8")
    selected_config = SimpleNamespace(
        output_subdir="h3-jobs",
        h3_binary=tmp_path / "h3",
        auto_metrics_poll_seconds=2.0,
    )
    updates: list[dict[str, object]] = []
    try:
        with patch.object(h3_control, "PROJECT_ROOT", tmp_path), patch.object(
            h3_control, "load_config", return_value=selected_config
        ), patch.object(
            h3_control, "orphan_jobs", return_value=[(job_dir, status)]
        ), patch.object(
            h3_control,
            "update_control",
            side_effect=lambda _job, **values: updates.append(values),
        ), patch.object(
            h3_control, "process_start_signature", return_value=""
        ), patch.object(h3_control, "process_group_alive", return_value=True):
            assert h3_control.cleanup_orphans() == 1
        assert updates == []
        assert not registration.entry_path.exists()
        final = json.loads((job_dir / "process.json").read_text(encoding="utf-8"))
        assert final["state"] == "orphan-stale"
    finally:
        finish_job(registration, "test-cleanup")


def test_registered_terminal_write_stays_inside_lock_before_new_token(
    tmp_path: Path,
):
    job_id = "2" * 20
    job_dir = (tmp_path / "custom" / "h3-jobs" / job_id).resolve()
    job_dir.mkdir(parents=True)
    old = register_starting_job(
        tmp_path,
        job_dir,
        job_id,
        "h3-jobs",
        controller_pid=777,
        controller_start_signature="old-controller",
    )
    activate_job(
        tmp_path,
        old.entry_path,
        old.token,
        pgid=4242,
        process_start_signature="old-engine",
    )
    old_status = {
        "pgid": 4242,
        "controller_pid": 777,
        "state": "running",
        "process_start_signature": "old-engine",
        "controller_start_signature": "old-controller",
    }
    (job_dir / "process.json").write_text(
        json.dumps(old_status), encoding="utf-8"
    )
    selected_config = SimpleNamespace(
        output_subdir="h3-jobs",
        h3_binary=tmp_path / "h3",
        auto_metrics_poll_seconds=2.0,
    )
    new_registrations = []
    new_status = {
        "pgid": 5252,
        "controller_pid": 888,
        "state": "running",
        "process_start_signature": "new-engine",
        "controller_start_signature": "new-controller",
    }

    @contextmanager
    def observation():
        yield True, {}
        # This runs exactly when the observation lock is released. A terminal
        # write performed after the with-block would corrupt this new job.
        new = register_starting_job(
            tmp_path,
            job_dir,
            job_id,
            "h3-jobs",
            controller_pid=888,
            controller_start_signature="new-controller",
        )
        activate_job(
            tmp_path,
            new.entry_path,
            new.token,
            pgid=5252,
            process_start_signature="new-engine",
        )
        new_registrations.append(new)
        (job_dir / "process.json").write_text(
            json.dumps(new_status), encoding="utf-8"
        )

    updates: list[dict[str, object]] = []
    try:
        with patch.object(h3_control, "PROJECT_ROOT", tmp_path), patch.object(
            h3_control, "load_config", return_value=selected_config
        ), patch.object(
            h3_control, "orphan_jobs", return_value=[(job_dir, old_status)]
        ), patch.object(
            h3_control, "generation_lock_observation", new=observation
        ), patch.object(
            h3_control,
            "update_control",
            side_effect=lambda _job, **values: updates.append(values),
        ):
            assert h3_control.cleanup_orphans() == 1

        assert updates == []
        assert json.loads(
            (job_dir / "process.json").read_text(encoding="utf-8")
        ) == new_status
        current = list(registered_jobs(tmp_path, "h3-jobs"))
        assert len(current) == 1
        assert current[0].token == new_registrations[0].token
    finally:
        finish_job(old, "test-cleanup")
        for new in new_registrations:
            finish_job(new, "test-cleanup")


def test_registered_leaderless_group_waits_for_delayed_lock_release(
    tmp_path: Path,
):
    job_id = "1" * 20
    job_dir = (tmp_path / "custom" / "h3-jobs" / job_id).resolve()
    job_dir.mkdir(parents=True)
    registration = register_starting_job(
        tmp_path,
        job_dir,
        job_id,
        "h3-jobs",
        controller_pid=777,
        controller_start_signature="controller-birth",
    )
    activate_job(
        tmp_path,
        registration.entry_path,
        registration.token,
        pgid=4242,
        process_start_signature="engine-birth",
    )
    status = {
        "pgid": 4242,
        "controller_pid": 777,
        "state": "running",
        "process_start_signature": "engine-birth",
        "controller_start_signature": "controller-birth",
    }
    (job_dir / "process.json").write_text(json.dumps(status), encoding="utf-8")
    selected_config = SimpleNamespace(
        output_subdir="h3-jobs",
        h3_binary=tmp_path / "h3",
        auto_metrics_poll_seconds=2.0,
    )
    lock_path = tmp_path / "runtime" / "h3-generation.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = lock_path.open("a+", encoding="utf-8")
    lock.write(
        json.dumps(
            {
                "schema_version": 1,
                "controller_pid": 777,
                "controller_start_signature": "controller-birth",
                "registration_token": registration.token,
                "job_id": job_id,
            }
        )
    )
    lock.flush()
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    group_checks = 0

    def group_alive(_pgid: int) -> bool:
        nonlocal group_checks
        group_checks += 1
        return group_checks <= 6

    updates: list[dict[str, object]] = []
    release_timer: threading.Timer | None = None

    def update(_job: Path, **values: object) -> bool:
        nonlocal release_timer
        updates.append(values)
        if values.get("selected_signal") == signal.SIGTERM:
            # Model the final inherited descriptor closing when the remaining
            # leaderless child exits shortly after TERM returns.
            release_timer = threading.Timer(0.15, lock.close)
            release_timer.start()
        return True

    try:
        with patch.object(h3_control, "PROJECT_ROOT", tmp_path), patch.object(
            h3_control, "load_config", return_value=selected_config
        ), patch.object(
            h3_control, "orphan_jobs", return_value=[(job_dir, status)]
        ), patch.object(
            h3_control, "process_start_signature", return_value=""
        ), patch.object(h3_control, "process_alive", return_value=False), patch.object(
            h3_control, "process_group_alive", side_effect=group_alive
        ), patch.object(
            h3_control,
            "update_control",
            side_effect=update,
        ):
            assert h3_control.cleanup_orphans() == 1
        assert [value["selected_signal"] for value in updates] == [
            signal.SIGCONT,
            signal.SIGTERM,
        ]
        assert not registration.entry_path.exists()
    finally:
        if release_timer is not None:
            release_timer.join(timeout=1)
        if not lock.closed:
            lock.close()
        finish_job(registration, "test-cleanup")


def test_registered_cleanup_waits_for_delayed_lock_release_after_kill(
    tmp_path: Path,
):
    job_id = "3" * 20
    job_dir = (tmp_path / "custom" / "h3-jobs" / job_id).resolve()
    job_dir.mkdir(parents=True)
    registration = register_starting_job(
        tmp_path,
        job_dir,
        job_id,
        "h3-jobs",
        controller_pid=777,
        controller_start_signature="controller-birth",
    )
    activate_job(
        tmp_path,
        registration.entry_path,
        registration.token,
        pgid=4242,
        process_start_signature="engine-birth",
    )
    status = {
        "pgid": 4242,
        "controller_pid": 777,
        "state": "running",
        "process_start_signature": "engine-birth",
        "controller_start_signature": "controller-birth",
    }
    (job_dir / "process.json").write_text(json.dumps(status), encoding="utf-8")
    selected_config = SimpleNamespace(
        output_subdir="h3-jobs",
        h3_binary=tmp_path / "h3",
        auto_metrics_poll_seconds=2.0,
    )
    lock_path = tmp_path / "runtime" / "h3-generation.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = lock_path.open("a+", encoding="utf-8")
    lock.write(
        json.dumps(
            {
                "schema_version": 1,
                "controller_pid": 777,
                "controller_start_signature": "controller-birth",
                "registration_token": registration.token,
                "job_id": job_id,
            }
        )
    )
    lock.flush()
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    phase = ["term"]
    term_group_checks = [0]
    release_timer: threading.Timer | None = None
    updates: list[dict[str, object]] = []

    def group_alive(_pgid: int) -> bool:
        if phase[0] == "killed":
            return False
        term_group_checks[0] += 1
        # Initial identity succeeds, the outer TERM wait observes leader exit,
        # then the still-locked descendants remain signalable until KILL.
        return term_group_checks[0] != 4

    def update(_job: Path, **values: object) -> bool:
        nonlocal release_timer
        updates.append(values)
        if values.get("selected_signal") == signal.SIGKILL:
            phase[0] = "killed"
            release_timer = threading.Timer(0.15, lock.close)
            release_timer.start()
        return True

    try:
        with patch.object(h3_control, "PROJECT_ROOT", tmp_path), patch.object(
            h3_control, "load_config", return_value=selected_config
        ), patch.object(
            h3_control, "orphan_jobs", return_value=[(job_dir, status)]
        ), patch.object(
            h3_control,
            "process_start_signature",
            return_value="engine-birth",
        ), patch.object(
            h3_control, "process_group_alive", side_effect=group_alive
        ), patch.object(h3_control, "update_control", side_effect=update):
            assert h3_control.cleanup_orphans() == 1

        assert [value["selected_signal"] for value in updates] == [
            signal.SIGCONT,
            signal.SIGTERM,
            signal.SIGKILL,
        ]
        assert not registration.entry_path.exists()
    finally:
        if release_timer is not None:
            release_timer.join(timeout=1)
        if not lock.closed:
            lock.close()
        finish_job(registration, "test-cleanup")


def test_original_group_remains_live_when_leader_is_gone_but_children_remain():
    with patch.object(h3_control, "process_group_alive", return_value=True), patch.object(
        h3_control, "process_start_signature", return_value=""
    ):
        assert h3_control.original_process_group_alive(4242, "engine-birth")

    # A different non-empty leader birth means the old group is gone and the
    # numeric ID was reused; Control must not signal the replacement.
    with patch.object(h3_control, "process_group_alive", return_value=True), patch.object(
        h3_control, "process_start_signature", return_value="replacement-birth"
    ):
        assert not h3_control.original_process_group_alive(4242, "engine-birth")


def test_runner_first_lock_publication_blocks_observer_until_metadata_is_new(
    tmp_path: Path,
):
    runner = runner_module.H3Runner(SimpleNamespace(project_root=tmp_path))
    publish_entered = threading.Event()
    allow_publish = threading.Event()
    publisher_holds_generation = threading.Event()
    release_generation = threading.Event()
    observer_started = threading.Event()
    observer_done = threading.Event()
    observation: list[tuple[bool, dict[str, object]]] = []
    errors: list[BaseException] = []
    real_write = runner_module._write_generation_lock_metadata

    def delayed_write(lock_fd: int, **values: object) -> None:
        publish_entered.set()
        assert allow_publish.wait(5)
        real_write(lock_fd, **values)

    def publish() -> None:
        try:
            with runner._generation_lock():
                publisher_holds_generation.set()
                assert release_generation.wait(5)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    def observe() -> None:
        try:
            observer_started.set()
            with h3_control.generation_lock_observation() as value:
                observation.append(value)
            observer_done.set()
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    with patch.object(h3_control, "PROJECT_ROOT", tmp_path), patch.object(
        runner_module, "process_start_signature", return_value="controller-birth"
    ), patch.object(
        runner_module, "_write_generation_lock_metadata", side_effect=delayed_write
    ):
        publisher = threading.Thread(target=publish)
        publisher.start()
        assert publish_entered.wait(5)
        observer = threading.Thread(target=observe)
        observer.start()
        assert observer_started.wait(5)
        assert not observer_done.wait(0.15)
        allow_publish.set()
        assert publisher_holds_generation.wait(5)
        assert observer_done.wait(5)
        release_generation.set()
        publisher.join(timeout=5)
        observer.join(timeout=5)

    assert not publisher.is_alive() and not observer.is_alive()
    assert errors == []
    lock_available, metadata = observation[0]
    assert lock_available is False
    assert metadata["controller_start_signature"] == "controller-birth"


def test_cleanup_observer_cannot_make_stale_metadata_look_authorized(
    tmp_path: Path,
):
    job_id = "5" * 20
    job_dir = (tmp_path / "custom" / "h3-jobs" / job_id).resolve()
    job_dir.mkdir(parents=True)
    registration = register_starting_job(
        tmp_path,
        job_dir,
        job_id,
        "h3-jobs",
        controller_pid=777,
        controller_start_signature="controller-birth",
    )
    activate_job(
        tmp_path,
        registration.entry_path,
        registration.token,
        pgid=4242,
        process_start_signature="engine-birth",
    )
    status = {
        "pgid": 4242,
        "controller_pid": 777,
        "state": "running",
        "updated_at": time.time(),
        "process_start_signature": "engine-birth",
        "controller_start_signature": "controller-birth",
    }
    (job_dir / "process.json").write_text(json.dumps(status), encoding="utf-8")
    lock_path = tmp_path / "runtime" / "h3-generation.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "controller_pid": 777,
                "controller_start_signature": "controller-birth",
                "registration_token": registration.token,
                "job_id": job_id,
            }
        ),
        encoding="utf-8",
    )
    config = SimpleNamespace(
        output_subdir="h3-jobs",
        h3_binary=tmp_path / "h3",
        auto_metrics_poll_seconds=2.0,
    )
    observer_entered = threading.Event()
    release_observer = threading.Event()
    control_started = threading.Event()
    control_done = threading.Event()
    selected: list[list[tuple[Path, dict[str, object]]]] = []
    errors: list[BaseException] = []

    def cleanup_observer() -> None:
        try:
            with h3_control.generation_lock_observation() as (available, _metadata):
                assert available
                observer_entered.set()
                assert release_observer.wait(5)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    def list_control() -> None:
        try:
            control_started.set()
            selected.append(h3_control.active_jobs())
            control_done.set()
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    try:
        with patch.object(h3_control, "PROJECT_ROOT", tmp_path), patch.object(
            h3_control, "load_config", return_value=config
        ), patch.object(
            h3_control, "process_start_signature", return_value=""
        ), patch.object(h3_control, "process_alive", return_value=False), patch.object(
            h3_control, "process_group_alive", return_value=True
        ):
            observer = threading.Thread(target=cleanup_observer)
            observer.start()
            assert observer_entered.wait(5)
            controller = threading.Thread(target=list_control)
            controller.start()
            assert control_started.wait(5)
            assert not control_done.wait(0.15)
            release_observer.set()
            observer.join(timeout=5)
            controller.join(timeout=5)
        assert errors == []
        assert selected == [[]]
    finally:
        finish_job(registration, "test-cleanup")


def test_cleanup_finalization_blocks_new_generation_publication(tmp_path: Path):
    job_id = "6" * 20
    job_dir = (tmp_path / "custom" / "h3-jobs" / job_id).resolve()
    job_dir.mkdir(parents=True)
    registration = register_starting_job(
        tmp_path,
        job_dir,
        job_id,
        "h3-jobs",
        controller_pid=777,
        controller_start_signature="controller-birth",
    )
    activate_job(
        tmp_path,
        registration.entry_path,
        registration.token,
        pgid=4242,
        process_start_signature="engine-birth",
    )
    registered = list(registered_jobs(tmp_path, "h3-jobs"))[0]
    config = SimpleNamespace(output_subdir="h3-jobs")
    finalizer_entered = threading.Event()
    release_finalizer = threading.Event()
    publisher_started = threading.Event()
    publisher_entered = threading.Event()
    cleanup_result: list[str] = []
    registry_seen_by_publisher: list[bool] = []
    errors: list[BaseException] = []

    def finalize() -> bool:
        finalizer_entered.set()
        assert release_finalizer.wait(5)
        return h3_control._finalize_registered_locked(
            config,
            registered,
            state="orphan-stale",
            reason="test-finalize",
        )

    def cleanup() -> None:
        try:
            cleanup_result.append(
                h3_control._registered_cleanup_state(registered, on_gone=finalize)
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    def publish() -> None:
        try:
            publisher_started.set()
            with publication_control_guard(tmp_path):
                publisher_entered.set()
                registry_seen_by_publisher.append(registered.entry_path.exists())
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    try:
        with patch.object(h3_control, "PROJECT_ROOT", tmp_path):
            cleanup_thread = threading.Thread(target=cleanup)
            cleanup_thread.start()
            assert finalizer_entered.wait(5)
            publisher_thread = threading.Thread(target=publish)
            publisher_thread.start()
            assert publisher_started.wait(5)
            assert not publisher_entered.wait(0.15)
            release_finalizer.set()
            cleanup_thread.join(timeout=5)
            publisher_thread.join(timeout=5)
        assert errors == []
        assert cleanup_result == ["finalized"]
        assert registry_seen_by_publisher == [False]
    finally:
        finish_job(registration, "test-cleanup")


def test_authorization_through_signal_blocks_new_publication(tmp_path: Path):
    job_id = "7" * 20
    job_dir = (tmp_path / "custom" / "h3-jobs" / job_id).resolve()
    job_dir.mkdir(parents=True)
    registration = register_starting_job(
        tmp_path,
        job_dir,
        job_id,
        "h3-jobs",
        controller_pid=777,
        controller_start_signature="controller-birth",
    )
    activate_job(
        tmp_path,
        registration.entry_path,
        registration.token,
        pgid=4242,
        process_start_signature="engine-birth",
    )
    registered = list(registered_jobs(tmp_path, "h3-jobs"))[0]
    status = {
        "pgid": 4242,
        "process_start_signature": "engine-birth",
    }
    config = SimpleNamespace(output_subdir="h3-jobs")
    update_entered = threading.Event()
    release_update = threading.Event()
    publisher_started = threading.Event()
    publisher_entered = threading.Event()
    control_result: list[bool] = []
    errors: list[BaseException] = []

    def update(_job: Path, **_values: object) -> bool:
        update_entered.set()
        assert release_update.wait(5)
        return True

    def authorize_and_signal() -> None:
        try:
            control_result.append(
                h3_control._authorized_update_control(
                    config,
                    job_dir,
                    status,
                    registered,
                    pgid=4242,
                    selected_signal=signal.SIGSTOP,
                    paused=True,
                )
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    def publish() -> None:
        try:
            publisher_started.set()
            with publication_control_guard(tmp_path):
                publisher_entered.set()
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    try:
        with held_generation_lock(
            tmp_path, token=registration.token, job_id=job_id
        ), patch.object(h3_control, "PROJECT_ROOT", tmp_path), patch.object(
            h3_control, "_original_group_state", return_value="exact"
        ), patch.object(h3_control, "update_control", side_effect=update):
            control_thread = threading.Thread(target=authorize_and_signal)
            control_thread.start()
            assert update_entered.wait(5)
            publisher_thread = threading.Thread(target=publish)
            publisher_thread.start()
            assert publisher_started.wait(5)
            assert not publisher_entered.wait(0.15)
            release_update.set()
            control_thread.join(timeout=5)
            publisher_thread.join(timeout=5)
        assert errors == []
        assert control_result == [True]
    finally:
        finish_job(registration, "test-cleanup")


def test_cleanup_reauthorizes_each_legacy_candidate_and_deduplicates_identity(
    tmp_path: Path,
):
    first = tmp_path / "first"
    second = tmp_path / "second"
    duplicate = tmp_path / "duplicate"
    for path in (first, second, duplicate):
        path.mkdir()
        (path / "process.json").write_text("{}", encoding="utf-8")
    first_status = {
        "pgid": 4242,
        "state": "running",
        "updated_at": time.time(),
        "process_start_signature": "first-birth",
    }
    second_status = {
        "pgid": 5252,
        "state": "running",
        "updated_at": time.time(),
        "process_start_signature": "second-birth",
    }
    updates: list[tuple[Path, signal.Signals]] = []
    authorizations = iter([True, True, False])
    config = SimpleNamespace(
        output_subdir="h3-jobs",
        h3_binary=tmp_path / "h3",
        auto_metrics_poll_seconds=2.0,
    )

    def update(job: Path, **values: object) -> bool:
        updates.append((job, values["selected_signal"]))
        return True

    with patch.object(h3_control, "PROJECT_ROOT", tmp_path), patch.object(
        h3_control, "load_config", return_value=config
    ), patch.object(
        h3_control,
        "orphan_jobs",
        return_value=[
            (first, first_status),
            (duplicate, dict(first_status)),
            (second, second_status),
        ],
    ), patch.object(
        h3_control,
        "_legacy_control_authorized",
        side_effect=lambda *_args: next(authorizations),
    ), patch.object(
        h3_control, "_original_group_state", return_value="reused"
    ), patch.object(
        h3_control, "process_group_alive", return_value=False
    ), patch.object(h3_control, "update_control", side_effect=update):
        assert h3_control.cleanup_orphans() == 2

    assert updates == [
        (first, signal.SIGCONT),
        (first, signal.SIGTERM),
    ]


def test_registered_orphan_never_downgrades_to_legacy_after_token_superseded(
    tmp_path: Path,
):
    job_id = "8" * 20
    job_dir = (tmp_path / "custom" / "h3-jobs" / job_id).resolve()
    job_dir.mkdir(parents=True)
    old = register_starting_job(
        tmp_path,
        job_dir,
        job_id,
        "h3-jobs",
        controller_pid=777,
        controller_start_signature="old-controller",
    )
    activate_job(
        tmp_path,
        old.entry_path,
        old.token,
        pgid=4242,
        process_start_signature="old-engine",
    )
    snapshot = {
        "pgid": 4242,
        "controller_pid": 777,
        "state": "running",
        "process_start_signature": "old-engine",
        "controller_start_signature": "old-controller",
        "_registry_trusted": True,
        "_registration_token": old.token,
    }
    finish_job(old, "superseded")
    new = register_starting_job(
        tmp_path,
        job_dir,
        job_id,
        "h3-jobs",
        controller_pid=888,
        controller_start_signature="new-controller",
    )
    activate_job(
        tmp_path,
        new.entry_path,
        new.token,
        pgid=5252,
        process_start_signature="new-engine",
    )
    new_status = {
        "pgid": 5252,
        "controller_pid": 888,
        "state": "running",
        "process_start_signature": "new-engine",
        "controller_start_signature": "new-controller",
    }
    new_control = {"paused": False, "policy": "max"}
    (job_dir / "process.json").write_text(json.dumps(new_status), encoding="utf-8")
    (job_dir / "control.json").write_text(json.dumps(new_control), encoding="utf-8")
    config = SimpleNamespace(
        output_subdir="h3-jobs",
        h3_binary=tmp_path / "h3",
        auto_metrics_poll_seconds=2.0,
    )
    try:
        with patch.object(h3_control, "PROJECT_ROOT", tmp_path), patch.object(
            h3_control, "load_config", return_value=config
        ), patch.object(
            h3_control, "orphan_jobs", return_value=[(job_dir, snapshot)]
        ), patch.object(h3_control, "_legacy_control_authorized") as legacy, patch.object(
            h3_control, "update_control"
        ) as update:
            assert h3_control.cleanup_orphans() == 1
        legacy.assert_not_called()
        update.assert_not_called()
        assert json.loads((job_dir / "process.json").read_text()) == new_status
        assert json.loads((job_dir / "control.json").read_text()) == new_control
    finally:
        finish_job(new, "test-cleanup")


def test_legacy_cleanup_cannot_overwrite_new_registered_job_in_same_directory(
    tmp_path: Path,
):
    job_id = "9" * 20
    job_dir = (tmp_path / "custom" / "h3-jobs" / job_id).resolve()
    job_dir.mkdir(parents=True)
    legacy_snapshot = {
        "pgid": 4242,
        "state": "running",
        "updated_at": time.time(),
        "process_start_signature": "legacy-birth",
    }
    new = register_starting_job(
        tmp_path,
        job_dir,
        job_id,
        "h3-jobs",
        controller_pid=888,
        controller_start_signature="new-controller",
    )
    activate_job(
        tmp_path,
        new.entry_path,
        new.token,
        pgid=5252,
        process_start_signature="new-engine",
    )
    new_status = {
        "pgid": 5252,
        "controller_pid": 888,
        "state": "running",
        "process_start_signature": "new-engine",
        "controller_start_signature": "new-controller",
    }
    new_control = {"paused": False, "policy": "max"}
    (job_dir / "process.json").write_text(json.dumps(new_status), encoding="utf-8")
    (job_dir / "control.json").write_text(json.dumps(new_control), encoding="utf-8")
    config = SimpleNamespace(
        output_subdir="h3-jobs",
        h3_binary=tmp_path / "h3",
        auto_metrics_poll_seconds=2.0,
    )
    try:
        with patch.object(h3_control, "PROJECT_ROOT", tmp_path), patch.object(
            h3_control, "load_config", return_value=config
        ), patch.object(
            h3_control,
            "orphan_jobs",
            return_value=[(job_dir, legacy_snapshot)],
        ), patch.object(
            h3_control, "_legacy_control_authorized", return_value=True
        ) as legacy, patch.object(h3_control, "update_control") as update:
            assert h3_control.cleanup_orphans() == 1
        legacy.assert_not_called()
        update.assert_not_called()
        assert json.loads((job_dir / "process.json").read_text()) == new_status
        assert json.loads((job_dir / "control.json").read_text()) == new_control
    finally:
        finish_job(new, "test-cleanup")


def test_legacy_control_rejects_birth_mismatch_inside_generation_transaction(
    tmp_path: Path,
):
    job_dir = tmp_path / "legacy"
    job_dir.mkdir()
    status = {
        "pgid": 4242,
        "state": "running",
        "updated_at": time.time(),
        "process_start_signature": "old-birth",
    }
    config = SimpleNamespace(
        output_subdir="h3-jobs",
        h3_binary=tmp_path / "h3",
        auto_metrics_poll_seconds=2.0,
    )
    with patch.object(h3_control, "PROJECT_ROOT", tmp_path), patch.object(
        h3_control, "process_group_alive", return_value=True
    ), patch.object(
        h3_control, "process_start_signature", return_value="replacement-birth"
    ), patch.object(h3_control, "process_matches_h3", return_value=True), patch.object(
        h3_control, "update_control"
    ) as update:
        assert not h3_control._authorized_update_control(
            config,
            job_dir,
            status,
            None,
            pgid=4242,
            selected_signal=signal.SIGSTOP,
            paused=True,
        )
    update.assert_not_called()


def test_locked_cli_control_really_stops_and_resumes_group(tmp_path: Path):
    if platform.system() != "Darwin":
        return
    (tmp_path / "control.json").write_text(
        json.dumps({"paused": False, "policy": "max"}),
        encoding="utf-8",
    )
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    try:
        assert h3_control.update_control(
            tmp_path,
            pgid=child.pid,
            selected_signal=signal.SIGSTOP,
            paused=True,
        )
        if process_group_stopped(child.pid) is None:
            pytest.skip("sandbox does not allow ps process-state inspection")
        for _ in range(100):
            if process_group_stopped(child.pid) is True:
                break
            time.sleep(0.01)
        assert process_group_stopped(child.pid) is True

        assert h3_control.update_control(
            tmp_path,
            pgid=child.pid,
            selected_signal=signal.SIGCONT,
            paused=False,
        )
        for _ in range(100):
            if process_group_stopped(child.pid) is False:
                break
            time.sleep(0.01)
        assert process_group_stopped(child.pid) is False
    finally:
        os.killpg(child.pid, signal.SIGCONT)
        os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=5)
