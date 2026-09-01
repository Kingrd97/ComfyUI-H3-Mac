from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.verify_vpipe_assets import (
    AUDIO_VAE_SIZE,
    LARRY_LORA_SIZE,
    LIGHTX_LORA_SIZE,
    MIN_DIT_BYTES,
    MIN_ENCODER_BYTES,
    VIDEO_VAE_SIZE,
    initialize_manifest,
    verify_assets,
)


ROOT = Path(__file__).resolve().parents[1]


def test_initialize_manifest_preserves_existing_hash_trust_root(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    versions = (ROOT / "versions.env").read_text()
    (project / "versions.env").write_text(versions)
    work = project / "runtime/vpipe-work"
    manifest = work / "models/.comfyui-h3-q8-manifest.json"
    manifest.parent.mkdir(parents=True)
    pinned = dict(
        line.split("=", 1)
        for line in versions.splitlines()
        if line and not line.startswith("#") and "=" in line
    )
    value = {
        "schema_version": 2,
        "vpipe_ref": pinned["VPIPE_REF"],
        "h3_repack_ref": pinned["VPIPE_H3_REPACK_REF"],
        "minimax_ref": pinned["H3_MODEL_REF"],
        "larry_lora_ref": pinned["VPIPE_LARRY_LORA_REF"],
        "lightx_lora_ref": pinned["VPIPE_LIGHTX_LORA_REF"],
        "sha256": {"models/example.safetensors": "a" * 64},
    }
    manifest.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    before = manifest.read_bytes()
    (project / "config.json").write_text(
        json.dumps(
            {
                "vpipe_binary": "/usr/bin/true",
                "vpipe_work_dir": "runtime/vpipe-work",
            }
        )
    )

    initialize_manifest(project)

    assert manifest.read_bytes() == before


def test_initialize_manifest_refuses_version_conflict_without_overwrite(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "versions.env").write_text((ROOT / "versions.env").read_text())
    work = project / "runtime/vpipe-work"
    manifest = work / "models/.comfyui-h3-q8-manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"vpipe_ref": "different"}), encoding="utf-8")
    before = manifest.read_bytes()
    (project / "config.json").write_text(
        json.dumps(
            {
                "vpipe_binary": "/usr/bin/true",
                "vpipe_work_dir": "runtime/vpipe-work",
            }
        )
    )

    with pytest.raises(ValueError, match="资产版本冲突"):
        initialize_manifest(project)

    assert manifest.read_bytes() == before


def test_initialize_manifest_refuses_corrupt_manifest_without_retrusting(
    tmp_path: Path,
):
    project = tmp_path / "project"
    project.mkdir()
    (project / "versions.env").write_text((ROOT / "versions.env").read_text())
    work = project / "runtime/vpipe-work"
    manifest = work / "models/.comfyui-h3-q8-manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_bytes(b'{"sha256":')
    before = manifest.read_bytes()
    (project / "config.json").write_text(
        json.dumps(
            {
                "vpipe_binary": "/usr/bin/true",
                "vpipe_work_dir": "runtime/vpipe-work",
            }
        )
    )

    with pytest.raises(ValueError, match="无法解析"):
        initialize_manifest(project)

    assert manifest.read_bytes() == before


def test_initialize_manifest_migrates_engine_only_ref(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    versions_text = (ROOT / "versions.env").read_text()
    (project / "versions.env").write_text(versions_text)
    pinned = dict(
        line.split("=", 1)
        for line in versions_text.splitlines()
        if line and not line.startswith("#") and "=" in line
    )
    work = project / "runtime/vpipe-work"
    manifest = work / "models/.comfyui-h3-q8-manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "vpipe_ref": "older-engine-only-ref",
                "h3_repack_ref": pinned["VPIPE_H3_REPACK_REF"],
                "minimax_ref": pinned["H3_MODEL_REF"],
                "larry_lora_ref": pinned["VPIPE_LARRY_LORA_REF"],
                "lightx_lora_ref": pinned["VPIPE_LIGHTX_LORA_REF"],
            }
        ),
        encoding="utf-8",
    )
    (project / "config.json").write_text(
        json.dumps(
            {
                "vpipe_binary": "/usr/bin/true",
                "vpipe_work_dir": "runtime/vpipe-work",
            }
        )
    )
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "scripts.verify_vpipe_assets.verify_assets",
        lambda _root, **kwargs: calls.append(kwargs) or {},
    )

    initialize_manifest(project)

    migrated = json.loads(manifest.read_text())
    assert migrated["vpipe_ref"] == pinned["VPIPE_REF"]
    assert calls == [
        {
            "deep": False,
            "allow_missing_hashes": True,
            "allow_vpipe_ref_mismatch": True,
            "allow_schema_migration": False,
        }
    ]


