"""One noise floor for every ratio gate that times a small piece of work.

A near-linear claim is checked by comparing two measurements, and at these
magnitudes a shared runner adds more than the work costs. Two guards were already
in place — the best of five attempts, and a floor of several clock ticks — and a
macOS shard still failed `0.0946 <= 0.0714` on PR 34 (2026-09-13): the quiet time
of that step is about 22 ms, and the scheduler added 70 ms to the best of five.

So the floor has to cover scheduler noise, not just clock granularity. Below it a
ratio says nothing and the gate stops asking; above it the ratio is the algorithm
again. What actually proves linearity in those tests is deterministic and
untouched: the counted calls into the scanner, which double exactly.

Research: `docs/research/2026-09-13-a-shorter-answer-and-a-fresher-line.md`.
"""
from __future__ import annotations

import os
import time

# Measured 2026-09-13 on a hosted macOS runner: 70 ms of noise on a 22 ms step,
# after taking the best of five attempts.
SCHEDULER_NOISE_SECONDS = 0.25
# `process_time` on Windows advances in ~15.6 ms steps whatever it reports.
WINDOWS_PROCESS_TIME_TICK = 0.015625


def process_clock_tick() -> float:
    """The real granularity of `process_time`, not the figure the OS reports."""
    reported = time.get_clock_info("process_time").resolution
    if os.name != "nt":
        return reported
    return max(reported, WINDOWS_PROCESS_TIME_TICK)


def noise_floor_seconds() -> float:
    """Below this, a timing ratio measures the machine and not the code."""
    return max(SCHEDULER_NOISE_SECONDS, 8 * process_clock_tick())
