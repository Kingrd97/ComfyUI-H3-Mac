from __future__ import annotations

import os
from pathlib import Path
from typing_extensions import override

import folder_paths
import comfy.model_management
import comfy.utils
from comfy_api.latest import ComfyExtension, Input, InputImpl, io, ui

from .h3_bridge import (
    H3Reference,
    H3Request,
    H3Runner,
    assemble_storyboard,
    build_shot_prompt,
    load_config,
)
from .h3_bridge.media import save_audio, save_image_tensor, validated_media_path


H3References = io.Custom("H3_REFERENCES")
CATEGORY = "H3 Mac"


class H3EmptyReferences(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="H3EmptyReferences",
            display_name="H3 · New reference list",
            category=CATEGORY,
            description="Start an ordered Ref2VA reference list. Order changes the result.",
            outputs=[H3References.Output("references", display_name="References")],
        )

    @classmethod
    def execute(cls):
        return io.NodeOutput(tuple())


class H3AddImageReference(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="H3AddImageReference",
            display_name="H3 · Add image reference",
            category=CATEGORY,
            inputs=[
                H3References.Input("references", display_name="Existing references"),
                io.Image.Input("image", display_name="Image"),
            ],
            outputs=[H3References.Output("references", display_name="References")],
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
            display_name="H3 · Add audio reference",
            category=CATEGORY,
            inputs=[
                H3References.Input("references", display_name="Existing references"),
                io.Audio.Input("audio", display_name="Audio"),
            ],
            outputs=[H3References.Output("references", display_name="References")],
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
            display_name="H3 · Add local media reference",
            category=CATEGORY,
            description="Append a local image, video, or audio file in a precise order.",
            inputs=[
                H3References.Input("references", display_name="Existing references"),
                io.Combo.Input(
                    "kind",
                    options=["silent_video", "video", "video_audio", "audio", "image"],
                    default="video",
                    display_name="Media type",
                ),
                io.String.Input("path", display_name="Media file path", default=""),
                io.String.Input(
                    "audio_path",
                    display_name="Separate audio path (optional)",
                    default="",
                    optional=True,
                    advanced=True,
                ),
            ],
            outputs=[H3References.Output("references", display_name="References")],
        )

    @classmethod
    def execute(cls, references, kind, path, audio_path=""):
        media_path = validated_media_path(path)
        extra_audio = validated_media_path(audio_path) if audio_path.strip() else None
        if kind == "video_audio" and extra_audio is None:
            raise ValueError("video_audio requires a separate audio path.")
        return io.NodeOutput(
            tuple(references) + (H3Reference(kind, media_path, extra_audio),)
        )


class H3BuildShotPrompt(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="H3BuildShotPrompt",
            display_name="H3 · Build shot prompt",
            category=CATEGORY,
            description="Turn a storyboard shot into a structured H3 prompt.",
            inputs=[
                io.String.Input(
                    "subject",
                    display_name="Subject and continuity",
                    multiline=True,
                    default="The same subject as the reference images, with stable identity and appearance.",
                ),
                io.String.Input(
                    "action_timeline",
                    display_name="Action timeline",
                    multiline=True,
                    default="0–2s: establish the action. 2–5s: clear continuous movement and interaction.",
                ),
                io.String.Input(
                    "environment",
                    display_name="Environment and physical interaction",
                    multiline=True,
                    default="Natural environment with physically plausible motion and contact.",
                ),
                io.String.Input(
                    "camera",
                    display_name="Camera and framing",
                    multiline=True,
                    default="Medium tracking shot, stable framing, one continuous take.",
                ),
                io.String.Input(
                    "look_and_sound",
                    display_name="Look, lighting, and sound",
                    multiline=True,
                    default="Photorealistic detail, natural light, coherent ambient sound.",
                ),
                io.String.Input(
                    "avoid",
                    display_name="Avoid",
                    multiline=True,
                    default="No identity drift, frozen pose, extra limbs, warped anatomy, text, watermark, or abrupt cut.",
                    optional=True,
                    advanced=True,
                ),
            ],
            outputs=[io.String.Output("prompt", display_name="Shot prompt")],
        )

    @classmethod
    def execute(
        cls,
        subject,
        action_timeline,
        environment,
        camera,
        look_and_sound,
        avoid="",
    ):
        return io.NodeOutput(
            build_shot_prompt(
                subject,
                action_timeline,
                environment,
                camera,
                look_and_sound,
                avoid,
            )
        )


