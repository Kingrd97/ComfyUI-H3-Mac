from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts import model_snapshot


REVISION = "a" * 40


def sibling(path: str, size: int, sha256: str):
    return SimpleNamespace(
        rfilename=path,
        size=size,
        lfs={"sha256": sha256, "size": size},
        blob_id="",
    )


def model_files() -> list[model_snapshot.ModelFile]:
    return [
        model_snapshot.ModelFile("model_index.json", 5, "index-blob"),
        model_snapshot.ModelFile("modular_model_index.json", 7, "modular-blob"),
        model_snapshot.ModelFile("FL2VA/shared.bin", 11, "shared-blob"),
        model_snapshot.ModelFile("Ref2VA/shared.bin", 11, "shared-blob"),
        model_snapshot.ModelFile("Ref2VA/unique.bin", 13, "ref-blob"),
    ]


def test_selection_and_unique_size_deduplicate_identical_hub_blobs():
    selected = model_snapshot.select_model_files(
        [
            sibling("model_index.json", 5, "index"),
            sibling("modular_model_index.json", 7, "modular"),
            sibling("FL2VA/nested/shared.bin", 11, "shared"),
            sibling("Ref2VA/nested/shared.bin", 11, "shared"),
            sibling("README.md", 999, "readme"),
        ],
        model_snapshot.patterns_for_task("Ref2VA"),
    )
    assert [item.path for item in selected] == [
        "FL2VA/nested/shared.bin",
        "Ref2VA/nested/shared.bin",
        "model_index.json",
        "modular_model_index.json",
    ]
    assert sum(item.size for item in selected) == 34
    assert model_snapshot.unique_bytes(selected) == 23


def test_manifest_upgrade_keeps_ref2va_when_fl2va_is_refreshed(tmp_path: Path):
    manifest = tmp_path / "manifest.json"
    fl_files = [item for item in model_files() if not item.path.startswith("Ref2VA/")]
    model_snapshot.write_manifest(
        manifest,
        revision=REVISION,
        task="FL2VA",
        files=fl_files,
        storage="test",
    )
    assert model_snapshot.effective_task(
        "Ref2VA", model_snapshot._existing_manifest_task(manifest, REVISION)
    ) == "Ref2VA"


def test_download_upgrades_fl2va_snapshot_to_ref2va_without_duplicate_blobs(
    tmp_path: Path,
):
    cache = tmp_path / "runtime" / "models" / ".cache" / "huggingface"
    root = tmp_path / "runtime" / "models" / "MiniMax-H3"
    manifest = tmp_path / "runtime" / "models" / "MiniMax-H3.manifest.json"
    all_files = model_files()
    fl_files = [item for item in all_files if not item.path.startswith("Ref2VA/")]

    def fetch(_revision: str, task: str):
        return REVISION, all_files if task == "Ref2VA" else fl_files

    def snapshot_download(**kwargs):
        selected = all_files if "Ref2VA/*" in kwargs["allow_patterns"] else fl_files
        selected_cache = Path(kwargs["cache_dir"])
        snapshot = (
            selected_cache
            / "models--MiniMaxAI--MiniMax-H3"
            / "snapshots"
            / REVISION
        )
        blobs = selected_cache / "models--MiniMaxAI--MiniMax-H3" / "blobs"
        blobs.mkdir(parents=True, exist_ok=True)
        for item in selected:
            blob = blobs / item.blob_key
            if not blob.exists():
                blob.write_bytes(b"x" * item.size)
            exposed = snapshot / item.path
            exposed.parent.mkdir(parents=True, exist_ok=True)
            if not exposed.exists():
                exposed.symlink_to(os.path.relpath(blob, start=exposed.parent))
        return str(snapshot)

    fake_hub = SimpleNamespace(snapshot_download=snapshot_download)
    with patch.object(model_snapshot, "_fetch_metadata", side_effect=fetch), patch.dict(
        sys.modules, {"huggingface_hub": fake_hub}
    ):
        model_snapshot.download(
            task="FL2VA",
            revision=REVISION,
            model_root=root,
            cache_dir=cache,
            manifest_path=manifest,
        )
        assert model_snapshot.verify_manifest(
            root, manifest, REVISION, required_task="FL2VA"
        )["installed_tasks"] == ["FL2VA"]

        model_snapshot.download(
            task="Ref2VA",
            revision=REVISION,
            model_root=root,
            cache_dir=cache,
            manifest_path=manifest,
        )

    result = model_snapshot.verify_manifest(
        root, manifest, REVISION, required_task="Ref2VA"
    )
    assert result["installed_tasks"] == ["FL2VA", "Ref2VA"]
    assert result["logical_bytes"] == 47
    assert result["unique_bytes"] == 36
    assert len(list((cache / "models--MiniMaxAI--MiniMax-H3" / "blobs").iterdir())) == 4

    model_snapshot.write_manifest(
        manifest,
        revision=REVISION,
        task="Ref2VA",
        files=model_files(),
        storage="test",
    )
    assert model_snapshot.effective_task(
        "FL2VA", model_snapshot._existing_manifest_task(manifest, REVISION)
    ) == "Ref2VA"


def test_manifest_rejects_wrong_revision_and_truncated_file(tmp_path: Path):
    root = tmp_path / "model"
    root.mkdir()
    files = model_files()[:3]
    for item in files:
        (root / item.path).parent.mkdir(parents=True, exist_ok=True)
        (root / item.path).write_bytes(b"x" * item.size)
    manifest = tmp_path / "manifest.json"
    model_snapshot.write_manifest(
        manifest,
        revision=REVISION,
        task="FL2VA",
        files=files,
        storage="legacy-local-directory",
    )
    assert model_snapshot.verify_manifest(root, manifest, REVISION)["revision"] == REVISION
    with pytest.raises(RuntimeError, match="revision mismatch"):
        model_snapshot.verify_manifest(root, manifest, "b" * 40)
    (root / files[0].path).write_bytes(b"bad")
    with pytest.raises(RuntimeError, match="wrong size"):
        model_snapshot.verify_manifest(root, manifest, REVISION)


