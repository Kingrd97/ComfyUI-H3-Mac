from __future__ import annotations

import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


_SCHEMA_VERSION = 1
_JOB_ID_RE = re.compile(r"(?:[0-9a-f]{20}|vpipe-[0-9a-f]{20})")
_TOKEN_RE = re.compile(r"[0-9a-f]{32}")
_MARKER_NAME = ".h3-job-registry.json"


@dataclass(frozen=True)
class JobRegistration:
    project_root: Path
    job_dir: Path
    job_id: str
    token: str
    entry_path: Path


@dataclass(frozen=True)
class RegisteredJob:
    job_dir: Path
    job_id: str
    token: str
    entry_path: Path
    pgid: int
    process_start_signature: str
    controller_pid: int
    controller_start_signature: str
    engine_profile: str
    activated_at: float
    registry_state: str


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def _registry_root(project_root: Path) -> Path:
    return project_root.resolve() / "runtime" / "job-registry"


def _safe_output_subdir(output_subdir: str) -> bool:
    return (
        bool(output_subdir)
        and output_subdir not in {".", ".."}
        and Path(output_subdir).name == output_subdir
        and "/" not in output_subdir
        and "\\" not in output_subdir
    )


def _canonical_job_dir(job_dir: Path, job_id: str, output_subdir: str) -> Path:
    if not _JOB_ID_RE.fullmatch(job_id):
        raise ValueError("Invalid H3 job ID for registry")
    if not _safe_output_subdir(output_subdir):
        raise ValueError("output_subdir must be one safe path component")
    if not job_dir.is_absolute():
        raise ValueError("H3 registry job directory must be absolute")
    resolved = job_dir.resolve(strict=True)
    if job_dir != resolved or resolved.name != job_id or resolved.parent.name != output_subdir:
        raise ValueError("H3 registry job directory is not canonical")
    return resolved


def register_starting_job(
    project_root: Path,
    job_dir: Path,
    job_id: str,
    output_subdir: str,
    *,
    controller_pid: int,
    controller_start_signature: str,
    engine_profile: str = "low",
) -> JobRegistration:
    """Atomically publish a path before launch, without yet making it signalable."""

    if (
        controller_pid <= 1
        or not controller_start_signature
        or engine_profile not in {"low", "auto", "max"}
    ):
        raise RuntimeError("Cannot register H3 job without controller identity")
    canonical = _canonical_job_dir(job_dir, job_id, output_subdir)
    root = _registry_root(project_root)
    root.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(16)
    entry_path = root / f"{token}.json"
    payload: dict[str, object] = {
        "schema_version": _SCHEMA_VERSION,
        "token": token,
        "job_id": job_id,
        "job_dir": str(canonical),
        "output_subdir": output_subdir,
        "state": "starting",
        "controller_pid": controller_pid,
        "controller_start_signature": controller_start_signature,
        "engine_profile": engine_profile,
        "created_at": time.time(),
    }
    # A second copy in the output directory binds a registry entry to the
    # intended job directory. Readers require both sides before trusting a
    # custom path, so a stray/corrupt registry JSON cannot redirect controls.
    _atomic_json(canonical / _MARKER_NAME, payload)
    _atomic_json(entry_path, payload)
    return JobRegistration(project_root.resolve(), canonical, job_id, token, entry_path)


def activate_job(
    project_root: Path,
    entry_path: Path,
    token: str,
    *,
    pgid: int,
    process_start_signature: str,
) -> None:
    """Publish the engine identity from the child immediately before exec."""

    root = _registry_root(project_root)
    if (
        not _TOKEN_RE.fullmatch(token)
        or entry_path.is_symlink()
        or entry_path.parent.resolve(strict=False) != root
        or entry_path.name != f"{token}.json"
        or pgid <= 1
        or not process_start_signature
    ):
        raise RuntimeError("Unsafe or incomplete H3 registry activation")
    value = _read_json(entry_path)
    if (
        value.get("schema_version") != _SCHEMA_VERSION
        or value.get("token") != token
        or value.get("state") != "starting"
    ):
        raise RuntimeError("H3 registry entry changed before launch")
    value.update(
        {
            "state": "active",
            "pgid": pgid,
            "process_start_signature": process_start_signature,
            "activated_at": time.time(),
        }
    )
    _atomic_json(entry_path, value)


def abandon_starting_job(project_root: Path, entry_path: Path, token: str) -> None:
    """Remove a gated launch whose parent closed the pipe without sending go."""

    root = _registry_root(project_root)
    if (
        not _TOKEN_RE.fullmatch(token)
        or entry_path.is_symlink()
        or entry_path.parent.resolve(strict=False) != root
        or entry_path.name != f"{token}.json"
    ):
        return
    value = _read_json(entry_path)
    if (
        value.get("schema_version") != _SCHEMA_VERSION
        or value.get("token") != token
        or value.get("state") != "starting"
    ):
        return
    try:
        job_id = str(value["job_id"])
        output_subdir = str(value["output_subdir"])
        job_dir = _canonical_job_dir(
            Path(str(value["job_dir"])), job_id, output_subdir
        )
    except (KeyError, OSError, RuntimeError, TypeError, ValueError):
        return
    entry_path.unlink(missing_ok=True)
    marker = job_dir / _MARKER_NAME
    marker_value = _read_json(marker)
    if marker_value.get("token") == token and not marker.is_symlink():
        marker.unlink(missing_ok=True)


