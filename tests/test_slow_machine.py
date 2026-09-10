"""The waits are named, ordered, and scaled at the invocation, never literal."""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_the_two_waits_are_ordered_and_the_pause_outlives_them() -> None:
    from tests import slow_machine

    assert 0 < slow_machine.SHORT_TIMEOUT < slow_machine.LONG_TIMEOUT
    assert slow_machine.PAUSE_TIMEOUT > slow_machine.LONG_TIMEOUT


@pytest.mark.parametrize(("scale", "expected"), [("2", 600.0), ("0.5", 150.0)])
def test_a_loaded_machine_scales_every_wait_at_the_invocation(scale, expected) -> None:
    code = "from tests import slow_machine; print(slow_machine.LONG_TIMEOUT)"
    # The child inherits the environment: a Windows Python started without
    # SYSTEMROOT dies before its first import (python/cpython#105436).
    env = {**os.environ, "LLM_WIKI_TEST_TIMEOUT_SCALE": scale, "PYTHONPATH": str(ROOT)}
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert float(result.stdout.strip()) == expected


def test_a_scale_that_is_not_positive_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests import slow_machine

    monkeypatch.setenv("LLM_WIKI_TEST_TIMEOUT_SCALE", "0")
    with pytest.raises(ValueError, match="must be positive"):
        importlib.reload(slow_machine)
    monkeypatch.delenv("LLM_WIKI_TEST_TIMEOUT_SCALE")
    importlib.reload(slow_machine)
