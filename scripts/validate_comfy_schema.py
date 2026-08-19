#!/usr/bin/env python3
"""Validate this extension against a real checked-out ComfyUI V3 API."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import sys
import types
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("comfyui", type=Path, help="Path to a ComfyUI checkout")
    args = parser.parse_args()
    comfy_root = args.comfyui.resolve()
    project_root = Path(__file__).resolve().parents[1]
    if not (comfy_root / "comfy_api" / "latest").is_dir():
        parser.error(f"Not a ComfyUI source checkout: {comfy_root}")

    sys.path.insert(0, str(comfy_root))
    # Load the real public API first, then stub only runtime services that
    # initialize devices or progress UI. Schema construction remains real.
    from comfy_api.latest import io  # noqa: PLC0415
    import comfy  # noqa: PLC0415

    model_management = types.ModuleType("comfy.model_management")
    model_management.processing_interrupted = lambda: False
    utils = types.ModuleType("comfy.utils")
    utils.ProgressBar = object
    sys.modules["comfy.model_management"] = model_management
    sys.modules["comfy.utils"] = utils
    setattr(comfy, "model_management", model_management)
    setattr(comfy, "utils", utils)

    spec = importlib.util.spec_from_file_location(
        "comfyui_h3_mac",
        project_root / "__init__.py",
        submodule_search_locations=[str(project_root)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not create the extension import spec.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    extension = asyncio.run(module.comfy_entrypoint())
    node_classes = asyncio.run(extension.get_node_list())

    node_ids = []
    for node_class in node_classes:
        schema: io.Schema = node_class.define_schema()
        schema.finalize()
        schema.validate()
        node_ids.append(schema.node_id)
    print(f"Validated {len(node_ids)} nodes: {', '.join(node_ids)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
