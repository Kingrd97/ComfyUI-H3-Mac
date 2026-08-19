# Resource scheduling, pause, and resume on a 48 GB M5 Pro

Use `resource_profile=auto` in the H3 generation node.

## Profiles

- `auto` is the recommended 48 GB default. It uses SSD streaming below 64 GiB, pauses for keyboard/mouse activity, substantial external CPU load, or battery power, then resumes at full scheduling policy after 60 AC-powered idle seconds.
- `low` uses SSD streaming and Darwin background scheduling. It always makes progress and does not pause for user input.
- `max` never pauses and does not use SSD streaming. It is intended for an idle Mac with enough memory; original BF16 Ref2VA can be very close to the limit on a 48 GB machine.

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

## Pause versus a disk checkpoint

Pause uses `SIGSTOP`, retaining the exact computation and loaded weights in unified memory. `SIGCONT` resumes in place without reloading or repeating completed steps.

This state cannot survive process exit, logout, or reboot. h3.c does not currently expose portable denoising-tensor checkpoints. The bridge still preserves the request, logs, latest progress, partial output, and every completed shot.

## Configuration

The defaults can be changed in `config.json`:

```json
{
  "auto_idle_seconds": 60,
  "auto_poll_seconds": 2,
  "auto_max_external_cpu_percent": 120,
  "auto_active_behavior": "pause",
  "auto_require_ac_power": true
}
```

Set `auto_active_behavior` to `background` if H3 should continue slowly while the Mac is being used. Keep `pause` when foreground responsiveness matters most.
