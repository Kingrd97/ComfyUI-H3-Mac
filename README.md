# ComfyUI-H3-Mac

**English** | [简体中文](README.zh-CN.md)

[![tests](https://github.com/Kingrd97/ComfyUI-H3-Mac/actions/workflows/tests.yml/badge.svg)](https://github.com/Kingrd97/ComfyUI-H3-Mac/actions/workflows/tests.yml)

A beginner-friendly bridge between the official [ComfyUI](https://github.com/Comfy-Org/ComfyUI) visual workflow system and MiniMax H3 Metal backends on Apple Silicon Macs. On macOS 26 it installs a pinned, signed [vpipe](https://github.com/tgo-app-dev/vpipe) runtime; users may prepare its Q8 FL2VA weights or skip Q8 completely and use [antirez/h3.c](https://github.com/antirez/h3.c) with the official original MiniMax H3 BF16 weights. The project provides native English/Chinese nodes, structured shot prompts, storyboard assembly, fixed post-production narration, persistent jobs, and MP4 output.

> This is an early release. h3.c is evolving quickly; this project prioritizes reproducible installation, explicit reference ordering, cancellation, and inspectable jobs.

## What it provides

- ComfyUI for visual workflow composition, reusable assets, and parameter management.
- A pinned official vpipe build and a resumable, verified Q8 preparation path for the recommended 24/48 GB workflow.
- h3.c for native MiniMax H3 weights, Metal inference, and MP4 encoding.
- `low / auto / max` resource profiles that keep generation settings explicit; auto normally progresses at background priority, temporarily pauses when the native responsiveness guardian or sustained fallback metrics detect pressure, and removes that policy after a sustained idle period on AC power.
- English and Simplified Chinese node names, fields, descriptions, and tooltips through ComfyUI's native locale system.
- A six-field shot prompt builder and lossless 2–8-shot MP4 storyboard assembly.
- A job directory containing the request, progress, engine log, partial output, and final video.
- Per-user launchd services keep both ComfyUI and a durable vpipe queue worker alive. A vpipe shot is owned by the worker, so closing or restarting ComfyUI does not kill the Metal process.
- Reuse of an identical completed request after a restart or accidental rerun.
- Explicit model-license acknowledgement; model files are never committed to Git.

## Requirements

- An Apple Silicon Mac. The recommended vpipe route requires **macOS 26 or newer**; the installer refuses an incompatible binary instead of attempting to run it. h3.c itself supports macOS 15+, but the pinned source build still needs Xcode or Command Line Tools with macOS SDK 26.
- Homebrew and a fast SSD with substantial free space.
- Recommended vpipe Q8: about 65 GiB for the final Q8 model plus about 2.5 GiB for both Turbo LoRAs. Its compact two-stage conversion needs about 120 GiB free the first time, deletes each temporary BF16 stage after verification, and can resume downloads after Ctrl-C.
- Advanced h3.c BF16: FL2VA is about 134 GiB; FL2VA + Ref2VA uses about 196 GiB physically through the pinned content-addressed cache. Start with 150 or 220 GiB free respectively.

A 48 GB M5 Pro should use vpipe Q8 with `auto`. It avoids the original-weight h3.c streaming path and normally advances at Darwin background priority while the Mac is in use or on battery. A native helper watches recent input plus consecutively abnormal display-link callback gaps or callback age and triggers the fast pause path when both indicate display-service trouble. It needs neither Accessibility nor Screen Recording permission and does not capture the screen. Main-display framebuffer age is recorded for diagnosis only and never triggers Pause by itself. If the strong display-link signal is unavailable, sustained non-H3 CPU or combined WindowServer/GPU pressure provides a fallback. `auto` also pauses on critical memory/swap/pageout or thermal pressure. After healthy recovery it probes in the background, and after five quiet AC-powered idle minutes it removes the background policy. These controls remain best-effort; `taskpolicy` is not a hard GPU quota.

macOS does not expose a universal frame-drop counter for arbitrary foreground applications. The guardian observes display-system responsiveness rather than another app's renderer, so even its native signal and fallback metrics are best-effort rather than hard real-time. `SIGSTOP` cannot retract Metal work already submitted to the GPU and does not release loaded weights from unified memory.

For the advanced h3.c route, the 64 GiB boundary is a safety heuristic rather than an engine requirement. The pinned engine reports roughly 40.1 GB peak physical footprint for complex resident Ref2VA examples, so 48 GB plus foreground applications can be tight. SSD streaming lowers DiT residency but performs large read-only model reads and can contend for disk bandwidth. See [resource control](docs/RESOURCE_CONTROL.md) before forcing resident mode.

## Choose one model route first

This project exposes two independent inference chains. Using ComfyUI does not require using vpipe:

```text
Original BF16: ComfyUI → H3 Generate Video (Metal) node → h3.c → MiniMax H3 FL2VA/Ref2VA BF16
Quantized Q8:  ComfyUI → H3 Generate with vpipe Q8 node → launchd worker → vpipe Q8
```

| Download option | Backend and weights | Capability | Disk requirement | Best fit |
|---|---|---|---:|---|
| `1) vpipe Q8 FL2VA` | vpipe + Q8 | First-frame FL2VA is required; no text-only generation or this bridge's ordered multi-reference Ref2VA chain | About 120 GiB free while preparing, about 67.5 GiB final | 24/48 GB Macs prioritizing speed and space |
| `2) h3.c Ref2VA BF16` | h3.c + official original BF16 | Ordered image, video, and audio references; automatically includes required FL2VA base files | About 196 GiB final; start with at least 220 GiB free | **Choose this for original weights plus multi-reference work on a 48 GB M5 Pro** |
| `3) h3.c FL2VA BF16` | h3.c + official original BF16 | Text-to-video or first/last-frame anchors; no ordered multi-reference chain | About 134 GiB final; start with at least 150 GiB free | Advanced users who explicitly do not need Ref2VA |

Option 2 downloads Ref2VA and its required FL2VA base directly. **Do not select option 3 first, and do not download or convert Q8.** Q8 and BF16 are independent assets that can coexist but do not replace one another; installing both adds their disk use.

"Original BF16" means that the on-disk checkpoints are the official MiniMax BF16 files. On a 48 GB Mac, `auto` and `low` use SSD streaming with original BF16 blocks. On supported M5 hardware, the resident `max` path may use h3.c's built-in INT8 MLP/QKV/attention projection kernels, so original checkpoint files do not promise pure-BF16 arithmetic on every execution path.

### 48 GB M5 Pro original-weight protocol for another GPT/AI coding agent

```bash
git clone https://github.com/Kingrd97/ComfyUI-H3-Mac.git
cd ComfyUI-H3-Mac
./Install.command
./Download\ Model.command Ref2VA
# The user reads the MiniMax H3 license and personally enters AGREE when prompted.
./Doctor.command
./Start.command
```

Constraints for the assistant performing the installation:

- First verify that `uname -m` is `arm64`, `xcrun --sdk macosx --show-sdk-version` is 26 or newer, `brew --version` works, and `df -h .` shows at least 220 GiB free. If Command Line Tools are missing, have the user run `xcode-select --install`; install Homebrew only from [brew.sh](https://brew.sh/).
- Confirm the final project directory before downloading large weights. Never install into the assistant's temporary sandbox.
- The command-line argument is `Ref2VA`, not numeric `2`. An assistant must not accept the model license for the user; stop and let the user personally enter `AGREE`.
- Do not run `Prepare vpipe Q8.command`, do not download Q8 merely to clear the vpipe worker's asset message, and do not silently change `auto_ssd_streaming_ram_gib` or default to `max`.
- Ctrl-C may interrupt a download. Rerun the same `./Download\ Model.command Ref2VA` command to reuse the content-addressed cache. Do not delete `runtime/models/.cache` or incomplete blobs.
- Installation is complete only when `Doctor.command` exits zero and reports FL2VA, Ref2VA, the pinned manifest, h3.c `--info`, and the H3 node as ready. Do not troubleshoot by blindly deleting `runtime/`.
- Installation and downloads need a network connection. Once dependencies, weights, and a local voice are present, BF16 video generation can run offline. Only an explicitly selected Neural speech service sends dialogue text to an online provider.
- Local text/image-to-video needs no Full Disk Access, Camera, Microphone, Accessibility, or Screen Recording permission. Grant a protected-folder, camera, or microphone permission only when the user explicitly chooses that input. ComfyUI listens only on `127.0.0.1` by default.

## One-click installation

1. Download or clone this repository.
2. Double-click `Install.command`. If Gatekeeper blocks it, right-click → Open; do not disable macOS security protections.
3. Double-click `Download Model.command` and select exactly one route from the table above: choose `1` for speed/space, or choose `2` for original weights and multi-reference work on a 48 GB M5 Pro.
4. Run `Doctor.command`. A BF16-only installation intentionally has no Q8 assets. If the vpipe worker says `degraded / Waiting for vpipe assets`, that is expected and does not affect h3.c inference through `H3 Generate Video (Metal)`.
5. The installer starts persistent ComfyUI and vpipe launchd services. Double-click `Start.command` to verify them and open `http://127.0.0.1:8188`. Select `Comfy > Locale > Language` to switch between English and Chinese.

Command-line equivalent:

```bash
git clone https://github.com/Kingrd97/ComfyUI-H3-Mac.git
cd ComfyUI-H3-Mac
./Install.command
./Download\ Model.command
./Doctor.command
./Start.command
```

For a fresh installation, ComfyUI, the pinned vpipe app bundle, h3.c, Python environments, the vpipe work directory, and models all live under `runtime/`. Choose the project's final location before downloading large weights. If you later move the whole directory:

- BF16-only: rerun `Install.command`, then `Doctor.command`. Project-relative content-addressed links continue to reuse the intact weights; no re-download is needed.
- vpipe Q8: rerun `Install.command`, then `Prepare vpipe Q8.command low` to refresh launchd paths, symlinks, and vpipe's model registry.

Validated upstream revisions and the official vpipe DMG checksum are pinned in `versions.env` instead of tracking unpredictable future main branches. On macOS 26, `Install.command` still installs the small vpipe runtime and starts its worker even for BF16-only use; that does not mean the approximately 67.5 GiB Q8 model has been downloaded.

Model downloading uses a separate pinned Python environment, so its Hugging Face client cannot change ComfyUI's dependencies. The exposed `runtime/models/MiniMax-H3` path is a relative link into a content-addressed cache inside the same `runtime/` tree; moving the whole project preserves it. A completed manifest records every expected path, size, blob identity, and model revision. `Doctor.command` checks that manifest and then asks h3.c to inspect the model with `--info`.

Configuration schema v4 has a conservative one-time upgrade path. A legacy configuration is backed up before migration; customized resource thresholds and existing external vpipe work directories are preserved, while the old unpinned `vpipe` executable default moves to the verified project-local binary.

Before upgrading an existing installation, wait for an active BF16 shot to finish or cancel it; do not restart services during inference. Then run:

```bash
git status --short       # make sure personal edits are saved first
git pull --ff-only
./Install.command
./Doctor.command
./Start.command
```

Complete pinned weights are normally reused. Rerun the same model-download command only if `Doctor.command` explicitly reports a model revision or manifest mismatch.

`Start.command` installs/checks the launchd services and opens the UI; it does not duplicate an already running server. The launchd program intentionally runs the ComfyUI control plane with PyTorch on CPU. This does **not** disable Metal generation: the H3 node starts a separate Metal engine. This default avoids unnecessary unified-memory use and PyTorch device-detection failures. For foreground diagnostics use `H3_FOREGROUND=1 H3_COMFY_DEVICE=auto ./Start.command`.

The pinned official ComfyUI frontend has native localization. Browser language is used on first launch; `Comfy > Locale > Language` changes it later. H3 node translations follow that setting without a third-party translation patch.

For the easiest start after preparing model option 1, open `Workflow > Browse Templates`, choose `ComfyUI-H3-Mac`, and load `H3_vpipe_Q8_2_Shot_Fixed_Voice`. The canvas is already grouped into references, Shot 1, Shot 2, final MP4 assembly, and one consistent narration pass. After model option 2, load `H3_Beginner_2_Shot_Storyboard`; it uses h3.c BF16/Ref2VA and now defaults to the conservative `preview + auto` settings for a first smoke test on a 48 GB M5 Pro.

## First workflow

### Recommended vpipe Q8 workflow

After model option 1 completes, load `example_workflows/H3_vpipe_Q8_2_Shot_Fixed_Voice.json`. Each `H3 · Generate with vpipe Q8` node renders a silent shot from a first-frame image; `H3 · Assemble storyboard MP4` joins the shots; `H3 · Add one fixed narration voice` applies every `seconds|dialogue` cue after assembly. The public template defaults to the offline `macOS:Tingting` voice. Neural voices are opt-in and send dialogue text to their online speech service. `Keep ambience` defaults off so independently generated H3 voices cannot overlap the final narration.

The node uses the project-local verified vpipe build first and submits durable tickets to a launchd-owned worker instead of making vpipe a disposable ComfyUI child. The worker stays online in a clear `degraded` state until the complete Q8 model and both pinned LoRAs pass verification. Advanced users can override paths and low-power memory-pool limits in `config.json`.

The shortest manual vpipe graph is:

1. `Load Image` for the shot's first frame.
2. Optional `H3 · Build Shot Prompt`; connect its text output to the generator.
3. `H3 · Generate with vpipe Q8 (Metal)`; connect the image to `First-frame reference`.
4. For a story, repeat the prompt/image/generator group and feed each `Job directory` to `H3 · Assemble storyboard MP4` in timeline order.
5. Add `H3 · Add one fixed narration voice` only after assembly, so every shot uses one voice.

The public template uses `960×544`, 124 frames, six Turbo steps, silent generation, and `resource=auto`. This is the recommended first run. `auto` remains background-friendly while the Mac is in use, pauses on sustained response pressure, and returns to normal priority after sustained idle on AC power. Use `max` only when the machine is otherwise idle.

For a higher-resolution final, select `turbo_highres_4step`, use at least `1152×640`, and set **exactly four steps**. Width and height must be multiples of 32, each between 256 and 1344; total canvas area may not exceed `1344×768`. This integration accepts 22–362 frames at 24 fps. Longer stories should still be split into reusable shots and assembled afterward.

The ordered multi-image reference nodes and the `preview / quality / reference` profiles below belong to the optional h3.c BF16/Ref2VA path, not the recommended vpipe Q8 FL2VA node. See the advanced h3.c workflow only after installing model option 2.

See the [storyboard tutorial](docs/STORYBOARD.md) for shot planning and assembly.

### First original-BF16 / Ref2VA workflow

1. Confirm that `./Download\ Model.command Ref2VA` completed model option 2 and that `Doctor.command` exits zero.
2. Load `H3_Beginner_2_Shot_Storyboard` from `Workflow > Browse Templates > ComfyUI-H3-Mac`.
3. Select the subject in `Load Image`, then connect `New reference list → Add image/audio/media reference` in temporal order. Reference order affects the result.
4. Keep both generators at `task=Ref2VA`. Start with the template's `640×384 / 3s / preview / auto`; after identity and motion direction are correct, move to `quality + auto`.
5. Ordered Ref2VA references cannot be combined with first/last-frame anchors. For text-only or first/last-frame work, install only option 3 if desired, add `H3 Generate Video (Metal)` manually, set `task=FL2VA`, and clear ordered references. There is currently no dedicated public FL2VA-BF16 template.

Do not let an assistant default a 48 GB M5 Pro to `max`. Both `auto` and `low` launch with SSD streaming at that memory tier; `auto` can additionally pause on sustained foreground pressure. Consider resident `max` only on an otherwise idle Mac after representative workloads show green memory pressure and negligible swap.

A BF16 installation is valid when `Doctor.command` exits zero and confirms FL2VA/Ref2VA, the pinned model manifest, h3.c `--info`, and the `H3GenerateVideo` node. For diagnosis, collect `runtime/comfyui-server.log` and `runtime/ComfyUI/output/h3-jobs/<job-id>/engine.log` before changing anything; do not blindly erase the model cache.

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
| low | Darwin background scheduling; vpipe uses its configured 12/8 GiB pool caps; always progresses | No |
| auto | Same conservative vpipe launch caps; background while in use/on battery, temporary pause under measured pressure, normal policy after five quiet AC-powered idle minutes | No |
| max | Normal priority and vpipe defaults; no automatic pause; use only while the Mac is idle | No |

The vpipe pool limits are fixed when a shot starts. Switching a running job from `auto` to `max` removes background scheduling but cannot recreate that process with different launch-time memory caps. For h3.c, the selected resident/SSD-streaming path is likewise fixed at launch.

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
├── vpipe-status.json  # vpipe jobs
├── engine.log
├── result.partial.mp4
└── result.mp4
```

An identical completed request can be reused. The vpipe worker survives a ComfyUI restart, and launchd restarts the worker if the controller itself exits; it reattaches to an exact surviving process group. After each vpipe engine exit, the worker cools down for 90 seconds by default. It starts the next shot only after public macOS memory headroom, wired-memory share, and swap/pageout growth remain healthy for three consecutive samples. If vpipe still refuses safely because wired Metal memory is short, the worker retains the identical prompt, reference, seed, geometry, and frame count and retries it once after another cooldown. The wait is recorded in `vpipe-status.json`; no Metal process runs during that wait and the queued shot can be cancelled.

**That durable-worker guarantee across ComfyUI restarts applies only to the vpipe route.** A live h3.c BF16 process can be paused and resumed, but do not restart ComfyUI/launchd services or update the project during BF16 inference. If the engine exits, that shot restarts from the beginning; already completed shots remain reusable.

Neither engine currently exports denoising-step state, so an engine process that actually dies cannot resume exactly from step 12/20. This retry restarts only the failed shot with identical settings; it is not a denoising checkpoint. Cancellation preserves logs and the partial file, although an unfinished MP4 may not be playable.

Double-click `H3 Control.command` to inspect, pause, resume, or change the scheduling policy of active jobs. The same controls are available from a shell:

```bash
./H3\ Control.command status
./H3\ Control.command pause
./H3\ Control.command resume
./H3\ Control.command auto
./H3\ Control.command max
```

These controls apply to registered h3.c **and vpipe** jobs. Pause/resume uses macOS `SIGSTOP/SIGCONT`: loaded weights and process state remain in RAM, so resuming does not reload or repeat completed CPU-side progress. This does not free unified memory, cannot revoke a Metal command buffer already committed to the GPU, is not a serialized checkpoint, and cannot survive engine exit or reboot. Use `resource=auto` for background progress with temporary pause when sustained foreground jank is detected. See [resource control](docs/RESOURCE_CONTROL.md).

Service supervision is separate from inference control:

```bash
./Service\ Control.command status
./Service\ Control.command restart --worker-only
```

Assembled projects are stored in `output/h3-storyboards/<storyboard-id>/`. If a later shot fails, completed shot jobs remain reusable.

## Why ComfyUI? What is Manager?

ComfyUI is the visual node graph, execution server, API, queue, history, and workflow format. It is the strongest open foundation for reproducible local generative workflows, but its raw graph UI is not automatically the easiest possible interface for a first-time creator. This project adds a smaller H3-specific creation layer rather than replacing that reliable foundation.

[ComfyUI-Manager](https://github.com/Comfy-Org/ComfyUI-Manager) is a separate extension for installing, updating, enabling, disabling, and snapshotting custom nodes and models. It is not another frontend and is not required here. We do not install it by default because this self-contained distribution pins validated revisions; unrestricted extension updates would make beginner installations less reproducible.

## Quantization direction

The pinned h3.c revision already selects its native resident INT8 MLP/QKV/attention projections on supported M5 hardware. SSD streaming is a separate original-BF16 path and disables those resident optimizations. A future UI revision will separate scheduling from the memory/engine path instead of presenting quantization as lossless acceleration; defaults will change only after same-prompt, seed, resolution, NFE, memory, and quality comparisons on a 48 GB M5 Pro.

## Validation status

- Automated backend tests, shell syntax, and GitHub Actions: verified.
- V3 node registration against the pinned ComfyUI revision: verified.
- Official vpipe v0.1.37 DMG checksum, Apple code signature, copied bundle, helper path, and pinned commit identity: verified on Apple Silicon.
- Q8 layout/index/size/immutable-revision verification and resumable compact preparation logic: covered by automated tests and checked against an existing complete Q8 installation.
- A full zero-to-video installation on a second physical M5 Pro Mac has not yet been completed; `Doctor.command`, first-start diagnostics, and actionable worker degradation are included specifically to make any remaining machine-specific issue visible.

## Privacy, licenses, and limitations

- Media processing and generation stay local. Selecting a Neural narration voice is the explicit exception: its dialogue text is sent to the corresponding online speech service.
- Models, generated media, logs, runtime files, and user configuration are Git-ignored.
- Put personal reusable graphs under `private_workflows/` or name them `*.private.json`; both patterns are Git-ignored.
- Before any push, an AI/coding assistant must run `git status --short` and must not commit `runtime/`, `output/`, `config.json`, source media, or private workflows. Installing this public repository never requires the user to provide a GitHub token.
- This bridge is MIT-licensed; ComfyUI, its official frontend, h3.c, FFmpeg, and the model retain their own licenses. See [THIRD_PARTY.md](THIRD_PARTY.md) for explicit upstream attribution.
- MiniMax H3 weights require acceptance of the MiniMax H3 Community License.
- Apple Silicon macOS only.
- h3.c requires the original directory layout. Ref2VA also requires the FL2VA base files.
- Ordered Ref2VA references cannot be mixed with first/last-frame anchors.
- vpipe Q8 currently supports first-frame FL2VA in this bridge; use h3.c BF16 for ordered multi-reference Ref2VA workflows.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install pytest numpy pillow typing_extensions
.venv/bin/pytest -q
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for boundaries and extension points. Chinese users can continue with [docs/QUICKSTART_zh.md](docs/QUICKSTART_zh.md) and the [Chinese storyboard tutorial](docs/STORYBOARD_zh-CN.md).
