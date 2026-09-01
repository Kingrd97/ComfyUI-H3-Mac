#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import lmdb

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from h3_bridge.vpipe import load_vpipe_config, validate_vpipe_installation


VIDEO_VAE_SIZE = 5_207_808_496
AUDIO_VAE_SIZE = 605_254_808
LARRY_LORA_SIZE = 779_849_816
LIGHTX_LORA_SIZE = 1_956_192_992
MIN_DIT_BYTES = 34_000_000_000
MIN_ENCODER_BYTES = 28_000_000_000


def _json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"无法读取 {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON 顶层不是对象：{path}")
    return value


def _versions(project_root: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (project_root / "versions.env").read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def _require_file(path: Path, *, exact_size: int | None = None) -> None:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"缺少普通文件：{path}")
    size = path.stat().st_size
    if size <= 0:
        raise ValueError(f"文件为空：{path}")
    if exact_size is not None and size != exact_size:
        raise ValueError(f"文件大小不匹配：{path}（{size} != {exact_size}）")


def _allocated_bytes(path: Path) -> int:
    stat = path.stat()
    blocks = getattr(stat, "st_blocks", 0)
    return int(blocks) * 512 if blocks else stat.st_size


def _require_dense_file(path: Path) -> None:
    size = path.stat().st_size
    allocated = _allocated_bytes(path)
    if allocated < size * 0.9:
        raise ValueError(
            f"权重文件异常稀疏：{path}"
            f"（逻辑 {size} bytes，实际占用 {allocated} bytes）"
        )


