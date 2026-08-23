# ComfyUI-H3-Mac

**English** | [简体中文](README.zh-CN.md)

[![tests](https://github.com/Kingrd97/ComfyUI-H3-Mac/actions/workflows/tests.yml/badge.svg)](https://github.com/Kingrd97/ComfyUI-H3-Mac/actions/workflows/tests.yml)

A beginner-friendly bridge between the official [ComfyUI](https://github.com/Comfy-Org/ComfyUI) visual workflow system and MiniMax H3 Metal backends on Apple Silicon Macs. It supports [antirez/h3.c](https://github.com/antirez/h3.c) plus an optional vpipe Q8 FL2VA backend, native English/Chinese nodes, structured shot prompts, storyboard assembly, one fixed post-production voice, persistent jobs, and MP4 output.

> This is an early release. h3.c is evolving quickly; this project prioritizes reproducible installation, explicit reference ordering, cancellation, and inspectable jobs.

## What it provides

- ComfyUI for visual workflow composition, reusable assets, and parameter management.
- h3.c for native MiniMax H3 weights, Metal inference, and MP4 encoding.
- `low / auto / max` resource profiles that keep generation settings explicit; auto normally progresses at background priority, temporarily pauses when the native responsiveness guardian or sustained fallback metrics detect pressure, and removes that policy after a sustained idle period on AC power.
- English and Simplified Chinese node names, fields, descriptions, and tooltips through ComfyUI's native locale system.
- A six-field shot prompt builder and lossless 2–8-shot MP4 storyboard assembly.
- A job directory containing the request, progress, engine log, partial output, and final video.
- Reuse of an identical completed request after a restart or accidental rerun.
- Explicit model-license acknowledgement; model files are never committed to Git.

## Requirements

- An Apple Silicon Mac. h3.c is currently optimized and tested mainly on M3 Max and M5 Max.
- macOS 15 or newer, Homebrew, and Xcode or Xcode Command Line Tools that provide macOS SDK 26 or newer. The pinned h3.c revision uses runtime Metal APIs introduced in macOS 15 and SDK symbols introduced in SDK 26; the installer checks both separately and builds with an explicit 15.0 deployment target instead of inheriting the current SDK version.
- A fast SSD with substantial free space.
- FL2VA is about 134 GiB. FL2VA plus Ref2VA is about 268 GiB as a logical tree, but the pinned content-addressed downloader stores identical blobs once: about 196 GiB physically. Start with at least 220 GiB free; the downloader performs an exact revision-aware preflight before writing.

A 48 GB M5 Pro should start with `auto`. With the current conservative memory rule it uses h3.c `--ssd-streaming` below 64 GiB and normally keeps H3 at Darwin background priority while the Mac is in use or on battery. A native helper watches recent input plus consecutively abnormal display-link callback gaps or callback age and triggers the fast pause path when both indicate display-service trouble. It needs neither Accessibility nor Screen Recording permission and does not capture the screen. Main-display framebuffer age is recorded for diagnosis only and never triggers Pause by itself. If the strong display-link signal is unavailable, sustained non-H3 CPU or combined WindowServer/GPU pressure provides a fallback. `auto` also pauses on critical memory/swap/pageout or thermal pressure, and blocks idle-max during Low Power Mode or marginal recovery. After 15 healthy seconds auto performs a 20-second background probe; if pressure does not return, background generation continues. After five AC-powered idle minutes, a fresh low external-CPU sample and settled WindowServer/display signals allow auto to remove the background policy. These controls remain best-effort; `taskpolicy` is not a hard GPU quota.

macOS does not expose a universal frame-drop counter for arbitrary foreground applications. The guardian observes display-system responsiveness rather than another app's renderer, so even its native signal and fallback metrics are best-effort rather than hard real-time. `SIGSTOP` cannot retract Metal work already submitted to the GPU and does not release loaded weights from unified memory.

The 64 GiB boundary is a safety heuristic, not an h3.c requirement. The pinned engine reports roughly 40.1 GB peak physical footprint for complex resident Ref2VA examples, so 48 GB plus foreground applications can be tight. SSD streaming greatly lowers DiT residency but is an explicit tradeoff: it performs large read-only, uncached model reads and can contend for disk bandwidth. It does not rewrite the model or consume SSD write-endurance as if those reads were writes. See [resource control](docs/RESOURCE_CONTROL.md) before forcing resident mode.

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

Model downloading uses a separate pinned Python environment, so its Hugging Face client cannot change ComfyUI's dependencies. The exposed `runtime/models/MiniMax-H3` path is a relative link into a content-addressed cache inside the same `runtime/` tree; moving the whole project preserves it. A completed manifest records every expected path, size, blob identity, and model revision. `Doctor.command` checks that manifest and then asks h3.c to inspect the model with `--info`.

Configuration schema v2 has a conservative one-time upgrade path. A legacy configuration is first backed up as `config.json.v1-backup`. Only a file that still exactly matches the former shipped `background` defaults is moved to the new `adaptive` behavior; customized behavior or thresholds are preserved.

`Start.command` intentionally runs the ComfyUI control plane with PyTorch on CPU. This does **not** disable Metal generation: the H3 node starts the separately compiled h3.c binary, which still performs inference with Metal. This default avoids unnecessary unified-memory use and PyTorch device-detection failures. If you also use other ComfyUI nodes that require MPS, start with `H3_COMFY_DEVICE=auto ./Start.command`.

The pinned official ComfyUI frontend has native localization. Browser language is used on first launch; `Comfy > Locale > Language` changes it later. H3 node translations follow that setting without a third-party translation patch.

For the easiest start, open `Workflow > Browse Templates`, choose `ComfyUI-H3-Mac`, and load `H3_Beginner_2_Shot_Storyboard`. The canvas is already grouped into references, Shot 1, Shot 2, and final MP4 assembly.

## First workflow

### Recommended vpipe Q8 workflow

Load `example_workflows/H3_vpipe_Q8_2_Shot_Fixed_Voice.json` when vpipe and the Q8 FL2VA model are already installed. Each `H3 · Generate with vpipe Q8` node renders a silent shot from a first-frame image; `H3 · Assemble storyboard MP4` joins the shots; `H3 · Add one fixed narration voice` then applies every `seconds|dialogue` cue with one voice. The recommended `zh-CN-YunxiNeural` voice is substantially more natural but requires internet; `macOS:Tingting` is the offline fallback. `Keep ambience` defaults off so the original H3 voice is completely discarded.

The vpipe node auto-detects `vpipe` on `PATH`. Override `vpipe_binary`, `vpipe_work_dir`, model, LoRA, and low-power resident-pool limits in `config.json` when needed. This optional backend does not change the pinned h3.c installation path.

Add and connect these nodes in ComfyUI:

1. `Load Image` for the subject.
2. `H3 · New Reference List` (`H3 · 新建参考素材列表` in the current UI).
3. `H3 · Add Image Reference`, connected to both previous nodes.
4. Chain more reference nodes as needed. Their connection order becomes Picture 1, Picture 2, and so on.
5. Optional: add `H3 · Build Shot Prompt`, fill the storyboard fields, and connect its output to the generator's Prompt input.
6. `H3 · Generate Video (Metal)`. Start with `quality=preview` and `resource=low` for a smoke test.

The normal single-shot limit is 5 seconds on lower-memory Macs. Build longer videos as multiple reusable shots and assemble them without re-encoding. h3.c mechanically supports up to 362 frames (about 15.08 seconds), but jobs above 5 seconds are allowed only on Macs with at least 64 GiB, or with the explicit expert override `H3_ALLOW_LARGE_JOB=1`; long VAE decode has shown extreme swap growth on memory-constrained systems.

After validating composition, use:

- `quality`: 20 steps, all 50 layers, no reuse; recommended for normal output.
- `reference`: 50-step slow reference for important shots or quality diagnosis.
- `resource=auto`: response-aware adaptive scheduling and conservative streaming on lower-memory Macs; it normally background-runs but can temporarily pause under detected contention.
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
| H3 Generate with vpipe Q8 (Metal) | Run the optional Q8 FL2VA backend; silent output is the recommended fixed-voice workflow |
| H3 Assemble Storyboard MP4 | Join 2–8 completed jobs in order without re-running H3 or re-encoding video |
| H3 Add One Fixed Narration Voice | Add timed dialogue to the assembled story with one consistent macOS voice |

## Resource and quality profiles

| Resource | Scheduling and memory | Changes steps/layers/reuse? |
|---|---|---|
| low | All cores remain available but macOS schedules them as background work; SSD streaming; always progresses | No |
| auto | Streaming below 64 GiB at process start; normally background while in use/on battery; temporary pause on native responsiveness or sustained fallback pressure; normal policy after five quiet AC-powered idle minutes | No |
| max | Normal priority, no automatic pause, resident weights; may be tight on a 48 GB Mac | No |

The memory path is fixed when a shot starts. Switching a running job from `auto` to `max` removes background scheduling but cannot turn its SSD-streamed weights into resident weights mid-denoise. On supported M5 hardware, the resident path also enables h3.c's default INT8 projections, while SSD streaming uses original BF16 blocks; this does not alter the selected steps/layers/reuse, but the two arithmetic paths can differ slightly in fine detail or framing.

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

Pause/resume uses macOS `SIGSTOP/SIGCONT`: loaded weights and process state remain in RAM, so resuming does not reload or repeat completed CPU-side progress. This does not free unified memory, cannot revoke a Metal command buffer already committed to the GPU, is not a serialized checkpoint, and cannot survive process exit or reboot. See [resource control](docs/RESOURCE_CONTROL.md).

Assembled projects are stored in `output/h3-storyboards/<storyboard-id>/`. If a later shot fails, completed shot jobs remain reusable.

## Why ComfyUI? What is Manager?

ComfyUI is the visual node graph, execution server, API, queue, history, and workflow format. It is the strongest open foundation for reproducible local generative workflows, but its raw graph UI is not automatically the easiest possible interface for a first-time creator. This project adds a smaller H3-specific creation layer rather than replacing that reliable foundation.

[ComfyUI-Manager](https://github.com/Comfy-Org/ComfyUI-Manager) is a separate extension for installing, updating, enabling, disabling, and snapshotting custom nodes and models. It is not another frontend and is not required here. We do not install it by default because this self-contained distribution pins validated revisions; unrestricted extension updates would make beginner installations less reproducible.

## Quantization direction

The pinned h3.c revision already selects its native resident INT8 MLP/QKV/attention projections on supported M5 hardware. SSD streaming is a separate original-BF16 path and disables those resident optimizations. A future UI revision will separate scheduling from the memory/engine path instead of presenting quantization as lossless acceleration; defaults will change only after same-prompt, seed, resolution, NFE, memory, and quality comparisons on a 48 GB M5 Pro.

## Validation status

- Automated backend tests, shell syntax, and GitHub Actions: verified.
- V3 node registration against the pinned ComfyUI revision: verified.
- Clean one-click install, h3.c Metal build, ComfyUI HTTP startup, and H3 node discovery through `/object_info`: verified.
- End-to-end generation with the real pinned H3 snapshot (about 196 GiB of unique blobs for Ref2VA): not re-verified in the current maintainer environment because the weights were intentionally not downloaded again. Reports from users with the model are welcome.

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
