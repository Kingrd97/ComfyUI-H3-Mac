# Resource scheduling, pause, and resume on a 48 GB M5 Pro

Use `resource_profile=auto` in the H3 generation node.

## Profiles

- `auto` is the recommended 48 GB default. Its default `adaptive` policy uses SSD streaming below 64 GiB at process start and normally runs at Darwin background priority while the Mac is in use or on battery. It temporarily pauses on a native responsiveness signal or sustained fallback pressure, probes recovery at background priority, and removes the background policy only after five quiet AC-powered idle minutes.
- `low` uses SSD streaming and Darwin background scheduling. It always makes progress and does not pause for user input.
- `max` never pauses and does not use SSD streaming. It is intended for an idle Mac with enough memory; complex resident Ref2VA runs can be very close to the limit on a 48 GB machine.

`low`, `max`, and manual Pause retain those exact meanings; adaptive pausing applies only to `auto`.

On Macs below 64 GiB, the normal node limit is one 5-second shot. Use storyboard assembly for longer work. The h3.c hard limit remains 362 frames (about 15.08 seconds), but `H3_ALLOW_LARGE_JOB=1` is an explicit expert override because [h3.c issue #5](https://github.com/antirez/h3.c/issues/5) reports a 10-second 960×544 run on a 64 GB M4 Max growing to roughly 64 GiB of swap during VAE decode. An override is not a promise that a 48 GB machine can finish safely.

## How adaptive response protection works

`Install.command` builds a small native `h3-guardian` helper. It runs only while the selected scheduler policy is `auto`, restarts after an unexpected exit, and stops again for `low` or `max`. It connects to the display system without activating an app or creating a window, samples recent session input and display-link callback timing, and captures no screen content. It also reports macOS thermal state and Low Power Mode. It needs neither Accessibility nor Screen Recording permission. Main-display framebuffer age is emitted as diagnostic telemetry only; it is not a Pause trigger. Sleep/wake and display-reconfiguration events clear the cadence window so they are not mistaken for foreground jank. If an older Xcode SDK cannot compile the helper, installation continues and `auto` uses the fallback metrics until the Command Line Tools are upgraded and installation is rerun.

The primary signal is recent keyboard/mouse input together with display-link callback gaps or callback age that stay abnormal across consecutive native samples. That indicates the display callback service itself is not arriving on cadence, so the scheduler can pause H3 at its next 0.5-second control poll without waiting for the slower fallback timer. Framebuffer age can look stale for benign reasons and never enters this strong trigger. The helper still does not read the foreground application's own renderer or FPS.

If the native signal is unavailable or has not fired, the scheduler falls back to sustained system metrics. With the defaults, either of these conditions must persist for about two seconds:

- processes outside the H3 process group use at least 300% CPU in aggregate (about three fully occupied CPU cores in macOS process accounting); or
- input occurred within the last five seconds and WindowServer CPU is at least 80% together with GPU utilization of at least 92%. A sustained display-link delay may also combine with high WindowServer or GPU use.

The controller checks native/user/control state every 0.5 seconds and refreshes the heavier process/GPU fallback metrics every two seconds. After the overload clears and the recovery indicators stay healthy for 15 seconds, H3 resumes at background priority for a 20-second probe. A relapse during that probe pauses it again; otherwise it returns to normal background progress. Separately, after five minutes without keyboard/mouse input and on AC power, auto waits for a fresh sample with low external CPU and settled WindowServer/display signals before removing the Darwin background policy for full-speed idle generation.

Every ten seconds, `auto` also samples the public `memory_pressure`, `vm.swapusage`, and `vm_stat` diagnostics. It pauses immediately when the advisory free-memory percentage reaches 8% or lower, the thermal state becomes serious/critical, or swap/pageout growth crosses the configured rate limits. Resume still goes through the normal healthy wait and background probe; memory must recover to at least 15%. Fair thermal state, Low Power Mode, memory below the recovery threshold, battery power, or active foreground load blocks idle-max. These are conservative proxies, and Pause prevents additional pressure but cannot evict H3's existing unified-memory allocations.

Darwin `taskpolicy` is also best-effort. A failed background/foreground policy change is recorded as unapplied and retried after a backoff; it is not reported as successful. It adjusts CPU/I/O scheduling priority and does not impose a hard Metal GPU quota. Live status is rewritten on a state change or every 15 seconds rather than every metrics sample.

macOS does not provide a public, universal API for the actual frame-drop rate of every foreground application. The native display signal and CPU/WindowServer/GPU metrics are therefore responsiveness evidence and proxies, not proof that a particular app dropped a frame. The protection is best-effort rather than hard real-time. In particular, `SIGSTOP` cannot retract a Metal command buffer already committed to the GPU, so some GPU work may finish after the pause decision, and stopping the process does not release its loaded weights from unified memory. If the helper is missing or exits, auto fails over to the metric path rather than requiring extra permissions.

The engine is wrapped with `caffeinate -s`: it prevents system idle sleep only while running on AC power. On battery, normal macOS idle-sleep policy remains effective. If ComfyUI crashes, the next `Start.command` cleans an H3 child only when both engine and controller birth fingerprints prove that the exact controller has exited; legacy or ambiguous records are left untouched. This startup recovery reduces orphan risk but is not an on-disk denoising checkpoint.

## Control a running job

Double-click `H3 Control.command`, or run:

```bash
./H3\ Control.command status
./H3\ Control.command pause
./H3\ Control.command resume
./H3\ Control.command auto
./H3\ Control.command low
./H3\ Control.command max
```

Each job stores live state in `process.json`, user intent in `control.json`, and denoising progress in `progress.json`.

Changing policy during a run changes pause state and Darwin scheduling only. SSD streaming is fixed when the engine process starts and cannot be toggled halfway through denoising; start a new shot run with another resource profile to change the memory strategy.

Therefore an `auto` job started with streaming does not become fully resident when the Mac later becomes idle. “Idle boost” means normal process scheduling, not a hot conversion from streamed BF16 blocks to resident weights.

## What SSD streaming costs

The 64 GiB rule is a conservative bridge heuristic, not a requirement imposed by h3.c. The pinned upstream engine reports that SSD streaming reduces tracked DiT storage from about 36.5 GiB to 2.0–2.1 GiB, while a complete forward was 26–84% slower depending on canvas shape. Complex resident Ref2VA examples reached about 40.1 GB process physical footprint, which leaves little room for macOS and foreground applications on a 48 GB machine.

Streaming uses read-only uncached checkpoint reads. It does not rewrite model weights, so logical read volume must not be treated as the same amount of SSD TBW. It can still consume bandwidth, power, and thermal headroom. Approximate model reads with the current presets are:

| Quality | Approximate checkpoint reads |
|---|---:|
| preview | 144 GiB |
| balanced | 356 GiB |
| quality | 719 GiB |
| reference | 1.75 TiB |

On supported M5 hardware the resident path also enables h3.c's default INT8 MLP/QKV/attention projections, while SSD streaming uses original BF16 blocks. The selected steps, layers, and reuse remain unchanged, but fine detail or framing can differ slightly between those arithmetic paths. See the pinned [h3.c memory and streaming notes](https://github.com/antirez/h3.c/tree/8974cc055ea9c02fcd14cc27dfda3e1027c05153#2-make-a-first-fast-video).

## Pause versus a disk checkpoint

Pause uses `SIGSTOP`, retaining the process state and loaded weights in unified memory. `SIGCONT` resumes in place without reloading or repeating completed CPU-side progress. Already committed Metal work is not retractable, and pausing does not return the model's unified memory to other applications.

This state cannot survive process exit, logout, or reboot. h3.c does not currently expose portable denoising-tensor checkpoints. The bridge still preserves the request, logs, latest progress, partial output, and every completed shot.

## Configuration

The defaults can be changed in `config.json`:

```json
{
  "auto_idle_seconds": 300,
  "auto_poll_seconds": 0.5,
  "auto_metrics_poll_seconds": 2,
  "auto_health_poll_seconds": 10,
  "auto_status_interval_seconds": 15,
  "auto_max_external_cpu_percent": 120,
  "auto_active_behavior": "adaptive",
  "auto_jank_interaction_seconds": 5,
  "auto_jank_pause_seconds": 2,
  "auto_jank_recover_seconds": 15,
  "auto_jank_probe_seconds": 20,
  "auto_jank_cpu_percent": 300,
  "auto_jank_window_server_percent": 80,
  "auto_jank_window_server_recover_percent": 50,
  "auto_jank_gpu_percent": 92,
  "auto_jank_gpu_recover_percent": 70,
  "auto_memory_pause_percent": 8,
  "auto_memory_recover_percent": 15,
  "auto_swap_growth_pause_mib_per_minute": 512,
  "auto_pageout_pause_mib_per_minute": 256,
  "auto_require_ac_power": true
}
```

`adaptive` is the default described above. Set `auto_active_behavior` to `background` to disable automatic responsiveness pauses and always keep progressing at background priority during active use, or to `pause` for the older strict behavior that stops H3 whenever the Mac is actively used. Manual Pause always overrides every resource profile. `auto_max_external_cpu_percent` remains the low-CPU gate for the five-minute idle boost; `auto_jank_*` values tune the sustained metric fallback and recovery state machine. The native display-link callback signal has deliberately conservative internal timing and is not a claim to measure another app's FPS; framebuffer age remains diagnostic only.

When an existing installation upgrades to configuration schema v2, the original file is backed up as `config.json.v1-backup` before migration. A configuration is changed from `background` to `adaptive` only when it exactly matches all former shipped defaults. Any customized behavior or threshold is retained, so upgrading does not silently replace a deliberate resource policy.

Advanced users can set `auto_ssd_streaming_ram_gib` to `0` to make newly started `auto` jobs use resident weights while retaining adaptive scheduling. On a 48 GB Mac, do this only after a representative smoke test shows green memory pressure and negligible swap with the actual foreground applications open. It does not change a job that is already running.
