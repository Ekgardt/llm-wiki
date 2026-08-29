"""The shared server warmed once, and one pass records the wrong cost.

`mcp_http` ran a single throwaway question before serving. That pass pays the
one-time model load and records it, clamped at the ceiling — the figure that
means "never fits" — so the first real question still found the dense leg
refused. The defect is not about this transport: it is the same admission
arithmetic `mcp_server.warmup_retrieval_path` was given two passes for.

The earlier measurement here timed the answer rather than asking which legs
reached it, which is how one pass looked sufficient.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

mcp_http = pytest.importorskip("mcp_http")

import mcp_server  # noqa: E402


@pytest.fixture
def warmed(monkeypatch) -> dict[str, list]:
    seen: dict[str, list] = {"shared": [], "encoder": []}

    monkeypatch.setattr(
        mcp_server,
        "warmup_retrieval_path",
        lambda seconds: seen["shared"].append(seconds),
    )
    monkeypatch.setattr(
        mcp_server,
        "_start_encoder_warmup",
        lambda: seen["encoder"].append(True),
    )
    return seen


def test_the_shared_warm_up_uses_the_two_pass_path(warmed) -> None:
    """One place decides what warming means, and it is not this module."""
    mcp_http._run_warmup_query()

    assert warmed["shared"] == [mcp_http._WARMUP_LIMIT_SECONDS]


def test_the_opt_out_falls_back_to_the_background_load(monkeypatch, warmed) -> None:
    """Skipping the synchronous warm must not remove all warming."""
    monkeypatch.setenv("LLMWIKI_NO_SHARED_WARMUP", "1")

    mcp_http._warm_shared_surface()

    assert warmed["shared"] == []
    assert warmed["encoder"] == [True]


def test_the_synchronous_warm_up_does_not_also_start_the_background_one(
    monkeypatch, warmed
) -> None:
    """Two full warm-ups racing on a small machine is the thing being fixed."""
    monkeypatch.delenv("LLMWIKI_NO_SHARED_WARMUP", raising=False)

    mcp_http._warm_shared_surface()

    assert warmed["shared"] == [mcp_http._WARMUP_LIMIT_SECONDS]
    assert warmed["encoder"] == []