def _safetensors_keys(path: Path) -> set[str]:
    """Validate a safetensors container and return its tensor names.

    This intentionally validates the local structure without reading multi-GiB
    tensor payloads into memory.  The pinned Hugging Face revisions plus the
    manifest provide source identity; this catches truncated, sparse, or
    structurally corrupt downloads before vpipe tries to mmap them.  `--deep`
    additionally verifies every payload byte against the preparation manifest.
    """

    _require_file(path)
    _require_dense_file(path)
    size = path.stat().st_size
    try:
        with path.open("rb") as handle:
            prefix = handle.read(8)
            if len(prefix) != 8:
                raise ValueError(f"safetensors 文件头不完整：{path}")
            header_size = int.from_bytes(prefix, "little", signed=False)
            if header_size < 2 or header_size > min(256 * 1024 * 1024, size - 8):
                raise ValueError(f"safetensors 头长度非法：{path}")
            header = json.loads(handle.read(header_size).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法解析 safetensors 头：{path}: {exc}") from exc
    if not isinstance(header, dict):
        raise ValueError(f"safetensors 头不是对象：{path}")

    payload_size = size - 8 - header_size
    keys: set[str] = set()
    for name, descriptor in header.items():
        if name == "__metadata__":
            continue
        if not isinstance(name, str) or not isinstance(descriptor, dict):
            raise ValueError(f"safetensors 张量描述非法：{path}")
        dtype = descriptor.get("dtype")
        shape = descriptor.get("shape")
        offsets = descriptor.get("data_offsets")
        if (
            not isinstance(dtype, str)
            or not isinstance(shape, list)
            or any(not isinstance(item, int) or item < 0 for item in shape)
            or not isinstance(offsets, list)
            or len(offsets) != 2
            or any(not isinstance(item, int) for item in offsets)
            or offsets[0] < 0
            or offsets[0] > offsets[1]
            or offsets[1] > payload_size
        ):
            raise ValueError(f"safetensors 张量范围非法：{path}#{name}")
        keys.add(name)
    if not keys:
        raise ValueError(f"safetensors 不含张量：{path}")
    return keys


def _verify_quantized_component(component: Path, minimum_bytes: int) -> int:
    config = _json(component / "config.json")
    quantization = config.get("quantization")
    if (
        not isinstance(quantization, dict)
        or quantization.get("bits") != 8
        or quantization.get("group_size") != 64
    ):
        raise ValueError(f"不是已验证的 Q8 组件：{component}")
    index = _json(component / "model.safetensors.index.json")
    weight_map = index.get("weight_map")
    if not isinstance(weight_map, dict) or not weight_map:
        raise ValueError(f"权重索引为空：{component}")
    names = {str(value) for value in weight_map.values() if value}
    if not names or any(Path(name).name != name for name in names):
        raise ValueError(f"权重索引含非法分片名：{component}")
    total = 0
    for name in sorted(names):
        shard = component / name
        shard_keys = _safetensors_keys(shard)
        indexed_keys = {
            str(tensor)
            for tensor, shard_name in weight_map.items()
            if str(shard_name) == name
        }
        missing = indexed_keys - shard_keys
        extra = shard_keys - indexed_keys
        if missing or extra:
            sample = ", ".join(sorted(missing or extra)[:3])
            raise ValueError(f"权重索引与分片不匹配：{shard}（{sample}）")
        total += shard.stat().st_size
    if total < minimum_bytes:
        raise ValueError(f"Q8 分片总量异常：{component}（{total} bytes）")
    return total


def _model_asset_files(config) -> list[Path]:
    model_root = config.work_dir / "models" / config.model
    paths: list[Path] = []
    for component_name in ("diffusion_models", "text_encoders"):
        component = model_root / component_name
        config_path = component / "config.json"
        index_path = component / "model.safetensors.index.json"
        weight_map = _json(index_path).get(
            "weight_map"
        )
        if not isinstance(weight_map, dict):
            raise ValueError(f"权重索引非法：{component}")
        paths.extend([config_path, index_path])
        paths.extend(
            component / name
            for name in sorted({str(value) for value in weight_map.values()})
        )
    paths.extend(
        [
            model_root / "vae/minimax_h3_video_vae_fp16.safetensors",
            model_root / "vae/minimax_h3_audio_vae_fp32.safetensors",
            model_root / "tokenizer/tokenizer.json",
            model_root / "tokenizer/tokenizer_config.json",
        ]
    )
    return paths


def _asset_files(config) -> list[Path]:
    return _model_asset_files(config) + [
        config.work_dir
        / "models/larryvrh/MiniMax-H3-Turbo-Lora"
        / "minimax_h3_turbo_v4_step600_ema.safetensors",
        config.work_dir
        / "models/lightx2v/Minimax-h3-Turbo"
        / "minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors",
    ]


def _schema3_added_files(config) -> set[str]:
    """Files first covered by the schema-3 SHA trust root.

    Schema 2 already required hashes for every large payload (Q8 shards,
    VAEs and LoRAs).  Only these small metadata/tokenizer files may be absent
    while upgrading an existing manifest; a missing payload hash must never be
    silently re-created from whatever bytes happen to be on disk.
    """

    model_root = config.work_dir / "models" / config.model
    paths = {
        model_root / "diffusion_models/config.json",
        model_root / "diffusion_models/model.safetensors.index.json",
        model_root / "text_encoders/config.json",
        model_root / "text_encoders/model.safetensors.index.json",
        model_root / "tokenizer/tokenizer.json",
        model_root / "tokenizer/tokenizer_config.json",
    }
    return {str(path.relative_to(config.work_dir)) for path in paths}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def verify_assets(
    project_root: Path,
    *,
    model_only: bool = False,
    deep: bool = False,
    allow_missing_hashes: bool = False,
    allow_vpipe_ref_mismatch: bool = False,
    allow_schema_migration: bool = False,
    verify_model_hashes: bool = False,
) -> dict[str, int]:
    project_root = project_root.resolve()
    config = load_vpipe_config(project_root)
    if not config.model.startswith("local/"):
        raise ValueError("自动校验只支持项目准备的 local/ Q8 模型")
    model_root = config.work_dir / "models" / config.model
    dit_bytes = _verify_quantized_component(
        model_root / "diffusion_models", MIN_DIT_BYTES
    )
    encoder_bytes = _verify_quantized_component(
        model_root / "text_encoders", MIN_ENCODER_BYTES
    )
    video_vae = model_root / "vae" / "minimax_h3_video_vae_fp16.safetensors"
    audio_vae = model_root / "vae" / "minimax_h3_audio_vae_fp32.safetensors"
    _require_file(video_vae, exact_size=VIDEO_VAE_SIZE)
    _require_file(audio_vae, exact_size=AUDIO_VAE_SIZE)
    _safetensors_keys(video_vae)
    _safetensors_keys(audio_vae)
    _json(model_root / "tokenizer" / "tokenizer.json")
    _json(model_root / "tokenizer" / "tokenizer_config.json")

    result = {"dit_bytes": dit_bytes, "encoder_bytes": encoder_bytes}
    if model_only and not verify_model_hashes:
        return result

    if not model_only:
        larry = (
            config.work_dir
            / "models/larryvrh/MiniMax-H3-Turbo-Lora"
            / "minimax_h3_turbo_v4_step600_ema.safetensors"
        )
        lightx = (
            config.work_dir
            / "models/lightx2v/Minimax-h3-Turbo"
            / "minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors"
        )
        _require_file(larry, exact_size=LARRY_LORA_SIZE)
        _require_file(lightx, exact_size=LIGHTX_LORA_SIZE)
        _safetensors_keys(larry)
        _safetensors_keys(lightx)

    manifest = _json(config.work_dir / "models/.comfyui-h3-q8-manifest.json")
    versions = _versions(project_root)
    expected = {
        "vpipe_ref": versions["VPIPE_REF"],
        "h3_repack_ref": versions["VPIPE_H3_REPACK_REF"],
        "minimax_ref": versions["H3_MODEL_REF"],
    }
    if not model_only:
        expected.update(
            {
                "larry_lora_ref": versions["VPIPE_LARRY_LORA_REF"],
                "lightx_lora_ref": versions["VPIPE_LIGHTX_LORA_REF"],
            }
        )
    for key, value in expected.items():
        if key == "vpipe_ref" and allow_vpipe_ref_mismatch:
            continue
        if manifest.get(key) != value:
            raise ValueError(f"Q8 清单版本不匹配：{key}")
    recorded_hashes = manifest.get("sha256")
    files = _model_asset_files(config) if model_only else _asset_files(config)
    expected_names = {
        str(path.relative_to(config.work_dir)): path for path in files
    }
    if not isinstance(recorded_hashes, dict):
        if allow_missing_hashes:
            return result
        raise ValueError("Q8 清单缺少权重 SHA-256；请重跑 Prepare vpipe Q8.command")
    schema_version = manifest.get("schema_version")
    migration_missing = (
        _schema3_added_files(config)
        if allow_schema_migration
        and isinstance(schema_version, int)
        and schema_version < 3
        else set()
    )
    for name, path in expected_names.items():
        recorded = recorded_hashes.get(name)
        if recorded is None and name in migration_missing:
            continue
        if (
            not isinstance(recorded, str)
            or len(recorded) != 64
            or any(character not in "0123456789abcdef" for character in recorded)
        ):
            raise ValueError(f"Q8 清单缺少权重哈希：{name}")
        if deep and _sha256(path) != recorded:
            raise ValueError(f"权重 SHA-256 不匹配：{name}")
    return result


def write_hash_manifest(project_root: Path) -> None:
    project_root = project_root.resolve()
    config = load_vpipe_config(project_root)
    manifest_path = config.work_dir / "models/.comfyui-h3-q8-manifest.json"
    manifest = _json(manifest_path)
    if isinstance(manifest.get("sha256"), dict):
        # An existing hash set is the trust root.  Never bless current bytes
        # again merely because Prepare was rerun after corruption.  New schema
        # revisions may add files: verify every old entry first, then append
        # only the newly covered paths.
        verify_assets(
            project_root, deep=True, allow_schema_migration=True
        )
        existing = dict(manifest["sha256"])
        missing = {
            str(path.relative_to(config.work_dir))
            for path in _asset_files(config)
            if str(path.relative_to(config.work_dir)) not in existing
        }
        unexpected_missing = missing - _schema3_added_files(config)
        if unexpected_missing:
            sample = ", ".join(sorted(unexpected_missing)[:3])
            raise ValueError(f"拒绝为旧资产补写缺失哈希：{sample}")
        for path in _asset_files(config):
            name = str(path.relative_to(config.work_dir))
            if name not in existing:
                existing[name] = _sha256(path)
        manifest["schema_version"] = 3
        manifest["sha256"] = existing
        temporary = manifest_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(manifest_path)
        return
    verify_assets(project_root, allow_missing_hashes=True)
    manifest["schema_version"] = 3
    manifest["sha256"] = {
        str(path.relative_to(config.work_dir)): _sha256(path)
        for path in _asset_files(config)
    }
    temporary = manifest_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(manifest_path)


def initialize_manifest(project_root: Path) -> None:
    """Create the pinned base manifest without overwriting a trust root."""

    project_root = project_root.resolve()
    config = load_vpipe_config(project_root)
    manifest_path = config.work_dir / "models/.comfyui-h3-q8-manifest.json"
    versions = _versions(project_root)
    expected = {
        "vpipe_ref": versions["VPIPE_REF"],
        "h3_repack_ref": versions["VPIPE_H3_REPACK_REF"],
        "minimax_ref": versions["H3_MODEL_REF"],
        "larry_lora_ref": versions["VPIPE_LARRY_LORA_REF"],
        "lightx_lora_ref": versions["VPIPE_LIGHTX_LORA_REF"],
    }
    if manifest_path.exists():
        try:
            manifest = _json(manifest_path)
        except ValueError as exc:
            raise ValueError(
                "Q8 校验清单已存在但无法解析；为避免重新认证潜在损坏权重，"
                f"已保留原文件并停止：{manifest_path}"
            ) from exc
        else:
            asset_keys = (
                "h3_repack_ref",
                "minimax_ref",
                "larry_lora_ref",
                "lightx_lora_ref",
            )
            for key in asset_keys:
                if manifest.get(key) != expected[key]:
                    raise ValueError(
                        f"Q8 资产版本冲突：{key}；"
                        "为保护现有权重和 SHA-256，"
                        "本次不会删除权重或覆盖清单"
                    )
            if manifest.get("vpipe_ref") == expected["vpipe_ref"]:
                return
            # Engine-only upgrades may reuse identical model/LoRA bytes.  A
            # stored trust root must pass before its engine ref is migrated.
            has_hashes = isinstance(manifest.get("sha256"), dict)
            verify_assets(
                project_root,
                deep=has_hashes,
                allow_missing_hashes=not has_hashes,
                allow_vpipe_ref_mismatch=True,
                allow_schema_migration=has_hashes,
            )
            manifest["vpipe_ref"] = expected["vpipe_ref"]
            manifest["engine_ref_migrated_at"] = time.time()
            temporary = manifest_path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(manifest_path)
            return
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "schema_version": 1,
        "created_at": time.time(),
        **expected,
    }
    temporary = manifest_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(manifest_path)


