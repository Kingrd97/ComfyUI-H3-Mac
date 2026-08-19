# Resource scheduling, pause, and resume on a 48 GB M5 Pro

Use `resource_profile=auto` in the H3 generation node.

## Profiles

- `auto` is the recommended 48 GB default. It uses SSD streaming below 64 GiB at process start, runs at Darwin background priority during keyboard/mouse activity, substantial external CPU load, or battery power, then removes the background policy after five AC-powered idle minutes. It keeps making progress in both states.
- `low` uses SSD streaming and Darwin background scheduling. It always makes progress and does not pause for user input.
- `max` never pauses and does not use SSD streaming. It is intended for an idle Mac with enough memory; complex resident Ref2VA runs can be very close to the limit on a 48 GB machine.

The controller checks every two seconds. A Metal command buffer already submitted to the GPU may need a short time to finish, so returning to the Mac is not guaranteed to stop GPU work within milliseconds.

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

Pause uses `SIGSTOP`, retaining the exact computation and loaded weights in unified memory. `SIGCONT` resumes in place without reloading or repeating completed steps.

This state cannot survive process exit, logout, or reboot. h3.c does not currently expose portable denoising-tensor checkpoints. The bridge still preserves the request, logs, latest progress, partial output, and every completed shot.

## Configuration

The defaults can be changed in `config.json`:

```json
{
  "auto_idle_seconds": 300,
  "auto_poll_seconds": 2,
  "auto_max_external_cpu_percent": 120,
  "auto_active_behavior": "background",
  "auto_require_ac_power": true
}
```

`background` is the default: H3 keeps progressing and macOS gives foreground CPU and I/O higher priority. Set `auto_active_behavior` to `pause` only if active use must stop H3 completely. Manual Pause always overrides every resource profile.

Advanced users can set `auto_ssd_streaming_ram_gib` to `0` to make newly started `auto` jobs use resident weights while retaining adaptive scheduling. On a 48 GB Mac, do this only after a representative smoke test shows green memory pressure and negligible swap with the actual foreground applications open. It does not change a job that is already running.