def test_content_addressed_manifest_rejects_wrong_blob_link(tmp_path: Path):
    root = tmp_path / "model"
    blobs = tmp_path / "cache" / "blobs"
    root.mkdir()
    blobs.mkdir(parents=True)
    files = model_files()[:3]
    for item in files:
        wrong = blobs / f"wrong-{item.blob_key}"
        wrong.write_bytes(b"x" * item.size)
        (root / item.path).parent.mkdir(parents=True, exist_ok=True)
        (root / item.path).symlink_to(wrong)
    manifest = tmp_path / "manifest.json"
    model_snapshot.write_manifest(
        manifest,
        revision=REVISION,
        task="FL2VA",
        files=files,
        storage="huggingface-content-addressed-cache",
    )
    with pytest.raises(RuntimeError, match="wrong blob link"):
        model_snapshot.verify_manifest(root, manifest, REVISION)


def test_content_addressed_manifest_rejects_regular_files(tmp_path: Path):
    root = tmp_path / "model"
    root.mkdir()
    files = model_files()[:3]
    for item in files:
        path = root / item.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * item.size)
    manifest = tmp_path / "manifest.json"
    model_snapshot.write_manifest(
        manifest,
        revision=REVISION,
        task="FL2VA",
        files=files,
        storage="huggingface-content-addressed-cache",
    )
    with pytest.raises(RuntimeError, match="not a content-addressed blob link"):
        model_snapshot.verify_manifest(root, manifest, REVISION)


def test_manifest_rejects_missing_required_task_and_unsafe_paths(tmp_path: Path):
    root = tmp_path / "model"
    root.mkdir()
    files = model_files()[:3]
    for item in files:
        (root / item.path).parent.mkdir(parents=True, exist_ok=True)
        (root / item.path).write_bytes(b"x" * item.size)
    manifest = tmp_path / "manifest.json"
    model_snapshot.write_manifest(
        manifest,
        revision=REVISION,
        task="FL2VA",
        files=files,
        storage="legacy-local-directory",
    )
    with pytest.raises(RuntimeError, match="Ref2VA is not completely installed"):
        model_snapshot.verify_manifest(root, manifest, REVISION, required_task="Ref2VA")

    value = model_snapshot._read_manifest(manifest)
    value["files"][0]["path"] = "../outside.bin"  # type: ignore[index]
    manifest.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(RuntimeError, match="invalid file record"):
        model_snapshot.verify_manifest(root, manifest, REVISION)


def test_snapshot_exposure_uses_relative_movable_symlink(tmp_path: Path):
    snapshot = tmp_path / "runtime" / "models" / ".cache" / "snapshots" / REVISION
    snapshot.mkdir(parents=True)
    model_root = tmp_path / "runtime" / "models" / "MiniMax-H3"
    model_snapshot._expose_snapshot(model_root, snapshot)
    assert model_root.is_symlink()
    assert not Path(model_root.readlink()).is_absolute()
    assert model_root.resolve() == snapshot.resolve()


def test_incomplete_legacy_directory_fails_without_deleting_files(tmp_path: Path):
    root = tmp_path / "MiniMax-H3"
    root.mkdir()
    sentinel = root / "keep-me.bin"
    sentinel.write_bytes(b"user-data")
    with patch.object(
        model_snapshot,
        "_fetch_metadata",
        return_value=(REVISION, model_files()),
    ):
        with pytest.raises(RuntimeError, match="incomplete"):
            model_snapshot.download(
                task="Ref2VA",
                revision=REVISION,
                model_root=root,
                cache_dir=tmp_path / "cache",
                manifest_path=tmp_path / "manifest.json",
            )
    assert sentinel.read_bytes() == b"user-data"
    assert not (tmp_path / "manifest.json").exists()


def test_download_refuses_disabled_or_unsupported_symlinks(tmp_path: Path):
    model_root = tmp_path / "runtime" / "models" / "MiniMax-H3"
    cache = tmp_path / "runtime" / "models" / ".cache" / "huggingface"
    with patch.dict(os.environ, {"HF_HUB_DISABLE_SYMLINKS": "1"}):
        with pytest.raises(RuntimeError, match="HF_HUB_DISABLE_SYMLINKS"):
            model_snapshot.require_deduplicated_storage(model_root, cache)

    with patch.dict(os.environ, {}, clear=False), patch.object(
        Path, "symlink_to", side_effect=OSError("unsupported")
    ):
        os.environ.pop("HF_HUB_DISABLE_SYMLINKS", None)
        with pytest.raises(RuntimeError, match="does not support"):
            model_snapshot.require_deduplicated_storage(model_root, cache)


def test_download_lock_serializes_metadata_through_commit(tmp_path: Path):
    manifest = tmp_path / "runtime" / "models" / "MiniMax-H3.manifest.json"
    mutex = threading.Lock()
    active = 0
    maximum_active = 0

    def worker() -> None:
        nonlocal active, maximum_active
        with model_snapshot.download_lock(manifest):
            with mutex:
                active += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.05)
            with mutex:
                active -= 1

    first = threading.Thread(target=worker)
    second = threading.Thread(target=worker)
    first.start()
    second.start()
    first.join(timeout=2)
    second.join(timeout=2)
    assert not first.is_alive() and not second.is_alive()
    assert maximum_active == 1
