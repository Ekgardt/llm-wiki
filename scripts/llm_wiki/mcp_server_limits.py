"""Operation limits, resource URIs, retrieval tool names and the optional MCP SDK types this server can use."""
from __future__ import annotations

import concurrent.futures
import itertools
import threading
from contextvars import ContextVar
from pathlib import Path
from typing import NamedTuple

# A queue commit locks readers out for milliseconds; wait it out instead of
# reporting a live queue as unreadable.
QUEUE_READ_BUSY_MS = 1_000
# Bounds on what one tool call reads or accepts. The input bounds are published in
# the tools' JSON schemas (`maxLength`, `maxItems`, `maximum`), so a client sees them
# before calling, and a value past one is refused with an error, never cut.
# One cited evidence slice and all of an answer's slices together; past either the
# answer fails by name rather than quoting a cut span. Basis unknown: values predate
# measurement; review if grounded answers are refused for their evidence size.
MAX_MCP_EVIDENCE_BYTES = 64 * 1024
MAX_MCP_TOTAL_EVIDENCE_BYTES = 256 * 1024
# A page slug is one file name: NAME_MAX, 255 bytes on ext4/APFS/NTFS.
MAX_MCP_SLUG_LENGTH = 255
# An error message returned to a client, after secret redaction: a display bound so a
# failing tool never answers with a whole traceback.
MAX_MCP_ERROR_CHARS = 256
# Every MCP tool call's default deadline unless a longer one is declared
# (docs/research/2026-09-25-a-grounded-recall-has-the-time-it-needs-and-no-more.md).
MCP_OPERATION_SECONDS = 10.0
# What a lexical-only pass needs, kept back from the hybrid run: 0.13-0.19 s
# measured on the live vault on 2026-09-25, five times over.
LEXICAL_FALLBACK_RESERVE_SECONDS = 1.0
# A precise architecture call's deadline, which may include starting a language
# server cold. Basis unknown: value predates measurement (2026-07-31); review when a
# cold start is timed on the installed profiles. A settings candidate.
MCP_LSP_STARTUP_SECONDS = 60.0
# CODE-03: indexing a repository builds a whole generation, so its budget is a
# measurement, not a choice. The real second repository on this machine --
# 436 sources, 1,235 chunks -- took
# 118.9 s end to end, most of it embedding. 600 s leaves room for a repository
# several times that size; anything larger is refused by name on the deadline
# rather than half-built, because the build registers only after every artifact
# is written, fsynced and validated.
MCP_REPOSITORY_INDEX_SECONDS = 600.0
# One repository source file read to answer a navigation question (untrusted input);
# a larger file is refused by lsp_security, never cut. Basis unknown: value predates
# measurement; review if a real source file is refused.
MAX_NAVIGATION_SOURCE_BYTES = 16 * 1024 * 1024
# Graph facts one navigation answer may gather (nodes of a symbol, locations, cached
# sources). Answers render at most `code_navigation_renderer.MAX_LIMIT` rows and say
# how many they left out; this bound is a hundred times that. CALLS edges are read
# anchored on the symbol's nodes (`_anchored_call_edges`), so the bound is per symbol.
MAX_NAVIGATION_GRAPH_FACTS = 10_000
# One answer's cache of sources it read: four of the largest files it may read. Past it
# the least recently read source is evicted and read again if asked for, so no span
# drops out of the answer (it used to refuse the next source silently, 2026-09-27).
# The value bounds memory, not correctness; basis unknown beyond that, review if one
# answer re-reads the same file repeatedly.
MAX_NAVIGATION_SOURCE_CACHE_BYTES = 64 * 1024 * 1024
PRECISE_ARCHITECTURE_MODES = frozenset(
    {"definition", "references", "implementations", "type", "diagnostics"}
)
MCP_WORKER_SLOTS = 4
MCP_WORKERS: set[concurrent.futures.Future] = set()
MCP_WORKERS_LOCK = threading.Lock()
_MCP_WORKER_IDS = itertools.count(1)
_OPERATION_DEADLINE: ContextVar[float | None] = ContextVar(
    "mcp_operation_deadline", default=None
)
_OPERATION_CANCELLED: ContextVar[object | None] = ContextVar(
    "mcp_operation_cancelled", default=None
)
_SEARCH_OPERATION_DEADLINE: ContextVar[float | None] = ContextVar(
    "search_operation_deadline", default=None
)
RETRIEVAL_TRACE_SCHEMA = (
    Path(__file__).resolve().parents[1] / "schemas" / "retrieval-trace-v1.json"
)

MCP_AVAILABLE = False
Resource = None
ReadResourceContents = None
CallToolResult = None
TextContent = None
Server = None
stdio_server = None
Tool = None
try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server as stdio_server
    from mcp.types import TextContent as TextContent
    from mcp.types import Tool
    MCP_AVAILABLE = True
except ImportError:
    pass
try:
    from mcp.types import CallToolResult
except ImportError:
    pass
try:
    from mcp.server.lowlevel.helper_types import ReadResourceContents
    from mcp.types import Resource
except ImportError:
    pass

MCP_STRUCTURED_OUTPUT_AVAILABLE = "outputSchema" in getattr(Tool, "model_fields", {})
MCP_CALL_TOOL_RESULT_AVAILABLE = MCP_AVAILABLE and {"content", "structuredContent", "isError"}.issubset(
    getattr(CallToolResult, "model_fields", {})
)
MCP_RESOURCES_AVAILABLE = all(
    (
        hasattr(Server, "list_resources"),
        hasattr(Server, "read_resource"),
        Resource is not None,
        ReadResourceContents is not None,
    )
)

HEALTH_RESOURCE_URI = "llm-wiki://health"
CONTEXT_RESOURCE_URI = "llm-wiki://context"


class SearchCaller(NamedTuple):
    """Which tool is asking, and whether the search logs its own impressions."""

    source_tool: str
    emit_telemetry: bool


RECALL_CALLER = SearchCaller("mcp.recall", True)
# The tools that answer from the corpus through `search_vault`: each reports its
# trace, its quality from that trace, and its generation's freshness.
RETRIEVAL_TOOLS = frozenset({"recall", "get_decisions"})
# `get_decisions` records its own decision-filter impressions, one per page.
DECISIONS_CALLER = SearchCaller("mcp.get_decisions", False)
