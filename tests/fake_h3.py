#!/usr/bin/env python3
from __future__ import annotations

import sys
import time
from pathlib import Path


def value_after(option: str) -> str:
    return sys.argv[sys.argv.index(option) + 1]


steps = int(value_after("--steps"))
for current in range(1, steps + 1):
    print(f"sample {current}/{steps}", flush=True)
    time.sleep(0.01)

Path(value_after("-o")).write_bytes(b"fake-mp4-for-tests")
