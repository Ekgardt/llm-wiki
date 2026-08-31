"""`mode=community` accepts a symbol anchor at the MCP boundary.

Measured 2026-08-28: naming all 4,078 communities of this repository costs
899,071 estimated tokens against a 25,000 ceiling, so the whole-graph listing
cannot be both complete and bounded. `detect_communities` grew a `symbol`
anchor that answers "which module does X belong to" in 291 tokens, but the
argument contract rejected `symbol` and the tool could not ask. The
verification that matters ran through the boundary, not around it — a mode
absent from the contract is refused there and nowhere else (`4494d8c`).
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import mcp_server  # noqa: E402

REQUIRED, ALLOWED = mcp_server._ARCHITECTURE_CONTRACTS["community"]


def test_the_anchor_is_allowed() -> None:
    assert "symbol" in ALLOWED


def test_the_anchor_stays_optional() -> None:
    """The whole-graph listing is still a legal question."""
    assert "symbol" not in REQUIRED


def test_the_handler_passes_the_anchor_through(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def _fake(directory, *, symbol=None, live=False, with_report=False):
        seen.update(directory=directory, symbol=symbol, live=live)
        return {"communities": []}

    import code_graph

    monkeypatch.setattr(code_graph, "detect_communities", _fake)
    mcp_server._architecture_community(
        {"resolved": Path("/tmp"), "live": False, "symbol": "fuse_rrf"}
    )
    assert seen["symbol"] == "fuse_rrf"


def test_an_unanchored_request_still_works(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def _fake(directory, *, symbol=None, live=False, with_report=False):
        seen.update(symbol=symbol)
        return {"communities": []}

    import code_graph

    monkeypatch.setattr(code_graph, "detect_communities", _fake)
    mcp_server._architecture_community({"resolved": Path("/tmp"), "live": False})
    assert seen["symbol"] is None
