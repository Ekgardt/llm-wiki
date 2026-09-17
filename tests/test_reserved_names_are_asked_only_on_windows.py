"""Audit 3, A1: pathlib's is_reserved() is removed in Python 3.15.

Research: `docs/research/2026-09-17-reserved-names-are-a-windows-question.md`.
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import pyright_profile  # noqa: E402


class _PathWithoutIsReserved(type(Path())):
    """The path object Python 3.15 hands us: the method is gone."""

    @property
    def is_reserved(self):
        raise AttributeError("is_reserved")


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX half of the rule")
def test_a_posix_path_is_judged_without_the_removed_method(tmp_path: Path) -> None:
    candidate = _PathWithoutIsReserved(tmp_path / "node")
    assert pyright_profile._is_local_absolute_path(candidate) is True


def test_the_windows_rule_reads_the_text_and_never_warns() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        verdicts = (
            pyright_profile._windows_name_is_reserved("C:\\tools\\nul"),
            pyright_profile._windows_name_is_reserved("C:\\tools\\node.exe"),
        )
    assert verdicts == (True, False)
