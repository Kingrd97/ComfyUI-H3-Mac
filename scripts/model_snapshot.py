#!/usr/bin/env python3
"""Download and verify a pinned MiniMax-H3 snapshot without duplicate blobs."""

from __future__ import annotations

import argparse
import fcntl
import fnmatch
import json
import os
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence


REPO_ID = "MiniMaxAI/MiniMax-H3"
ROOT_FILES = ("model_index.json", "modular_model_index.json")
GIB = 1024**3
MANIFEST_SCHEMA = 1


def _environment_flag(name: str) -> bool:
    value = os.environ.get(name, "").strip().lower()
    return value not in {"", "0", "false", "no", "off"}


def _probe_relative_symlink(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    probe = Path(tempfile.mkdtemp(prefix=".h3-symlink-probe-", dir=directory))
    target = probe / "target"
    link = probe / "relative-link"
    try:
        target.write_bytes(b"h3")
        link.symlink_to(target.name)
        if (
            not link.is_symlink()
            or link.readlink().is_absolute()
            or link.resolve(strict=True) != target.resolve(strict=True)
        ):
            raise OSError("relative symlink did not resolve to its target")
    except OSError as exc:
        raise RuntimeError(
            f"Filesystem does not support the relative symlinks required for deduplicated "
            f"models: {directory}"
        ) from exc
    finally:
        shutil.rmtree(probe, ignore_errors=True)


def require_deduplicated_storage(model_root: Path, cache_dir: Path) -> None:
    if _environment_flag("HF_HUB_DISABLE_SYMLINKS"):
        raise RuntimeError(
            "HF_HUB_DISABLE_SYMLINKS is enabled. Refusing a download that could duplicate "
            "FL2VA/Ref2VA weights; unset it and rerun."
        )
    _probe_relative_symlink(cache_dir)
    _probe_relative_symlink(model_root.parent)


@contextmanager
def download_lock(manifest_path: Path) -> Iterator[None]:
    """Serialize metadata selection through final manifest replacement."""

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = manifest_path.parent / ".MiniMax-H3.download.lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True)
class ModelFile:
    path: str
    size: int
    blob_key: str


def patterns_for_task(task: str) -> tuple[str, ...]:
    if task == "FL2VA":
        return (*ROOT_FILES, "FL2VA/*")
    if task == "Ref2VA":
        return (*ROOT_FILES, "FL2VA/*", "Ref2VA/*")
    raise ValueError(f"Unsupported model task: {task}")


def _attribute(value: object, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _blob_key(sibling: object) -> str:
    lfs = _attribute(sibling, "lfs")
    for candidate in (
        _attribute(lfs, "sha256", ""),
        _attribute(lfs, "oid", ""),
        _attribute(sibling, "xet_hash", ""),
        _attribute(sibling, "blob_id", ""),
    ):
        if candidate:
            return str(candidate)
    return ""


def select_model_files(
    siblings: Iterable[object], patterns: Sequence[str]
) -> list[ModelFile]:
    selected: list[ModelFile] = []
    for sibling in siblings:
        path = str(_attribute(sibling, "rfilename", ""))
        if not path or not any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns):
            continue
        size_value = _attribute(sibling, "size")
        if size_value is None:
            size_value = _attribute(_attribute(sibling, "lfs"), "size")
        try:
            size = int(size_value)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Hugging Face did not report a size for {path}") from exc
        if size < 0:
            raise RuntimeError(f"Hugging Face reported an invalid size for {path}")
        selected.append(ModelFile(path, size, _blob_key(sibling)))
    selected.sort(key=lambda item: item.path)
    missing_roots = [name for name in ROOT_FILES if name not in {item.path for item in selected}]
    if missing_roots:
        raise RuntimeError(f"Model snapshot metadata is missing: {', '.join(missing_roots)}")
    return selected


def unique_bytes(files: Sequence[ModelFile]) -> int:
    blobs: dict[str, int] = {}
    for item in files:
        key = item.blob_key or f"path:{item.path}"
        previous = blobs.setdefault(key, item.size)
        if previous != item.size:
            raise RuntimeError(f"Conflicting sizes reported for model blob {key}")
    return sum(blobs.values())


def cached_bytes(files: Sequence[ModelFile], cache_dir: Path) -> int:
    repo_cache = cache_dir / "models--MiniMaxAI--MiniMax-H3" / "blobs"
    found: dict[str, int] = {}
    for item in files:
        if not item.blob_key or item.blob_key in found:
            continue
        blob = repo_cache / item.blob_key
        try:
            if blob.is_file() and blob.stat().st_size == item.size:
                found[item.blob_key] = item.size
        except OSError:
            continue
    return sum(found.values())


def verify_files(
    model_root: Path,
    files: Sequence[ModelFile],
    *,
    verify_blob_links: bool = False,
) -> list[str]:
    errors: list[str] = []
    for item in files:
        path = model_root / item.path
        try:
            if not path.is_file():
                errors.append(f"missing: {item.path}")
                continue
            actual_size = path.stat().st_size
        except OSError as exc:
            errors.append(f"unreadable: {item.path} ({exc})")
            continue
        if actual_size != item.size:
            errors.append(
                f"wrong size: {item.path} (expected {item.size}, got {actual_size})"
            )
        if verify_blob_links and not item.blob_key:
            errors.append(f"missing blob identity: {item.path}")
        elif verify_blob_links and not path.is_symlink():
            errors.append(f"not a content-addressed blob link: {item.path}")
        elif verify_blob_links:
            try:
                target_name = path.resolve(strict=True).name
            except OSError as exc:
                errors.append(f"broken blob link: {item.path} ({exc})")
                continue
            if target_name != item.blob_key:
                errors.append(
                    f"wrong blob link: {item.path} (expected {item.blob_key}, got {target_name})"
                )
    return errors


def _read_manifest(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"Model manifest is missing or invalid: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Model manifest must contain a JSON object: {path}")
    return value


def _manifest_files(value: dict[str, object]) -> list[ModelFile]:
    raw_files = value.get("files")
    if not isinstance(raw_files, list):
        raise RuntimeError("Model manifest has no file list")
    files: list[ModelFile] = []
    seen: set[str] = set()
    try:
        for raw in raw_files:
            if not isinstance(raw, dict):
                raise TypeError
            relative = str(raw["path"])
            parts = relative.split("/")
            if (
                relative.startswith("/")
                or "\\" in relative
                or any(part in {"", ".", ".."} for part in parts)
                or relative in seen
            ):
                raise ValueError
            seen.add(relative)
            size = int(raw["size"])
            if size < 0:
                raise ValueError
            files.append(ModelFile(relative, size, str(raw.get("blob_key", ""))))
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Model manifest contains an invalid file record") from exc
    return files


def _installed_tasks(value: dict[str, object], files: Sequence[ModelFile]) -> list[str]:
    raw_tasks = value.get("installed_tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise RuntimeError("Model manifest has no installed task list")
    tasks = [str(item) for item in raw_tasks]
    if len(set(tasks)) != len(tasks) or any(task not in {"FL2VA", "Ref2VA"} for task in tasks):
        raise RuntimeError("Model manifest contains an invalid installed task list")
    if "FL2VA" not in tasks:
        raise RuntimeError("Model manifest must include the FL2VA base task")
    prefixes = {item.path.split("/", 1)[0] for item in files if "/" in item.path}
    if "FL2VA" not in prefixes or ("Ref2VA" in tasks) != ("Ref2VA" in prefixes):
        raise RuntimeError("Model manifest task list does not match its file tree")
    allowed = set(ROOT_FILES) | {f"{task}/" for task in tasks}
    for item in files:
        if item.path in ROOT_FILES:
            continue
        if not any(item.path.startswith(prefix) for prefix in allowed if prefix.endswith("/")):
            raise RuntimeError(f"Model manifest has an out-of-task path: {item.path}")
    return tasks


def write_manifest(
    path: Path,
    *,
    revision: str,
    task: str,
    files: Sequence[ModelFile],
    storage: str,
) -> None:
    installed_tasks = ["FL2VA"] + (["Ref2VA"] if task == "Ref2VA" else [])
    payload = {
        "schema_version": MANIFEST_SCHEMA,
        "repo_id": REPO_ID,
        "revision": revision,
        "installed_tasks": installed_tasks,
        "storage": storage,
        "logical_bytes": sum(item.size for item in files),
        "unique_bytes": unique_bytes(files),
        "completed_at": time.time(),
        "files": [asdict(item) for item in files],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def verify_manifest(
    model_root: Path,
    manifest_path: Path,
    revision: str,
    required_task: str | None = None,
) -> dict[str, object]:
    value = _read_manifest(manifest_path)
    if value.get("schema_version") != MANIFEST_SCHEMA:
        raise RuntimeError("Unsupported model manifest schema")
    if value.get("repo_id") != REPO_ID:
        raise RuntimeError("Model manifest belongs to a different repository")
    if value.get("revision") != revision:
        raise RuntimeError(
            f"Model revision mismatch: expected {revision}, manifest has {value.get('revision')}"
        )
    files = _manifest_files(value)
    tasks = _installed_tasks(value, files)
    if required_task is not None and required_task not in tasks:
        raise RuntimeError(f"Model task {required_task} is not completely installed")
    errors = verify_files(
        model_root,
        files,
        verify_blob_links=(value.get("storage") == "huggingface-content-addressed-cache"),
    )
    if errors:
        preview = "\n  ".join(errors[:8])
        suffix = f"\n  ... and {len(errors) - 8} more" if len(errors) > 8 else ""
        raise RuntimeError(f"Model snapshot is incomplete:\n  {preview}{suffix}")
    return value


def _fetch_metadata(revision: str, task: str) -> tuple[str, list[ModelFile]]:
    try:
        from huggingface_hub import HfApi
    except ImportError as exc:
        raise RuntimeError("huggingface_hub is not installed; run Install.command") from exc
    info = HfApi().model_info(REPO_ID, revision=revision, files_metadata=True)
    resolved = str(info.sha)
    if resolved != revision:
        raise RuntimeError(f"Pinned model revision resolved unexpectedly to {resolved}")
    return resolved, select_model_files(info.siblings, patterns_for_task(task))


def _existing_manifest_task(manifest_path: Path, revision: str) -> str | None:
    try:
        value = _read_manifest(manifest_path)
    except RuntimeError:
        return None
    if value.get("revision") != revision:
        return None
    tasks = value.get("installed_tasks")
    if isinstance(tasks, list) and "Ref2VA" in tasks:
        return "Ref2VA"
    return "FL2VA" if isinstance(tasks, list) and "FL2VA" in tasks else None


def effective_task(requested_task: str, previous_task: str | None) -> str:
    """Never discard an already installed Ref2VA manifest on a FL2VA refresh."""

    return "Ref2VA" if "Ref2VA" in {requested_task, previous_task} else "FL2VA"


def _expose_snapshot(model_root: Path, snapshot: Path) -> None:
    if model_root.is_symlink():
        current_target = model_root.resolve(strict=False)
        managed_cache = (model_root.parent / ".cache").resolve(strict=False)
        if not current_target.is_relative_to(managed_cache):
            raise RuntimeError(
                f"Refusing to replace an unmanaged model symlink: {model_root} -> "
                f"{model_root.readlink()}"
            )
    elif model_root.exists():
        if not model_root.is_dir() or any(model_root.iterdir()):
            raise RuntimeError(
                f"Existing model path is not managed by the deduplicated downloader: {model_root}. "
                "If it is a complete legacy download, Doctor.command can still inspect it. "
                "Otherwise move it aside manually, rerun the downloader, and delete the backup "
                "only after Doctor.command succeeds."
            )
        model_root.rmdir()
    model_root.parent.mkdir(parents=True, exist_ok=True)
    temporary = model_root.with_name(f".{model_root.name}.tmp-{os.getpid()}")
    temporary.unlink(missing_ok=True)
    relative_snapshot = os.path.relpath(snapshot, start=model_root.parent)
    temporary.symlink_to(relative_snapshot, target_is_directory=True)
    temporary.replace(model_root)


def download(
    *,
    task: str,
    revision: str,
    model_root: Path,
    cache_dir: Path,
    manifest_path: Path,
) -> None:
    with download_lock(manifest_path):
        _download_locked(
            task=task,
            revision=revision,
            model_root=model_root,
            cache_dir=cache_dir,
            manifest_path=manifest_path,
        )


def _download_locked(
    *,
    task: str,
    revision: str,
    model_root: Path,
    cache_dir: Path,
    manifest_path: Path,
) -> None:
    require_deduplicated_storage(model_root, cache_dir)
    previous_task = _existing_manifest_task(manifest_path, revision)
    selected_task = effective_task(task, previous_task)
    resolved, files = _fetch_metadata(revision, selected_task)

    if model_root.exists() and not model_root.is_symlink() and any(model_root.iterdir()):
        existing_errors = verify_files(model_root, files)
        if not existing_errors:
            write_manifest(
                manifest_path,
                revision=resolved,
                task=selected_task,
                files=files,
                storage="legacy-local-directory",
            )
            print(
                "Existing legacy model directory is complete. It remains usable, but files "
                "already duplicated between FL2VA and Ref2VA were not rewritten automatically."
            )
            return
        raise RuntimeError(
            f"Existing legacy model directory is incomplete ({existing_errors[0]}). "
            "Move it aside manually before using the deduplicated downloader; no file was deleted."
        )

    cache_dir.mkdir(parents=True, exist_ok=True)
    logical = sum(item.size for item in files)
    unique = unique_bytes(files)
    cached = cached_bytes(files, cache_dir)
    missing = max(0, unique - cached)
    reserve = max(10 * GIB, missing // 20)
    free = shutil.disk_usage(cache_dir).free
    print(
        f"Pinned revision: {resolved}\n"
        f"Logical tree: {logical / GIB:.2f} GiB\n"
        f"Unique blobs: {unique / GIB:.2f} GiB\n"
        f"Already cached: {cached / GIB:.2f} GiB\n"
        f"Free space: {free / GIB:.2f} GiB"
    )
    if free < missing + reserve and os.environ.get("H3_SKIP_DISK_CHECK") != "1":
        raise RuntimeError(
            f"Insufficient free space: need about {(missing + reserve) / GIB:.1f} GiB "
            f"including download headroom, have {free / GIB:.1f} GiB. "
            "Free space or choose another disk. H3_SKIP_DISK_CHECK=1 bypasses only this "
            "preflight and is not recommended."
        )

    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise RuntimeError("huggingface_hub is not installed; run Install.command") from exc
    snapshot = Path(
        snapshot_download(
            repo_id=REPO_ID,
            revision=resolved,
            allow_patterns=list(patterns_for_task(selected_task)),
            cache_dir=str(cache_dir),
            max_workers=4,
        )
    ).resolve()
    snapshot_errors = verify_files(snapshot, files, verify_blob_links=True)
    if snapshot_errors:
        raise RuntimeError(f"Downloaded snapshot verification failed: {snapshot_errors[0]}")
    _expose_snapshot(model_root, snapshot)
    write_manifest(
        manifest_path,
        revision=resolved,
        task=selected_task,
        files=files,
        storage="huggingface-content-addressed-cache",
    )
    verify_manifest(model_root, manifest_path, resolved)
    print(f"Verified {len(files)} files at {model_root}")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subparsers = result.add_subparsers(dest="command", required=True)
    download_parser = subparsers.add_parser("download")
    download_parser.add_argument("--task", choices=("FL2VA", "Ref2VA"), required=True)
    verify_parser = subparsers.add_parser("verify")
    for selected in (download_parser, verify_parser):
        selected.add_argument("--revision", required=True)
        selected.add_argument("--model-root", type=Path, required=True)
        selected.add_argument("--manifest", type=Path, required=True)
    download_parser.add_argument("--cache-dir", type=Path, required=True)
    verify_parser.add_argument("--require-task", choices=("FL2VA", "Ref2VA"))
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "download":
            download(
                task=args.task,
                revision=args.revision,
                model_root=args.model_root,
                cache_dir=args.cache_dir,
                manifest_path=args.manifest,
            )
        else:
            value = verify_manifest(
                args.model_root,
                args.manifest,
                args.revision,
                required_task=args.require_task,
            )
            tasks = ", ".join(str(item) for item in value.get("installed_tasks", []))
            print(f"Verified {len(_manifest_files(value))} files ({tasks})")
    except RuntimeError as exc:
        print(f"model snapshot error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