class H3GenerateVideo(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="H3GenerateVideo",
            display_name="H3 · Generate video (Metal)",
            category=CATEGORY,
            description="Generate an MP4 through h3.c Metal inference on Apple Silicon.",
            inputs=[
                io.String.Input(
                    "prompt",
                    display_name="Prompt",
                    multiline=True,
                    dynamic_prompts=True,
                    default="A cinematic, natural video with clear subject motion and stable identity.",
                ),
                H3References.Input("references", display_name="References", optional=True),
                io.Image.Input("first_frame", display_name="First frame (optional)", optional=True),
                io.Image.Input("last_frame", display_name="Last frame (optional)", optional=True),
                io.Combo.Input("task", options=["Ref2VA", "FL2VA"], default="Ref2VA", display_name="Model task"),
                io.Int.Input("width", default=640, min=256, max=1536, step=16, display_name="Width"),
                io.Int.Input("height", default=384, min=256, max=1536, step=16, display_name="Height"),
                io.Float.Input("seconds", default=5.0, min=1.0, max=30.0, step=0.5, display_name="Duration (seconds)"),
                io.Combo.Input(
                    "quality_profile",
                    options=["preview", "balanced", "quality", "reference"],
                    default="quality",
                    display_name="Quality profile",
                ),
                io.Combo.Input(
                    "resource_profile",
                    options=["low", "auto", "max"],
                    default="auto",
                    display_name="Resource profile",
                ),
                io.Int.Input(
                    "seed",
                    default=42,
                    min=0,
                    max=0x7FFFFFFF,
                    control_after_generate=True,
                    display_name="Seed",
                ),
                io.Boolean.Input(
                    "reuse_completed",
                    default=True,
                    label_on="Reuse",
                    label_off="Rerun",
                    display_name="Reuse identical completed job",
                    advanced=True,
                ),
            ],
            hidden=[io.Hidden.unique_id],
            outputs=[
                io.Video.Output("video", display_name="Video"),
                io.String.Output("job_dir", display_name="Job directory"),
                io.String.Output("summary", display_name="Run summary"),
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
            f"job={result.job_id} | elapsed={result.elapsed_seconds:.1f}s | "
            f"profile={resource_profile}/{quality_profile} | output={relative}"
        )
        return io.NodeOutput(
            video,
            str(result.job_dir),
            summary,
            ui=ui.PreviewVideo(
                [ui.SavedResult(relative.name, str(relative.parent), io.FolderType.output)]
            ),
        )


class H3AssembleStoryboard(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="H3AssembleStoryboard",
            display_name="H3 · Assemble storyboard MP4",
            category=CATEGORY,
            description="Join 2–6 completed H3 shots in order without another generation pass.",
            inputs=[
                io.String.Input("title", display_name="Project title", default="My H3 storyboard"),
                io.String.Input("shot_1_job", display_name="Shot 1 job directory"),
                io.String.Input("shot_2_job", display_name="Shot 2 job directory"),
                io.String.Input(
                    "shot_3_job",
                    display_name="Shot 3 job directory (optional)",
                    default="",
                    optional=True,
                ),
                io.String.Input(
                    "shot_4_job",
                    display_name="Shot 4 job directory (optional)",
                    default="",
                    optional=True,
                ),
                io.String.Input(
                    "shot_5_job",
                    display_name="Shot 5 job directory (optional)",
                    default="",
                    optional=True,
                    advanced=True,
                ),
                io.String.Input(
                    "shot_6_job",
                    display_name="Shot 6 job directory (optional)",
                    default="",
                    optional=True,
                    advanced=True,
                ),
            ],
            outputs=[
                io.Video.Output("video", display_name="Final video"),
                io.String.Output("project_dir", display_name="Storyboard directory"),
                io.String.Output("summary", display_name="Assembly summary"),
            ],
            is_output_node=True,
        )

    @classmethod
    def execute(
        cls,
        title,
        shot_1_job,
        shot_2_job,
        shot_3_job="",
        shot_4_job="",
        shot_5_job="",
        shot_6_job="",
    ):
        config = load_config()
        output_root = Path(folder_paths.get_output_directory()).resolve()
        result = assemble_storyboard(
            [
                shot_1_job,
                shot_2_job,
                shot_3_job,
                shot_4_job,
                shot_5_job,
                shot_6_job,
            ],
            output_root=output_root,
            jobs_subdir=config.output_subdir,
            title=title,
        )
        video = InputImpl.VideoFromFile(str(result.output_path))
        relative = result.output_path.relative_to(output_root)
        summary = (
            f"storyboard={result.storyboard_id} | reused={str(result.reused).lower()} | "
            f"output={relative}"
        )
        return io.NodeOutput(
            video,
            str(result.project_dir),
            summary,
            ui=ui.PreviewVideo(
                [ui.SavedResult(relative.name, str(relative.parent), io.FolderType.output)]
            ),
        )


class H3MacExtension(ComfyExtension):
    @override
    async def get_node_list(self) -> list[type[io.ComfyNode]]:
        return [
            H3BuildShotPrompt,
            H3EmptyReferences,
            H3AddImageReference,
            H3AddAudioReference,
            H3AddMediaFileReference,
            H3GenerateVideo,
            H3AssembleStoryboard,
        ]


async def comfy_entrypoint() -> H3MacExtension:
    return H3MacExtension()
