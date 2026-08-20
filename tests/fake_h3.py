#!/usr/bin/env python3
from __future__ import annotations

import sys
import time
from pathlib import Path


def value_after(option: str) -> str:
    return sys.argv[sys.argv.index(option) + 1]


if Path.cwd() != Path(sys.argv[0]).resolve().parent:
    print("fake h3 was not launched from its engine directory", file=sys.stderr)
    raise SystemExit(78)

steps = int(value_after("--steps"))
if value_after("-p") == "cr-progress-wait":
    print(f"sample 1/{steps}", end="\r", flush=True)
    time.sleep(30)
for current in range(1, steps + 1):
    print(f"sample {current}/{steps}", flush=True)
    time.sleep(0.01)

Path(value_after("-o")).write_bytes(b"fake-mp4-for-tests")
