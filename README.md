# ComfyUI-H3-Mac

**English** | [简体中文](README.zh-CN.md)

[![tests](https://github.com/Kingrd97/ComfyUI-H3-Mac/actions/workflows/tests.yml/badge.svg)](https://github.com/Kingrd97/ComfyUI-H3-Mac/actions/workflows/tests.yml)

A beginner-friendly ComfyUI bridge for driving [antirez/h3.c](https://github.com/antirez/h3.c) on Apple Silicon Macs. It provides double-click installation, ordered image/audio/video references, Metal inference, resource profiles, persistent jobs, and native MP4 output.

> This is an early release. h3.c is evolving quickly; this project prioritizes reproducible installation, explicit reference ordering, cancellation, and inspectable jobs.

## What it provides

- ComfyUI for visual workflow composition, reusable assets, and parameter management.
- h3.c for native MiniMax H3 weights, Metal inference, and MP4 encoding.
- `low / auto / max` resource profiles that do not silently lower quality.
- A job directory containing the request, progress, engine log, partial output, and final video.
- Reuse of an identical completed request after a restart or accidental rerun.
- Explicit model-license acknowledgement; model files are never committed to Git.

## Requirements

- An Apple Silicon Mac. h3.c is currently optimized and tested mainly on M3 Max and M5 Max.
- macOS, Homebrew, and Xcode Command Line Tools.
- A fast SSD with substantial free space.
- The Ref2VA bundle is about 144 GB; at least 170 GB free is recommended.

Start with `low` or `auto` on a memory-constrained Mac. `low` enables h3.c SSD streaming, while `auto` enables it when physical memory is below 64 GiB. This exchanges speed for memory without intentionally changing generation parameters.

## One-click installation

1. Download or clone this repository.
2. Double-click `Install.command`. If Gatekeeper blocks it, right-click → Open; do not disable macOS security protections.
3. Double-click `Download Model.command`; beginners should choose `1) Ref2VA`.
4. Double-click `Start.command` and wait for `http://127.0.0.1:8188` to open.

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

## First workflow

Add and connect these nodes in ComfyUI:

1. `Load Image` for the subject.
2. `H3 · New Reference List` (`H3 · 新建参考素材列表` in the current UI).
3. `H3 · Add Image Reference`, connected to both previous nodes.
4. Chain more reference nodes as needed. Their connection order becomes Picture 1, Picture 2, and so on.
5. `H3 · Generate Video (Metal)`. Start with `quality=preview` and `resource=low` for a smoke test.

After validating composition, use:

- `quality`: 20 steps, all 50 layers, no reuse; recommended for normal output.
- `reference`: 50-step slow reference for important shots or quality diagnosis.
- `resource=auto`: foreground-friendly scheduling and automatic streaming on lower-memory Macs.
- `resource=max`: normal priority and resident weights when the Mac is idle and has enough memory.

## Nodes

| Node | Purpose |
|---|---|
| H3 Empty References | Start an immutable ordered reference chain |
| H3 Add Image Reference | Materialize a ComfyUI IMAGE as PNG and append it |
| H3 Add Audio Reference | Materialize ComfyUI AUDIO as WAV and append it |
| H3 Add Local Media Reference | Append local image, audio, video, or video-plus-audio paths |
| H3 Generate Video (Metal) | Run h3.c and return native ComfyUI VIDEO, job path, and summary |

## Resource and quality profiles

| Resource | Scheduling and memory | Changes quality settings? |
|---|---|---|
| low | macOS background QoS, nice 15, SSD streaming | No |
| auto | background QoS, nice 10; streaming below 64 GiB | No |
| max | normal priority and resident weights | No |

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

## Validation status

- Automated backend tests, shell syntax, and GitHub Actions: verified.
- V3 node registration against the pinned ComfyUI revision: verified.
- Clean one-click install, h3.c Metal build, ComfyUI HTTP startup, and H3 node discovery through `/object_info`: verified.
- End-to-end generation with the real 144 GB H3 weights: not re-verified in the current maintainer environment because the weights were intentionally not downloaded again. Reports from users with the model are welcome.

## Privacy, licenses, and limitations

- Media processing and generation stay local.
- Models, generated media, logs, runtime files, and user configuration are Git-ignored.
- This bridge is MIT-licensed; ComfyUI, h3.c, FFmpeg, and the model retain their own licenses.
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

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for boundaries and extension points. Chinese users can continue with [docs/QUICKSTART_zh.md](docs/QUICKSTART_zh.md).
