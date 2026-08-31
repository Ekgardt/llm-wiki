"""The compile gets its own provider ceiling; everyone else keeps the short one.

Measured 2026-08-28 on the live vault: the compile failed with
`draft:claude:provider_timeout` at the 90s default and the same daily compiled
at 600s, the whole pass — a rejected draft, its retry and the critique batches
— taking 225s of wall time. So one call is over 90s and under 225s. Raising
the global default instead would make a stuck capture flush wait three times
longer before anyone heard about it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import compile_memory  # noqa: E402
import llm_client  # noqa: E402


@pytest.fixture(autouse=True)
def _no_environment_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEMORY_LLM_TIMEOUT_S", raising=False)


def test_the_default_ceiling_stays_short() -> None:
    assert llm_client._timeout_s() == llm_client.DEFAULT_TIMEOUT_S


def test_a_block_raises_the_ceiling_only_inside_itself() -> None:
    with llm_client.call_ceiling(300):
        inside = llm_client._timeout_s()
    assert (inside, llm_client._timeout_s()) == (300, llm_client.DEFAULT_TIMEOUT_S)


def test_the_ceiling_is_restored_after_a_failure() -> None:
    with pytest.raises(RuntimeError), llm_client.call_ceiling(300):
        raise RuntimeError("the body failed")
    assert llm_client._timeout_s() == llm_client.DEFAULT_TIMEOUT_S


def test_blocks_nest_and_unwind_in_order() -> None:
    with llm_client.call_ceiling(300):
        with llm_client.call_ceiling(120):
            inner = llm_client._timeout_s()
        outer = llm_client._timeout_s()
    assert (inner, outer) == (120, 300)


def test_the_environment_still_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator debugging a hang can widen every call from outside."""
    monkeypatch.setenv("MEMORY_LLM_TIMEOUT_S", "45")
    with llm_client.call_ceiling(300):
        assert llm_client._timeout_s() == 45


@pytest.mark.parametrize("bad", [0, -1, 1.5, True, "300", None])
def test_a_ceiling_that_is_not_positive_whole_seconds_is_refused(bad: object) -> None:
    with pytest.raises(ValueError):
        with llm_client.call_ceiling(bad):  # type: ignore[arg-type]
            pass


def test_the_compile_ceiling_is_above_the_default_and_below_the_observed_pass() -> None:
    ceiling = compile_memory.COMPILE_PROVIDER_CEILING_S
    assert llm_client.DEFAULT_TIMEOUT_S < ceiling
    assert ceiling < 600
