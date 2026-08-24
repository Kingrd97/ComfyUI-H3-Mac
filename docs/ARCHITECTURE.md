# Architecture

```text
ComfyUI graph + native en/zh locale data
   │ IMAGE / AUDIO / local media paths
   ▼
H3 shot-prompt + reference-chain nodes ── preserves user-defined ordering
   │ H3Request
   ▼
h3_bridge runner / durable vpipe ticket client
   ├── validation and deterministic job ID
   ├── reversible low / auto / max Darwin process policy
   ├── permission-free native input/display responsiveness guardian
   ├── WindowServer/GPU/external-CPU fallback + AC-power controller
   ├── best-effort in-memory SIGSTOP/SIGCONT pause and resume
   ├── progress and cancellation
   └── persistent request, log, partial and result files
   │ argv (never shell=True)
   ├── h3.c runner: Ref2VA / FL2VA, adaptive pause and resume
   └── vpipe client: durable ticket + status observer
       │
       ▼
   launchd KeepAlive vpipe worker
   ├── owns the one-at-a-time queue independently of ComfyUI
   ├── exact-PGID recovery after a worker restart
   └── shared low / auto / max scheduler and SIGSTOP/SIGCONT control
   ▼
antirez/h3.c or vpipe ── Metal inference ── FFmpeg MP4
   │
   ▼
ComfyUI native VIDEO output and preview
   │ completed shot job directories
   ▼
Storyboard assembler ── validated paths ── FFmpeg stream-copy MP4
   │
   ▼
Fixed narration ── one Neural or offline macOS voice + timed cues ── final AAC MP4
```

## Boundaries

- ComfyUI owns graph composition and reusable creative workflows.
- The bridge owns input materialization, validation, scheduling and job persistence.
- launchd keeps the ComfyUI control plane and vpipe worker alive. It supervises the worker, not a successfully completed per-shot command, avoiding accidental infinite reruns.
- The storyboard layer owns structured prompt composition and ordered assembly of completed jobs; it never runs model inference itself.
- h3.c or vpipe owns model loading, conditioning, inference, decoding and MP4 generation.
- The narration layer intentionally runs after assembly. It prevents independently sampled H3 audio from changing the character voice between shots.
- Model acquisition is separate because model terms and storage needs differ from source code.

## Safety properties

- Subprocesses receive argument vectors; prompts and paths are never interpolated into a shell command.
- The server listens on `127.0.0.1` by default.
- Runtime/model/output/config files are excluded from Git.
- A cancelled job terminates the entire child process group.
- Pause, resume, and policy changes target only the recorded h3.c or vpipe process group; no process-name-wide signals are used.
- `control.json` records user intent while `process.json` records the effective live state. Neither file is treated as a serialized tensor checkpoint.
- Adaptive protection primarily combines recent input with consecutive abnormal display-link callback gaps or callback age from the native helper; it needs no Accessibility or Screen Recording permission and captures no screen content. Framebuffer age is diagnostic telemetry only and never triggers Pause by itself. Sustained WindowServer/GPU/external-CPU metrics are the fallback.
- macOS does not expose a universal frame-drop counter for arbitrary foreground applications. A stopped process retains unified memory, and already committed Metal command buffers cannot be retracted, so pause is best-effort rather than hard real-time.
- Configuration schema v2 migration first writes `config.json.v1-backup`. It changes `background` to `adaptive` only for a configuration that exactly matches the former shipped defaults; customized policy or thresholds are preserved.
- Ordered references are immutable tuples, so downstream nodes cannot mutate an earlier branch.
- Storyboard inputs must resolve below the configured H3 jobs directory, preventing arbitrary local files from being read through the node.

## Backend extension

`H3Request` is deliberately independent from ComfyUI types. A future adapter can implement a common runner protocol and expose engine-specific capability flags. stable-diffusion.cpp should be added as a distinct backend—not treated as a drop-in synonym—because its model formats, arguments and supported H3 conditioning paths differ.

Quantized h3.c execution should likewise be an explicit engine capability. The bridge must not combine an INT8 mode with `--ssd-streaming` unless the pinned h3.c revision declares that combination supported, and it must include the engine mode in the deterministic job request before completed-result reuse is allowed.