def sparse(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as output:
        if path.suffix == ".safetensors":
            header = json.dumps(
                {
                    "tensor": {
                        "dtype": "F32",
                        "shape": [1],
                        "data_offsets": [0, 4],
                    }
                },
                separators=(",", ":"),
            ).encode("utf-8")
            header += b" " * (-len(header) % 8)
            output.write(len(header).to_bytes(8, "little"))
            output.write(header)
        output.truncate(size)


def component(path: Path, size: int) -> None:
    path.mkdir(parents=True)
    (path / "config.json").write_text(
        json.dumps({"quantization": {"bits": 8, "group_size": 64}}),
        encoding="utf-8",
    )
    (path / "model.safetensors.index.json").write_text(
        json.dumps({"weight_map": {"tensor": "model-00001-of-00001.safetensors"}}),
        encoding="utf-8",
    )
    sparse(path / "model-00001-of-00001.safetensors", size)


def test_verify_vpipe_assets_rejects_missing_index_shard(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "versions.env").write_text((ROOT / "versions.env").read_text())
    work = project / "runtime/vpipe-work"
    (project / "config.json").write_text(
        json.dumps(
            {
                "vpipe_binary": "/usr/bin/true",
                "vpipe_work_dir": "runtime/vpipe-work",
                "vpipe_model": "local/MiniMax-H3-FL2VA-8bit",
            }
        ),
        encoding="utf-8",
    )
    model = work / "models/local/MiniMax-H3-FL2VA-8bit"
    component(model / "diffusion_models", MIN_DIT_BYTES)
    component(model / "text_encoders", MIN_ENCODER_BYTES)
    (model / "diffusion_models/model-00001-of-00001.safetensors").unlink()

    try:
        verify_assets(project, model_only=True)
    except ValueError as exc:
        assert "缺少普通文件" in str(exc)
    else:
        raise AssertionError("missing shard must fail verification")


def test_verify_vpipe_assets_rejects_wrong_q8_group_size(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "versions.env").write_text((ROOT / "versions.env").read_text())
    work = project / "runtime/vpipe-work"
    (project / "config.json").write_text(
        json.dumps(
            {
                "vpipe_binary": "/usr/bin/true",
                "vpipe_work_dir": "runtime/vpipe-work",
                "vpipe_model": "local/MiniMax-H3-FL2VA-8bit",
            }
        ),
        encoding="utf-8",
    )
    model = work / "models/local/MiniMax-H3-FL2VA-8bit"
    component(model / "diffusion_models", MIN_DIT_BYTES)
    component(model / "text_encoders", MIN_ENCODER_BYTES)
    config_path = model / "text_encoders/config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["quantization"]["group_size"] = 32
    config_path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(
        "scripts.verify_vpipe_assets._allocated_bytes",
        lambda path: path.stat().st_size,
    )

    with pytest.raises(ValueError, match="Q8"):
        verify_assets(project, model_only=True)


def test_verify_vpipe_assets_rejects_same_size_corrupt_shard(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "versions.env").write_text((ROOT / "versions.env").read_text())
    work = project / "runtime/vpipe-work"
    (project / "config.json").write_text(
        json.dumps(
            {
                "vpipe_binary": "/usr/bin/true",
                "vpipe_work_dir": "runtime/vpipe-work",
                "vpipe_model": "local/MiniMax-H3-FL2VA-8bit",
            }
        ),
        encoding="utf-8",
    )
    model = work / "models/local/MiniMax-H3-FL2VA-8bit"
    component(model / "diffusion_models", MIN_DIT_BYTES)
    component(model / "text_encoders", MIN_ENCODER_BYTES)
    shard = model / "diffusion_models/model-00001-of-00001.safetensors"
    with shard.open("r+b") as output:
        output.write((0).to_bytes(8, "little"))

    with pytest.raises(ValueError, match="safetensors"):
        verify_assets(project, model_only=True)


def test_verify_vpipe_assets_rejects_valid_header_with_sparse_payload(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "versions.env").write_text((ROOT / "versions.env").read_text())
    work = project / "runtime/vpipe-work"
    (project / "config.json").write_text(
        json.dumps(
            {
                "vpipe_binary": "/usr/bin/true",
                "vpipe_work_dir": "runtime/vpipe-work",
                "vpipe_model": "local/MiniMax-H3-FL2VA-8bit",
            }
        ),
        encoding="utf-8",
    )
    model = work / "models/local/MiniMax-H3-FL2VA-8bit"
    component(model / "diffusion_models", MIN_DIT_BYTES)
    component(model / "text_encoders", MIN_ENCODER_BYTES)

    with pytest.raises(ValueError, match="稀疏"):
        verify_assets(project, model_only=True)


def test_verify_vpipe_assets_accepts_complete_structural_fixture(
    tmp_path: Path, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    versions_text = (ROOT / "versions.env").read_text()
    (project / "versions.env").write_text(versions_text)
    versions = dict(
        line.split("=", 1)
        for line in versions_text.splitlines()
        if line and not line.startswith("#") and "=" in line
    )
    work = project / "runtime/vpipe-work"
    (project / "config.json").write_text(
        json.dumps(
            {
                "vpipe_binary": "/usr/bin/true",
                "vpipe_work_dir": "runtime/vpipe-work",
                "vpipe_model": "local/MiniMax-H3-FL2VA-8bit",
            }
        ),
        encoding="utf-8",
    )
    model = work / "models/local/MiniMax-H3-FL2VA-8bit"
    component(model / "diffusion_models", MIN_DIT_BYTES)
    component(model / "text_encoders", MIN_ENCODER_BYTES)
    sparse(model / "vae/minimax_h3_video_vae_fp16.safetensors", VIDEO_VAE_SIZE)
    sparse(model / "vae/minimax_h3_audio_vae_fp32.safetensors", AUDIO_VAE_SIZE)
    (model / "tokenizer").mkdir(parents=True, exist_ok=True)
    (model / "tokenizer/tokenizer.json").write_text("{}", encoding="utf-8")
    (model / "tokenizer/tokenizer_config.json").write_text("{}", encoding="utf-8")
    sparse(
        work
        / "models/larryvrh/MiniMax-H3-Turbo-Lora"
        / "minimax_h3_turbo_v4_step600_ema.safetensors",
        LARRY_LORA_SIZE,
    )
    sparse(
        work
        / "models/lightx2v/Minimax-h3-Turbo"
        / "minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors",
        LIGHTX_LORA_SIZE,
    )
    monkeypatch.setattr(
        "scripts.verify_vpipe_assets._allocated_bytes",
        lambda path: path.stat().st_size,
    )
    tensor_paths = [
        model / "diffusion_models/config.json",
        model / "diffusion_models/model.safetensors.index.json",
        model / "diffusion_models/model-00001-of-00001.safetensors",
        model / "text_encoders/config.json",
        model / "text_encoders/model.safetensors.index.json",
        model / "text_encoders/model-00001-of-00001.safetensors",
        model / "vae/minimax_h3_video_vae_fp16.safetensors",
        model / "vae/minimax_h3_audio_vae_fp32.safetensors",
        model / "tokenizer/tokenizer.json",
        model / "tokenizer/tokenizer_config.json",
        work
        / "models/larryvrh/MiniMax-H3-Turbo-Lora"
        / "minimax_h3_turbo_v4_step600_ema.safetensors",
        work
        / "models/lightx2v/Minimax-h3-Turbo"
        / "minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors",
    ]
    manifest_path = work / "models/.comfyui-h3-q8-manifest.json"
    manifest_payload = {
        "schema_version": 3,
        "vpipe_ref": versions["VPIPE_REF"],
        "h3_repack_ref": versions["VPIPE_H3_REPACK_REF"],
        "minimax_ref": versions["H3_MODEL_REF"],
        "larry_lora_ref": versions["VPIPE_LARRY_LORA_REF"],
        "lightx_lora_ref": versions["VPIPE_LIGHTX_LORA_REF"],
        "sha256": {
            str(path.relative_to(work)): "0" * 64 for path in tensor_paths
        },
    }
    manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")

    sizes = verify_assets(project)

    assert sizes == {"dit_bytes": MIN_DIT_BYTES, "encoder_bytes": MIN_ENCODER_BYTES}

    # Schema-2 migration may add only the six newly covered small metadata
    # files. It must never bless a missing large-payload trust-root entry.
    manifest_payload["schema_version"] = 2
    config_name = str(
        (model / "diffusion_models/config.json").relative_to(work)
    )
    manifest_payload["sha256"].pop(config_name)
    manifest_payload["vpipe_ref"] = "older-engine-only-ref"
    manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
    with pytest.raises(ValueError, match="vpipe_ref"):
        verify_assets(
            project,
            model_only=True,
            verify_model_hashes=True,
            allow_schema_migration=True,
        )
    verify_assets(
        project,
        model_only=True,
        verify_model_hashes=True,
        allow_schema_migration=True,
        allow_vpipe_ref_mismatch=True,
    )

    shard_name = str(
        (model / "diffusion_models/model-00001-of-00001.safetensors").relative_to(
            work
        )
    )
    manifest_payload["sha256"].pop(shard_name)
    manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
    with pytest.raises(ValueError, match="缺少权重哈希"):
        verify_assets(
            project,
            model_only=True,
            verify_model_hashes=True,
            allow_schema_migration=True,
            allow_vpipe_ref_mismatch=True,
        )
