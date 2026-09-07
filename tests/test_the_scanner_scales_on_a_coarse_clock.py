"""The POSIX path scanner scales near-linearly, measured on a clock that can see it.

Moved out of `test_lsp_security.py` on 2026-09-07 with its floor corrected.
Windows reports a 100 ns resolution for `process_time` and delivers 15.625 ms
ticks; the reported figure is not the tick. Measured on CI 2026-09-07: four
ticks (0.0625 s) against a floor of 0.0508 s, one clock edge, called a scaling
failure. The floor is now eight ticks of the clock the machine actually has.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import lsp_security  # noqa: E402

ROOT = PurePosixPath("/srv/Program Files/linear-repository")
NATIVE_ROOT = ROOT.as_posix()
URI_ROOT = "file:///srv/Program%20Files/linear-repository"
WINDOWS_PROCESS_TIME_TICK = 0.015625
_SHAPES = {
    "native": lambda index: f"path={NATIVE_ROOT}/pkg/module-{index}.py:12:34",
    "uri": lambda index: f"uri={URI_ROOT}/pkg/module-{index}.py:56:78",
    "dotted": lambda index: (
        f"/srv/scratch/../Program Files/linear-repository/pkg/module-{index}.py"
    ),
    "sibling": lambda index: f"path={NATIVE_ROOT}-sibling/module-{index}.py",
    "outside": lambda index: f"path=/outside/module-{index}.py",
}
_ORDER = tuple(_SHAPES)


def _token(index: int) -> str:
    """One token of each shape the scanner must classify, by position."""
    return _SHAPES[_ORDER[index % len(_ORDER)]](index)


def _separator(index: int) -> str:
    return "," if index % 2 else " "


def _joined(count: int) -> str:
    return "".join(_token(index) + _separator(index) for index in range(count))


def _process_clock_tick() -> float:
    """The real granularity of `process_time`, not the figure the OS reports."""
    reported = time.get_clock_info("process_time").resolution
    if os.name != "nt":
        return reported
    return max(reported, WINDOWS_PROCESS_TIME_TICK)


def _measure(count: int) -> float:
    """CPU time, not wall time: contention can only pull it towards the truth."""
    value = _joined(count)
    started = time.process_time()
    result = lsp_security._redact_path(value, ROOT, "<repository>")
    elapsed = time.process_time() - started
    assert result.count("<repository>") == count * 3 // 5
    return elapsed


def _cheapest(count: int, attempts: int = 5) -> float:
    return min(_measure(count) for _attempt in range(attempts))


def test_the_scanner_redacts_every_shape_it_is_asked_to():
    multiple = (
        "bad=file://server/srv/Program%20Files/linear-repository/one.py,"
        "/srv/scratch/../Program Files/linear-repository/two.py;"
        "file:///srv/scratch/../Program%20Files/linear-repository/three.py tail"
    )

    assert lsp_security._redact_path(multiple, ROOT, "<repository>") == (
        "bad=file://server/srv/Program%20Files/linear-repository/one.py,"
        "<repository>;<repository> tail"
    )


def test_the_scanner_scales_near_linearly_for_200_400_800_tokens():
    timings = tuple(_cheapest(count) for count in (200, 400, 800))
    # A ratio cannot be measured with a clock coarser than the quantity: the
    # floor is several ticks of whatever clock this machine actually has.
    floor = max(0.05, 8 * _process_clock_tick())

    assert timings[1] <= max(floor, timings[0] * 3.25)
    assert timings[2] <= max(floor, timings[1] * 3.25)
    assert sum(timings) < 5.0


def test_the_windows_tick_is_the_floor_on_windows(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")

    assert _process_clock_tick() >= WINDOWS_PROCESS_TIME_TICK
