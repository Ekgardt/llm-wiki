"""The retrieval entry points a real caller uses, called the way it calls them.

Both vault stands used to ask `search_memory.search(question, limit=k)` and
nothing more. No caller in this product asks for retrieval that way. The MCP
tool -- the path an agent takes -- goes through `mcp_server._search_vault`,
which imposes the operation budget, decides the candidate pool, and falls back
to a lexical answer when the budget runs out. The CLI -- the path an operator
takes -- passes its own defaults and deliberately leaves the cross-encoder off.

Four separately confirmed retrieval defects (`knowledge/log.md`, 2026-08-26)
lived in exactly that gap: an optional-stage ceiling that only bites when a
deadline is passed, temporal routing that dropped the dense signal, an entry
point that never resolved a query encoder, and answer size passed as
`max_candidates` inside the MCP wrapper. A stand calling `search()` with no
budget and no wrapper could not see any of them, and did not.

So the stands come through here instead, and the shape of that call is one
thing to keep honest rather than two. `tests/test_vault_stand_entry_points.py`
reads the shape off the product and fails when this file drifts from it.
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

# The agent's path, the operator's path, and the shape the stands used to use.
MCP = "mcp"
CLI = "cli"
API = "api"
PATHS = (MCP, CLI, API)
DEFAULT_PATH = MCP

# What `search_memory._run_cli_search` passes for a bare
# `search_memory.py "<query>"`. Copied here so a stand can call it, and pinned
# against the real parser by the test, which reads the defaults off the product
# rather than trusting this copy.
CLI_SCOPE = "all"
CLI_KWARGS: dict[str, object] = {
    "force_rebuild": False,
    "project": None,
    "since": None,
    "as_of": None,
    "semantic": True,
    "profile": None,
    "graph": True,
    # The CLI leaves the cross-encoder off on purpose: about twenty seconds to
    # load in a cold process, and a one-shot call pays that every time.
    "rerank": False,
}

# One question that is nothing like the corpus, used only to make the process
# resemble a resident server before anything is measured.
WARMUP_QUERY = "warmup query for the retrieval stand"


@dataclass(frozen=True)
class Observation:
    """One retrieval, as the product returned it, with why it went that way."""

    path: str
    result_paths: list[str] = field(default_factory=list)
    trace: dict[str, object] = field(default_factory=dict)
    seconds: float = 0.0
    # Set when the budget ran out and the caller got nothing at all. That is an
    # answer the product really gives, so it is recorded rather than raised.
    error: str | None = None

    @property
    def signals(self) -> list[str]:
        signals = self.trace.get("signals_used", [])
        return list(signals) if isinstance(signals, list) else []

    @property
    def fallback_reason(self) -> str | None:
        reason = self.trace.get("fallback_reason")
        return reason if isinstance(reason, str) else None


def result_path(item: dict) -> str:
    return str(item.get("path") or item.get("relative_path") or "")


def _mcp_rows(query: str, limit: int) -> list[dict]:
    """The agent's path: the MCP tool's own wrapper, with the MCP budget.

    `deadline=None` is not an omission. It is how the wrapper is reached when
    the dispatcher has bound no deadline, and it makes the wrapper compute the
    same `MCP_OPERATION_SECONDS` budget the server gives every operation --
    which keeps that number out of this file, where it would go stale.
    """
    import mcp_server

    return mcp_server._search_vault(query, limit=limit, deadline=None)


def _cli_rows(query: str, limit: int) -> list[dict]:
    """The operator's path: `search_memory.py "<query>"` with its own defaults."""
    from search_memory import search

    return search(query, CLI_SCOPE, limit, **CLI_KWARGS)  # type: ignore[arg-type]


def _api_rows(query: str, limit: int) -> list[dict]:
    """The shape both stands used until 2026-08-26: no budget, no wrapper.

    Kept as the control. An injected defect that moves the MCP number and
    leaves this one standing still is the demonstration that the stand was
    blind -- not that the defect is imaginary.
    """
    from search_memory import search

    return search(query, limit=limit)


_ROWS = {MCP: _mcp_rows, CLI: _cli_rows, API: _api_rows}


def _rows_for(path: str):
    backend = _ROWS.get(path)
    if backend is None:
        raise ValueError(f"unknown retrieval path: {path!r} (known: {PATHS})")
    return backend


def rows(path: str, query: str, limit: int) -> list[dict]:
    """Whatever that entry point returns, untouched."""
    return _rows_for(path)(query, limit)


def _trace(query: str, found: list[dict]) -> dict[str, object]:
    """Why the answer came out that way, read off the rows the product returned."""
    from mcp_server import _retrieval_trace

    return _retrieval_trace(query, found)


def _abandoned(path: str, exceeded: TimeoutError, started: float) -> Observation:
    """The budget ran out and the caller got nothing.

    A stand that crashed here would be reporting its own fragility. The agent
    on this path gets no rows, so this is a miss with a named cause.
    """
    return Observation(
        path=path,
        trace={"fallback_reason": f"{type(exceeded).__name__}: {exceeded}"},
        seconds=round(time.monotonic() - started, 3),
        error=str(exceeded),
    )


def observe(path: str, query: str, limit: int) -> Observation:
    """One measured retrieval through one real entry point."""
    started = time.monotonic()
    try:
        found = rows(path, query, limit)
    except TimeoutError as exceeded:
        return _abandoned(path, exceeded, started)
    return Observation(
        path=path,
        result_paths=[result_path(item) for item in found],
        trace=_trace(query, found),
        seconds=round(time.monotonic() - started, 3),
    )


def warm(path: str, query: str = WARMUP_QUERY) -> float:
    """Load what a resident server already has loaded, before the clock starts.

    The MCP server is resident: it pays the embedding and cross-encoder load
    once, and every later call finds them warm. A stand is a fresh process, so
    without this the first cases pay a cold load against a ten-second budget,
    lose their optional legs to it, and the run-to-run wander that follows is
    the process starting up rather than the product ranking.

    It goes through the unbudgeted shape first, because that is the one that
    loads the models synchronously instead of abandoning them to a straggler
    thread, and then once through the path being measured, because a resident
    server has served requests before the one you time. Neither is measured.
    """
    started = time.monotonic()
    _api_rows(query, 5)
    _warm_measured_path(path, query)
    return round(time.monotonic() - started, 3)


def _warm_measured_path(path: str, query: str) -> None:
    """Running out of budget here is the point: better this call than case one."""
    try:
        rows(path, query, 5)
    except TimeoutError:
        return
