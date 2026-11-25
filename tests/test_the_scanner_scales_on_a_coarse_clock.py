"""The POSIX path scanner scales near-linearly, counted rather than clocked.

It used to be timed with `process_time`, and every floor put under that ratio
-- clock ticks, then scheduler noise -- was a margin that one loaded runner
crossed. The characters the scanner touches are the same on every machine, so
the near-linear claim is now made about them.
"""

from __future__ import annotations

import sys
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import lsp_security  # noqa: E402

ROOT = PurePosixPath("/srv/Program Files/linear-repository")
NATIVE_ROOT = ROOT.as_posix()
URI_ROOT = "file:///srv/Program%20Files/linear-repository"
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


def _count_touched(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """One running total of the characters each component step touches."""
    touched = [0]
    real = lsp_security._posix_add_semantic_component

    def counted(raw_component: str, components: list[str], *args: Any, **kwargs: Any) -> Any:
        touched[0] += len(raw_component) + len(components)
        return real(raw_component, components, *args, **kwargs)

    monkeypatch.setattr(lsp_security, "_posix_add_semantic_component", counted)
    return touched


def _measure(count: int, touched: list[int]) -> int:
    value = _joined(count)
    before = touched[0]
    result = lsp_security._redact_path(value, ROOT, "<repository>")
    assert result.count("<repository>") == count * 3 // 5
    return touched[0] - before


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


def test_the_scanner_scales_near_linearly_for_200_400_800_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    touched = _count_touched(monkeypatch)
    counts = (200, 400, 800)
    work = tuple(_measure(count, touched) for count in counts)

    assert work[1] <= work[0] * 3
    assert work[2] <= work[1] * 3
    assert sum(work) <= sum(len(_joined(count)) for count in counts)