def finish_job(registration: JobRegistration, terminal_state: str) -> None:
    """Mark terminal briefly, then remove the active registry and path marker."""

    entry = _read_json(registration.entry_path)
    if entry.get("token") == registration.token and not registration.entry_path.is_symlink():
        entry.update({"state": terminal_state, "finished_at": time.time()})
        _atomic_json(registration.entry_path, entry)
        registration.entry_path.unlink(missing_ok=True)
    marker = registration.job_dir / _MARKER_NAME
    marker_value = _read_json(marker)
    if marker_value.get("token") == registration.token and not marker.is_symlink():
        marker.unlink(missing_ok=True)


def mark_cleanup_needed(registration: JobRegistration, reason: str) -> None:
    """Keep an identified live entry discoverable for Control retries."""

    entry = _read_json(registration.entry_path)
    if (
        entry.get("token") != registration.token
        or registration.entry_path.is_symlink()
    ):
        return
    entry.update(
        {
            "state": "cleanup-needed",
            "cleanup_reason": reason,
            "cleanup_requested_at": time.time(),
        }
    )
    _atomic_json(registration.entry_path, entry)


def _registered_job(
    project_root: Path,
    entry_path: Path,
    output_subdir: str,
) -> RegisteredJob | None:
    root = _registry_root(project_root)
    if entry_path.is_symlink() or entry_path.parent.resolve(strict=False) != root:
        return None
    token = entry_path.stem
    if not _TOKEN_RE.fullmatch(token) or entry_path.name != f"{token}.json":
        return None
    value = _read_json(entry_path)
    try:
        if (
            value.get("schema_version") != _SCHEMA_VERSION
            or value.get("token") != token
            or value.get("state") not in {"active", "cleanup-needed"}
            or value.get("output_subdir") != output_subdir
        ):
            return None
        job_id = str(value["job_id"])
        raw_job_dir = Path(str(value["job_dir"]))
        job_dir = _canonical_job_dir(raw_job_dir, job_id, output_subdir)
        pgid = int(value["pgid"])
        controller_pid = int(value["controller_pid"])
        process_signature = str(value["process_start_signature"])
        controller_signature = str(value["controller_start_signature"])
        engine_profile = str(value["engine_profile"])
        activated_at = float(value["activated_at"])
        registry_state = str(value["state"])
    except (KeyError, OSError, RuntimeError, TypeError, ValueError):
        return None
    marker = job_dir / _MARKER_NAME
    if marker.is_symlink():
        return None
    marker_value = _read_json(marker)
    if (
        marker_value.get("schema_version") != _SCHEMA_VERSION
        or marker_value.get("token") != token
        or marker_value.get("job_id") != job_id
        or Path(str(marker_value.get("job_dir", ""))) != job_dir
        or min(pgid, controller_pid) <= 1
        or not process_signature
        or not controller_signature
        or engine_profile not in {"low", "auto", "max"}
        or not (activated_at > 0)
    ):
        return None
    return RegisteredJob(
        job_dir=job_dir,
        job_id=job_id,
        token=token,
        entry_path=entry_path,
        pgid=pgid,
        process_start_signature=process_signature,
        controller_pid=controller_pid,
        controller_start_signature=controller_signature,
        engine_profile=engine_profile,
        activated_at=activated_at,
        registry_state=registry_state,
    )


def registered_jobs(
    project_root: Path,
    output_subdir: str,
) -> Iterator[RegisteredJob]:
    """Yield only canonical, two-sided, fully identified active entries."""

    if not _safe_output_subdir(output_subdir):
        return
    root = _registry_root(project_root)
    if not root.is_dir() or root.is_symlink():
        return
    for entry_path in sorted(root.glob("*.json")):
        selected = _registered_job(project_root, entry_path, output_subdir)
        if selected is not None:
            yield selected


def remove_registered_job(
    project_root: Path,
    output_subdir: str,
    job_dir: Path,
    *,
    pgid: int,
    process_start_signature: str,
) -> None:
    """Remove an orphan entry only when its complete engine identity matches."""

    target = job_dir.resolve(strict=False)
    for registered in registered_jobs(project_root, output_subdir):
        if (
            registered.job_dir == target
            and registered.pgid == pgid
            and registered.process_start_signature == process_start_signature
        ):
            finish_job(
                JobRegistration(
                    project_root.resolve(),
                    registered.job_dir,
                    registered.job_id,
                    registered.token,
                    registered.entry_path,
                ),
                "orphan-terminated",
            )