def verify_registry(project_root: Path) -> None:
    """Read vpipe's LMDB model registry without loading model tensors."""

    config = load_vpipe_config(project_root.resolve())
    validate_vpipe_installation(config)
    expected: dict[str, tuple[Path, bytes]] = {
        config.model: (
            config.work_dir / "models" / config.model,
            b"minimax-h3-fl2va",
        ),
        config.lora: (
            config.work_dir / "models/larryvrh/MiniMax-H3-Turbo-Lora",
            b"minimax-h3-lora",
        ),
        config.lora_768p: (
            config.work_dir / "models/lightx2v/Minimax-h3-Turbo",
            b"minimax-h3-lora",
        ),
    }
    try:
        environment = lmdb.open(
            str(config.work_dir),
            subdir=True,
            readonly=True,
            create=False,
            max_dbs=64,
            readahead=False,
            lock=False,
        )
        registry = environment.open_db(b"__vpipe_model_registry", create=False)
        with environment.begin(db=registry) as transaction:
            for key, (expected_path, expected_type) in expected.items():
                record = transaction.get(key.encode("utf-8"))
                relative_path = expected_path.relative_to(config.work_dir)
                path_markers = (
                    str(expected_path.resolve()).encode("utf-8"),
                    f"./{relative_path}".encode("utf-8"),
                    str(relative_path).encode("utf-8"),
                )
                if record is None or expected_type not in record or not any(
                    marker in record for marker in path_markers
                ):
                    raise ValueError(f"vpipe 注册表记录不匹配：{key}")
    except lmdb.Error as exc:
        raise ValueError(f"无法读取 vpipe 注册表：{exc}") from exc
    finally:
        if "environment" in locals():
            environment.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify prepared vpipe H3 Q8 assets")
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--model-only", action="store_true")
    parser.add_argument(
        "--deep",
        action="store_true",
        help="read and SHA-256 every model/LoRA payload (slow, for Doctor)",
    )
    parser.add_argument(
        "--write-hashes",
        action="store_true",
        help="record SHA-256 for the completed local Q8 assets",
    )
    parser.add_argument(
        "--initialize-manifest",
        action="store_true",
        help="create a pinned base manifest without replacing existing hashes",
    )
    parser.add_argument(
        "--allow-missing-hashes",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--allow-schema-migration",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--allow-engine-migration",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--verify-model-hashes",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--files-only",
        action="store_true",
        help="skip the lightweight vpipe registry resolution smoke test",
    )
    parser.add_argument("--component-root", type=Path)
    parser.add_argument("--component-kind", choices=("dit", "encoder"))
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    try:
        if args.initialize_manifest:
            if args.write_hashes or args.model_only or args.component_root is not None:
                parser.error("--initialize-manifest must be used by itself")
            initialize_manifest(args.project_root)
            if not args.quiet:
                print("vpipe Q8 基础清单已保留/初始化")
            return 0
        if args.write_hashes:
            if args.model_only or args.component_root is not None:
                parser.error("--write-hashes requires the complete Q8 asset set")
            write_hash_manifest(args.project_root)
        if args.component_root is not None:
            if args.component_kind is None:
                parser.error("--component-root requires --component-kind")
            minimum = MIN_DIT_BYTES if args.component_kind == "dit" else MIN_ENCODER_BYTES
            total = _verify_quantized_component(args.component_root, minimum)
            if not args.quiet:
                print(f"Q8 {args.component_kind} 组件完整：{total / 1024**3:.1f} GiB")
            return 0
        if args.verify_model_hashes and not args.model_only:
            parser.error("--verify-model-hashes requires --model-only")
        sizes = verify_assets(
            args.project_root,
            model_only=args.model_only,
            deep=args.deep,
            allow_missing_hashes=args.allow_missing_hashes,
            allow_vpipe_ref_mismatch=args.allow_engine_migration,
            allow_schema_migration=args.allow_schema_migration,
            verify_model_hashes=args.verify_model_hashes,
        )
        if not args.model_only and not args.files_only:
            verify_registry(args.project_root)
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        if not args.quiet:
            print(f"vpipe Q8 校验失败：{exc}")
        return 1
    if not args.quiet:
        total_gib = (sizes["dit_bytes"] + sizes["encoder_bytes"]) / 1024**3
        suffix = "（仅基础模型）" if args.model_only else "（含两套 Turbo LoRA）"
        print(f"vpipe Q8 资产完整：核心量化权重约 {total_gib:.1f} GiB {suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
