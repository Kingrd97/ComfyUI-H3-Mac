# ComfyUI-H3-Mac

**English** | [简体中文](README.zh-CN.md)

[![tests](https://github.com/Kingrd97/ComfyUI-H3-Mac/actions/workflows/tests.yml/badge.svg)](https://github.com/Kingrd97/ComfyUI-H3-Mac/actions/workflows/tests.yml)

A beginner-friendly bridge between the official [ComfyUI](https://github.com/Comfy-Org/ComfyUI) visual workflow system and [antirez/h3.c](https://github.com/antirez/h3.c) on Apple Silicon Macs. It provides double-click installation, native English/Chinese nodes, structured shot prompts, storyboard assembly, Metal inference, persistent jobs, and MP4 output.

> This is an early release. h3.c is evolving quickly; this project prioritizes reproducible installation, explicit reference ordering, cancellation, and inspectable jobs.

## What it provides

- ComfyUI for visual workflow composition, reusable assets, and parameter management.
- h3.c for native MiniMax H3 weights, Metal inference, and MP4 encoding.
- `low / auto / max` scheduling profiles that do not silently lower quality; auto pauses when the Mac is in use and resumes at full policy when it becomes idle.
- English and Simplified Chinese node names, fields, descriptions, and tooltips through ComfyUI's native locale system.
- A six-field shot prompt builder and lossless 2–6-shot MP4 storyboard assembly.
- A job directory containing the request, progress, engine log, partial output, and final video.
- Reuse of an identical completed request after a restart or accidental rerun.
- Explicit model-license acknowledgement; model files are never committed to Git.

## Requirements

- An Apple Silicon Mac. h3.c is currently optimized and tested mainly on M3 Max and M5 Max.
- macOS, Homebrew, and Xcode Command Line Tools.
- A fast SSD with substantial free space.
- The Ref2VA bundle is about 144 GB; at least 170 GB free is recommended.

A 48 GB M5 Pro should start with `auto`. It uses h3.c `--ssd-streaming` to control unified-memory pressure, pauses H3 for keyboard/mouse activity, substantial external CPU work, or battery power, and resumes without background policy after 60 seconds of AC-powered idle time. Pausing releases CPU/GPU execution while retaining the exact in-memory inference state.

## One-click installation

1. Download or clone this repository.
2. Double-click `Install.command`. If Gatekeeper blocks it, right-click → Open; do not disable macOS security protections.
3. Double-click `Download Model.command`; beginners should choose `1) Ref2VA`.
4. Double-click `Start.command` and wait for `http://127.0.0.1:8188` to open. Select `Comfy > Locale > Language` to switch between English and Chinese.

Command-line equivalent:

```bash
git clone https://github.com/Kingrd97/ComfyUI-H3-Mac.git
cd ComfyUI-H3-Mac
./Install.command
./Download\ Model.command
./Start.command
```

ComfyUI, h3.c, the Python virtual environment, and models live under `runtime/`, so the installation is self-contained. Validated upstream revisions are pinned in `versions.env` instead of tracking unpredictable future main branches.

`Start.command` intentionally runs the ComfyUI control plane with PyTorch on CPU. This does **not** disable Metal generation: the H3 node starts the separately compiled h3.c binary, which still performs inference with Metal. This default avoids unnecessary unified-memory use and PyTorch device-detection failures. If you also use other ComfyUI nodes that require MPS, start with `H3_COMFY_DEVICE=auto ./Start.command`.

The pinned official ComfyUI frontend has native localization. Browser language is used on first launch; `Comfy > Locale > Language` changes it later. H3 node translations follow that setting without a third-party translation patch.

For the easiest start, open `Workflow > Browse Templates`, choose `ComfyUI-H3-Mac`, and load `H3_Beginner_2_Shot_Storyboard`. The canvas is already grouped into references, Shot 1, Shot 2, and final MP4 assembly.

## First workflow

Add and connect these nodes in ComfyUI:

1. `Load Image` for the subject.
2. `H3 · New Reference List` (`H3 · 新建参考素材列表` in the current UI).
3. `H3 · Add Image Reference`, connected to both previous nodes.
4. Chain more reference nodes as needed. Their connection order becomes Picture 1, Picture 2, and so on.
5. Optional: add `H3 · Build Shot Prompt`, fill the storyboard fields, and connect its output to the generator's Prompt input.
6. `H3 · Generate Video (Metal)`. Start with `quality=preview` and `resource=low` for a smoke test.

After validating composition, use:

- `quality`: 20 steps, all 50 layers, no reuse; recommended for normal output.
- `reference`: 50-step slow reference for important shots or quality diagnosis.
- `resource=auto`: foreground-friendly scheduling and automatic streaming on lower-memory Macs.
- `resource=max`: normal priority and resident weights when the Mac is idle and has enough memory.

For a multi-shot story, use one prompt/generator pair per shot, then connect each generator's `Job directory` output to `H3 · Assemble Storyboard MP4`. See the [storyboard tutorial](docs/STORYBOARD.md).

## Nodes

| Node | Purpose |
|---|---|
| H3 Build Shot Prompt | Turn six beginner-friendly storyboard fields into one structured prompt |
| H3 Empty References | Start an immutable ordered reference chain |
| H3 Add Image Reference | Materialize a ComfyUI IMAGE as PNG and append it |
| H3 Add Audio Reference | Materialize ComfyUI AUDIO as WAV and append it |
| H3 Add Local Media Reference | Append local image, audio, video, or video-plus-audio paths |
| H3 Generate Video (Metal) | Run h3.c and return native ComfyUI VIDEO, job path, and summary |
| H3 Assemble Storyboard MP4 | Join 2–6 completed jobs in order without re-running H3 or re-encoding video |

## Resource and quality profiles

| Resource | Scheduling and memory | Changes quality settings? |
|---|---|---|
| low | All cores remain available but macOS schedules them as background work; SSD streaming; always progresses | No |
| auto | Streaming below 64 GiB; pauses while the Mac is active and resumes at full policy after 60 AC-powered idle seconds | No |
| max | Normal priority, no automatic pause, resident weights; may be tight on a 48 GB Mac | No |

| Quality | steps | layers | reuse | Intended use |
|---|---:|---:|---:|---|
| preview | 4 | 50 | 1 | Prompt and composition iteration |
| balanced | 20 | 45 | 2 | Faster drafts |
| quality | 20 | 50 | 1 | Normal final output |
| reference | 50 | 50 | 1 | Slow comparison oracle |

## Persistent jobs

Each request receives a deterministic job ID:

```text
output/h3-jobs/<job-id>/
├── request.json
├── progress.json
├── engine.log
├── result.partial.mp4
└── result.mp4
```

An identical completed request can be reused. h3.c does not currently export denoising-step state, so a run cannot resume exactly from step 12/20. Cancellation preserves logs and the partial file, although an unfinished MP4 may not be playable.

Double-click `H3 Control.command` to inspect, pause, resume, or change the scheduling policy of active jobs. The same controls are available from a shell:

```bash
./H3\ Control.command status
./H3\ Control.command pause
./H3\ Control.command resume
./H3\ Control.command auto
./H3\ Control.command max
```

Pause/resume uses macOS `SIGSTOP/SIGCONT`: loaded weights and the exact current computation remain in RAM, so resuming does not reload or repeat completed steps. This is not a serialized checkpoint and cannot survive process exit or reboot. See [resource control](docs/RESOURCE_CONTROL.md).

Assembled projects are stored in `output/h3-storyboards/<storyboard-id>/`. If a later shot fails, completed shot jobs remain reusable.

## Why ComfyUI? What is Manager?

ComfyUI is the visual node graph, execution server, API, queue, history, and workflow format. It is the strongest open foundation for reproducible local generative workflows, but its raw graph UI is not automatically the easiest possible interface for a first-time creator. This project adds a smaller H3-specific creation layer rather than replacing that reliable foundation.

[ComfyUI-Manager](https://github.com/Comfy-Org/ComfyUI-Manager) is a separate extension for installing, updating, enabling, disabling, and snapshotting custom nodes and models. It is not another frontend and is not required here. We do not install it by default because this self-contained distribution pins validated revisions; unrestricted extension updates would make beginner installations less reproducible.

## Quantization direction

Quantization can be added as an explicit engine profile, but it will not be presented as lossless acceleration. The pinned h3.c revision's experimental INT8 option cannot be combined with SSD streaming and is not a useful default for a 48 GB machine. Newer h3.c work is expanding native INT8 Metal execution; this project will expose it only after a same-prompt, seed, resolution, NFE, memory, and quality comparison on a 48 GB M5 Pro.

## Validation status

- Automated backend tests, shell syntax, and GitHub Actions: verified.
- V3 node registration against the pinned ComfyUI revision: verified.
- Clean one-click install, h3.c Metal build, ComfyUI HTTP startup, and H3 node discovery through `/object_info`: verified.
- End-to-end generation with the real 144 GB H3 weights: not re-verified in the current maintainer environment because the weights were intentionally not downloaded again. Reports from users with the model are welcome.

## Privacy, licenses, and limitations

- Media processing and generation stay local.
- Models, generated media, logs, runtime files, and user configuration are Git-ignored.
- This bridge is MIT-licensed; ComfyUI, its official frontend, h3.c, FFmpeg, and the model retain their own licenses. See [THIRD_PARTY.md](THIRD_PARTY.md) for explicit upstream attribution.
- MiniMax H3 weights require acceptance of the MiniMax H3 Community License.
- Apple Silicon macOS only.
- h3.c requires the original directory layout. Ref2VA also requires the FL2VA base files.
- Ordered Ref2VA references cannot be mixed with first/last-frame anchors.
- The current backend is h3.c only. A future stable-diffusion.cpp adapter should remain a distinct backend because its formats and capabilities differ.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install pytest numpy pillow typing_extensions
.venv/bin/pytest -q
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for boundaries and extension points. Chinese users can continue with [docs/QUICKSTART_zh.md](docs/QUICKSTART_zh.md) and the [Chinese storyboard tutorial](docs/STORYBOARD_zh-CN.md).
