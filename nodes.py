from __future__ import annotations

import os
from pathlib import Path
from typing_extensions import override

import folder_paths
import comfy.model_management
import comfy.utils
from comfy_api.latest import ComfyExtension, Input, InputImpl, io, ui

from .h3_bridge import H3Reference, H3Request, H3Runner, load_config
from .h3_bridge.media import save_audio, save_image_tensor, validated_media_path


H3References = io.Custom("H3_REFERENCES")
CATEGORY = "H3 Mac"


class H3EmptyReferences(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="H3EmptyReferences",
            display_name="H3 · 新建参考素材列表",
            category=CATEGORY,
            description="建立有顺序的参考素材列表。素材顺序会影响 Ref2VA 的结果。",
            outputs=[H3References.Output("references", display_name="参考素材")],
        )

    @classmethod
    def execute(cls):
        return io.NodeOutput(tuple())


class H3AddImageReference(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="H3AddImageReference",
            display_name="H3 · 添加图片参考",
            category=CATEGORY,
            inputs=[
                H3References.Input("references", display_name="已有参考素材"),
                io.Image.Input("image", display_name="图片"),
            ],
            outputs=[H3References.Output("references", display_name="参考素材")],
        )

    @classmethod
    def execute(cls, references, image):
        asset_dir = Path(folder_paths.get_output_directory()) / "h3-assets"
        path = save_image_tensor(image, asset_dir)
        return io.NodeOutput(tuple(references) + (H3Reference("image", path),))


class H3AddAudioReference(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="H3AddAudioReference",
            display_name="H3 · 添加音频参考",
            category=CATEGORY,
            inputs=[
                H3References.Input("references", display_name="已有参考素材"),
                io.Audio.Input("audio", display_name="音频"),
            ],
            outputs=[H3References.Output("references", display_name="参考素材")],
        )

    @classmethod
    def execute(cls, references, audio: Input.Audio):
        asset_dir = Path(folder_paths.get_output_directory()) / "h3-assets"
        path = save_audio(audio, asset_dir)
        return io.NodeOutput(tuple(references) + (H3Reference("audio", path),))


class H3AddMediaFileReference(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="H3AddMediaFileReference",
            display_name="H3 · 添加本地媒体参考",
            category=CATEGORY,
            description="添加本地视频或音频路径。video_audio 模式需同时填写独立音轨路径。",
            inputs=[
                H3References.Input("references", display_name="已有参考素材"),
                io.Combo.Input(
                    "kind",
                    options=["silent_video", "video", "video_audio", "audio", "image"],
                    default="video",
                    display_name="素材类型",
                ),
                io.String.Input("path", display_name="媒体文件路径", default=""),
                io.String.Input(
                    "audio_path",
                    display_name="独立音轨路径（可选）",
                    default="",
                    optional=True,
                    advanced=True,
                ),
            ],
            outputs=[H3References.Output("references", display_name="参考素材")],
        )

    @classmethod
    def execute(cls, references, kind, path, audio_path=""):
        media_path = validated_media_path(path)
        extra_audio = validated_media_path(audio_path) if audio_path.strip() else None
        if kind == "video_audio" and extra_audio is None:
            raise ValueError("video_audio 模式需要填写独立音轨路径。")
        return io.NodeOutput(
            tuple(references) + (H3Reference(kind, media_path, extra_audio),)
        )


class H3GenerateVideo(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="H3GenerateVideo",
            display_name="H3 · 生成视频（Metal）",
            category=CATEGORY,
            description="通过 h3.c 在 Apple Silicon 上使用 Metal 生成 MP4。",
            inputs=[
                io.String.Input(
                    "prompt",
                    display_name="提示词",
                    multiline=True,
                    dynamic_prompts=True,
                    default="A cinematic, natural video with clear subject motion and stable identity.",
                ),
                H3References.Input("references", display_name="参考素材", optional=True),
                io.Image.Input("first_frame", display_name="首帧（可选）", optional=True),
                io.Image.Input("last_frame", display_name="尾帧（可选）", optional=True),
                io.Combo.Input("task", options=["Ref2VA", "FL2VA"], default="Ref2VA", display_name="模型任务"),
                io.Int.Input("width", default=640, min=256, max=1536, step=16, display_name="宽度"),
                io.Int.Input("height", default=384, min=256, max=1536, step=16, display_name="高度"),
                io.Float.Input("seconds", default=5.0, min=1.0, max=30.0, step=0.5, display_name="时长（秒）"),
                io.Combo.Input(
                    "quality_profile",
                    options=["preview", "balanced", "quality", "reference"],
                    default="quality",
                    display_name="画质档位",
                ),
                io.Combo.Input(
                    "resource_profile",
                    options=["low", "auto", "max"],
                    default="auto",
                    display_name="资源档位",
                ),
                io.Int.Input(
                    "seed",
                    default=42,
                    min=0,
                    max=0x7FFFFFFF,
                    control_after_generate=True,
                    display_name="随机种子",
                ),
                io.Boolean.Input(
                    "reuse_completed",
                    default=True,
                    label_on="复用",
                    label_off="重跑",
                    display_name="复用相同的已完成任务",
                    advanced=True,
                ),
            ],
            hidden=[io.Hidden.unique_id],
            outputs=[
                io.Video.Output("video", display_name="视频"),
                io.String.Output("job_dir", display_name="任务目录"),
                io.String.Output("summary", display_name="运行摘要"),
            ],
            is_output_node=True,
        )

    @classmethod
    def execute(
        cls,
        prompt,
        task,
        width,
        height,
        seconds,
        quality_profile,
        resource_profile,
        seed,
        reuse_completed,
        references=None,
        first_frame=None,
        last_frame=None,
    ):
        config = load_config()
        output_root = Path(folder_paths.get_output_directory()).resolve()
        asset_dir = output_root / "h3-assets"
        first_path = save_image_tensor(first_frame, asset_dir) if first_frame is not None else None
        last_path = save_image_tensor(last_frame, asset_dir) if last_frame is not None else None
        request = H3Request(
            prompt=prompt,
            task=task,
            width=width,
            height=height,
            seconds=seconds,
            fps=24,
            quality_profile=quality_profile,
            resource_profile=resource_profile,
            seed=seed,
            references=tuple(references or ()),
            first_frame=first_path,
            last_frame=last_path,
        )
        progress_bar = comfy.utils.ProgressBar(100, node_id=cls.hidden.unique_id)

        def on_progress(current: int, total: int, _line: str) -> None:
            progress_bar.update_absolute(current, max(1, total))

        result = H3Runner(config).run(
            request,
            output_root=output_root,
            progress=on_progress,
            cancelled=comfy.model_management.processing_interrupted,
            reuse_completed=reuse_completed,
        )
        video = InputImpl.VideoFromFile(str(result.output_path))
        relative = result.output_path.relative_to(output_root)
        summary = (
            f"任务 {result.job_id} 完成；耗时 {result.elapsed_seconds:.1f} 秒；"
            f"模式 {resource_profile}/{quality_profile}；输出 {relative}"
        )
        return io.NodeOutput(
            video,
            str(result.job_dir),
            summary,
            ui=ui.PreviewVideo(
                [ui.SavedResult(relative.name, str(relative.parent), io.FolderType.output)]
            ),
        )


class H3MacExtension(ComfyExtension):
    @override
    async def get_node_list(self) -> list[type[io.ComfyNode]]:
        return [
            H3EmptyReferences,
            H3AddImageReference,
            H3AddAudioReference,
            H3AddMediaFileReference,
            H3GenerateVideo,
        ]


async def comfy_entrypoint() -> H3MacExtension:
    return H3MacExtension()
