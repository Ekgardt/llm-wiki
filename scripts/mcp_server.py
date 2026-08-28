"""LLM-Wiki MCP Server — 12 task-shaped tools, stdio transport, 100% local.

Gives AI agents (Claude Code, OpenCode, Codex) structured
access to the knowledge vault via Model Context Protocol. No server, no cloud,
no network — stdio subprocess on the same machine.

Install: uv sync --extra mcp-server
Run:    uv run --locked --no-sync python scripts/mcp_server.py

Agent config (e.g. for Claude Code ~/.claude/.mcp.json):
{
  "mcpServers": {
    "llm-wiki": {
      "command": "uv",
      "args": ["run", "--locked", "--no-sync", "--directory", "/path/to/llm-wiki", "python", "scripts/mcp_server.py"]
    }
  }
}

Tools (task-shaped, not entity-shaped — see repowise design):
  recall(query, limit)       — hybrid search (BM25 + vector + graph + reranker)
  read_page(slug)            — full page content (the only raw-bytes tool)
  wiki_overview()            — vault stats, page count, retrieval tier
  get_context(slugs, include) — batch page context and compatibility previews
  get_decisions(query)       — active architectural decisions
  vault_status()             — metacognitive block (gaps, backlog, stale)
  log_decision(summary)      — append a decision to daily log
  check_contradiction(claim) — find conflicting pages
  compile(scope)             — compile pending logs within the operation deadline
  find_dead_code(directory)  — conservative zero-confirmed-caller candidates
  get_architecture(directory) — entry points, routes, hotspots, communities
  doctor(repair)            — local health and optional safe repairs
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import contextvars
import datetime as dt
import hashlib
import inspect
import itertools
import os
import re
import sys
import threading
import time
from contextvars import ContextVar
from dataclasses import asdict
from pathlib import Path, PureWindowsPath

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bounded_io import read_stable_bytes  # noqa: E402

# A queue commit locks readers out for milliseconds; wait it out instead of
# reporting a live queue as unreadable.
QUEUE_READ_BUSY_MS = 1_000
MAX_MCP_PAGE_BYTES = 4 * 1024 * 1024
MAX_MCP_EVIDENCE_BYTES = 64 * 1024
MAX_MCP_TOTAL_EVIDENCE_BYTES = 256 * 1024
MAX_MCP_QUERY_LENGTH = 8_192
MAX_MCP_SLUG_LENGTH = 255
MAX_MCP_CONTEXT_SLUGS = 20
MAX_MCP_CONTEXT_INCLUDE = 10
MAX_MCP_INCLUDE_LENGTH = 64
MAX_MCP_CONTEXT_TOKENS = 32_768
MAX_MCP_ERROR_CHARS = 256
MCP_OPERATION_SECONDS = 10.0
MCP_LSP_STARTUP_SECONDS = 60.0
MAX_NAVIGATION_SOURCE_BYTES = 16 * 1024 * 1024
MAX_NAVIGATION_GRAPH_FACTS = 10_000
MAX_NAVIGATION_SOURCE_CACHE_BYTES = 64 * 1024 * 1024
PRECISE_ARCHITECTURE_MODES = frozenset(
    {"definition", "references", "implementations", "type", "diagnostics"}
)
MCP_WORKER_SLOTS = 4
_MCP_WORKERS: set[concurrent.futures.Future] = set()
_MCP_WORKERS_LOCK = threading.Lock()
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
    Path(__file__).resolve().parent / "schemas" / "retrieval-trace-v1.json"
)

MCP_AVAILABLE = False
MCP_RESOURCES_AVAILABLE = False
MCP_STRUCTURED_OUTPUT_AVAILABLE = False
MCP_CALL_TOOL_RESULT_AVAILABLE = False
Resource = None
TextResourceContents = None
CallToolResult = None
TextContent = None
try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool
    MCP_AVAILABLE = True
    MCP_STRUCTURED_OUTPUT_AVAILABLE = "outputSchema" in getattr(Tool, "model_fields", {})
except ImportError:
    pass

if MCP_AVAILABLE:
    try:
        from mcp.types import CallToolResult

        call_result_fields = getattr(CallToolResult, "model_fields", {})
        MCP_CALL_TOOL_RESULT_AVAILABLE = {
            "content",
            "structuredContent",
            "isError",
        }.issubset(call_result_fields)
    except ImportError:
        pass

if MCP_AVAILABLE:
    try:
        from mcp.types import Resource, TextResourceContents

        MCP_RESOURCES_AVAILABLE = all(
            (
                hasattr(Server, "list_resources"),
                hasattr(Server, "read_resource"),
                Resource is not None,
                TextResourceContents is not None,
            )
        )
    except ImportError:
        pass

from answer_budget import MAX_BUDGET_TOKENS as ANSWER_BUDGET_MAX_TOKENS  # noqa: E402
from answer_budget import MIN_BUDGET_TOKENS as ANSWER_BUDGET_MIN_TOKENS  # noqa: E402
from answer_budget import shape_code_answer  # noqa: E402
from answer_cost import attach_answer_cost  # noqa: E402
from mcp_contract import build_envelope, envelope_schema  # noqa: E402
from retrieval import PROFILES as QA_PROFILES  # noqa: E402
from secret_redact import redact_secrets  # noqa: E402

HEALTH_RESOURCE_URI = "llm-wiki://health"
CONTEXT_RESOURCE_URI = "llm-wiki://context"


def _operation_deadline(deadline: float | None = None) -> float:
    if deadline is not None:
        return deadline
    inherited = _OPERATION_DEADLINE.get()
    return inherited if inherited is not None else time.monotonic() + MCP_OPERATION_SECONDS


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("MCP operation deadline reached")


def _positioned_architecture_call(arguments: dict, mode: str) -> bool:
    if mode not in {"callers", "callees"}:
        return False
    return all(key in arguments for key in ("path", "line", "character"))


def _tool_operation_seconds(name: str, arguments: object) -> float:
    if name != "get_architecture" or not isinstance(arguments, dict):
        return MCP_OPERATION_SECONDS
    mode = arguments.get("mode", "summary")
    if mode in PRECISE_ARCHITECTURE_MODES or _positioned_architecture_call(
        arguments, mode
    ):
        return MCP_LSP_STARTUP_SECONDS
    return MCP_OPERATION_SECONDS


def _operation_cancelled():
    cancelled = _OPERATION_CANCELLED.get()
    return cancelled if callable(cancelled) else lambda: False


def _call_with_deadline(function, *args, deadline: float, **kwargs):
    """Pass deadlines to helpers that explicitly support them."""
    try:
        accepts_deadline = "deadline" in inspect.signature(function).parameters
    except (TypeError, ValueError):
        accepts_deadline = False
    if accepts_deadline:
        kwargs["deadline"] = deadline
    return function(*args, **kwargs)


def _cancellation_context():
    """Copy the caller context with the abandonment flag already bound in it."""
    abandoned = threading.Event()
    cancellation_token = _OPERATION_CANCELLED.set(abandoned.is_set)
    try:
        return abandoned, contextvars.copy_context()
    finally:
        _OPERATION_CANCELLED.reset(cancellation_token)


def _reserve_mcp_worker(submitted) -> None:
    with _MCP_WORKERS_LOCK:
        _MCP_WORKERS.difference_update(
            future for future in _MCP_WORKERS if future.done()
        )
        if len(_MCP_WORKERS) >= MCP_WORKER_SLOTS:
            raise TimeoutError("MCP worker capacity exhausted")
        _MCP_WORKERS.add(submitted)


def _completed_worker_error(completed):
    try:
        return completed.exception()
    except concurrent.futures.CancelledError:
        return None


def _discard_mcp_worker(completed, abandoned) -> None:
    error = _completed_worker_error(completed)
    if error is not None and abandoned.is_set():
        print(
            f"mcp worker failed after caller timeout: {type(error).__name__}",
            file=sys.stderr,
        )
    with _MCP_WORKERS_LOCK:
        _MCP_WORKERS.discard(completed)


def _worker_discarder(abandoned):
    def discard(completed):
        _discard_mcp_worker(completed, abandoned)

    return discard


def _run_submitted(submitted, context, function, args) -> None:
    if not submitted.set_running_or_notify_cancel():
        return
    try:
        result = context.run(function, *args)
    except BaseException as error:
        submitted.set_exception(error)
    else:
        submitted.set_result(result)


def _start_worker_thread(submitted, context, function, args) -> None:
    thread = threading.Thread(
        target=_run_submitted,
        args=(submitted, context, function, args),
        name=f"llm-wiki-mcp-{next(_MCP_WORKER_IDS)}",
        daemon=True,
    )
    try:
        thread.start()
    except BaseException:
        submitted.cancel()
        raise


# ``code_graph``'s live extraction takes no deadline and cannot be interrupted
# from outside, so an abandoned run keeps its worker until it finishes on its
# own. The slot cap keeps repeated timeouts from stacking runaway workers.
CODE_GRAPH_WORK_SLOTS = 2

_CODE_GRAPH_SLOTS = threading.Semaphore(CODE_GRAPH_WORK_SLOTS)


def _run_code_graph_work(function, args, kwargs, outcome) -> None:
    try:
        outcome.set_result(function(*args, **kwargs))
    except BaseException as error:  # noqa: BLE001 - delivered to the waiting caller
        outcome.set_exception(error)
    finally:
        _CODE_GRAPH_SLOTS.release()


def _started_code_graph_worker(function, args, kwargs):
    """Start abandonable code-graph work on its own bounded daemon worker."""
    if not _CODE_GRAPH_SLOTS.acquire(blocking=False):
        raise TimeoutError(
            "code graph workers are still busy with previously abandoned work"
        )
    outcome = concurrent.futures.Future()
    thread = threading.Thread(
        target=_run_code_graph_work,
        args=(function, args, kwargs, outcome),
        name="llm-wiki-code-graph",
        daemon=True,
    )
    try:
        thread.start()
    except BaseException:
        _CODE_GRAPH_SLOTS.release()
        raise
    return outcome


def _bounded_code_graph_call(function, *args, deadline: float, **kwargs):
    """Bound non-cooperative code-graph work by abandoning it at the deadline."""
    _check_deadline(deadline)
    outcome = _started_code_graph_worker(function, args, kwargs)
    try:
        return outcome.result(timeout=max(0.0, deadline - time.monotonic()))
    except concurrent.futures.TimeoutError:
        raise TimeoutError(
            "code graph analysis exceeded the operation deadline"
        ) from None


def _code_graph_timeout_data(
    directory: object, error: BaseException, *, completed: tuple[str, ...]
) -> dict:
    """A bounded, named result for a code-graph budget that ran out."""
    return {
        "directory": directory if isinstance(directory, str) else None,
        "status": "timeout",
        "warning": "code_graph_timeout",
        "detail": str(error),
        "completed": list(completed),
        "skipped": ["code_graph_analysis"],
    }


async def _run_bounded(function, *args, deadline: float):
    """Run synchronous work off-loop without an unbounded submission queue."""
    _check_deadline(deadline)
    submitted = concurrent.futures.Future()
    abandoned, context = _cancellation_context()
    _reserve_mcp_worker(submitted)
    submitted.add_done_callback(_worker_discarder(abandoned))
    _start_worker_thread(submitted, context, function, args)
    future = asyncio.wrap_future(submitted)
    try:
        done, _pending = await asyncio.wait(
            {future}, timeout=max(0.0, deadline - time.monotonic())
        )
    except asyncio.CancelledError:
        abandoned.set()
        future.cancel()
        raise
    if not done:
        abandoned.set()
        future.cancel()
        raise TimeoutError("MCP operation deadline reached")
    return future.result()


def _timeout_envelope_text() -> str:
    import json

    error = "operation_timeout"
    envelope = {
        "schema_version": "1.0",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "index_timestamp": None,
        "source_commit": None,
        "freshness": "unknown",
        "coverage": 0.0,
        "confidence": 0.2,
        "fallback": False,
        "partial": True,
        "warnings": [error],
        "components": {},
        "data": {"error": error},
    }
    return json.dumps(envelope, indent=2, ensure_ascii=False, allow_nan=False)


def _navigation_failure_from_arguments(
    arguments: object,
    *,
    status: str,
    warning: str,
) -> dict | None:
    if not isinstance(arguments, dict) or not _is_precise_architecture_request(
        arguments
    ):
        return None
    mode = arguments.get("mode")
    if not isinstance(mode, str):
        return None
    directory = arguments.get("directory")
    return _normalized_navigation_failure(
        directory=directory if isinstance(directory, str) else None,
        mode=mode,
        status=status,
        warning=warning,
        offset=arguments.get("offset", 0),
        limit=arguments.get("limit", 10),
    )


def _tool_timeout_envelope_text(name: str, arguments: object) -> str:
    import json

    data = (
        _navigation_failure_from_arguments(
            arguments,
            status="timeout",
            warning="navigation_timeout",
        )
        if name == "get_architecture"
        else None
    )
    if data is None:
        return _timeout_envelope_text()
    data["directory"] = None
    warning = "navigation_timeout"
    envelope = {
        "schema_version": "1.0",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "index_timestamp": None,
        "source_commit": None,
        "freshness": "unknown",
        "coverage": 0.0,
        "confidence": 0.2,
        "fallback": False,
        "partial": True,
        "warnings": [warning],
        "components": {},
        "data": data,
    }
    return json.dumps(envelope, indent=2, ensure_ascii=False, allow_nan=False)


def _doctor_branch(
    action: str,
    *,
    target: bool = False,
    limit: bool = False,
    mutation: bool = False,
) -> dict:
    properties = {"action": {"type": "string", "const": action}}
    required = ["action"]
    optional = (
        (target, "target_id", {"type": "string", "minLength": 1, "maxLength": 128}, True),
        (limit, "limit", {"type": "integer", "minimum": 1, "maximum": 100}, False),
        (mutation, "repair", {"type": "boolean", "const": True}, True),
    )
    for wanted, key, field, mandatory in optional:
        if wanted:
            properties[key] = field
            required.extend([key] if mandatory else [])
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


DOCTOR_INPUT_SCHEMA = {
    "type": "object",
    "oneOf": [
        _doctor_branch("status", limit=True),
        _doctor_branch("queue-inspect", target=True),
        _doctor_branch("queue-cancel", target=True, mutation=True),
        _doctor_branch("queue-redrive", target=True, mutation=True),
        _doctor_branch("queue-dead-list", limit=True),
        _doctor_branch("transaction-recover", limit=True, mutation=True),
        _doctor_branch("transaction-undo", target=True, mutation=True),
        _doctor_branch("archive-status", limit=True),
        _doctor_branch("claim-status", limit=True),
    ],
}

# CODE-06: the two arguments that let a caller pay less for a code answer.
# Bounds come from `answer_budget`; 25 000 is the client-side tool-result
# ceiling Anthropic documents for Claude Code, and the low bound sits below any
# real answer frame so the named refusal stays reachable and testable.
ANSWER_BUDGET_SCHEMA_FIELDS = {
    "budget_tokens": {
        "type": "integer",
        "minimum": ANSWER_BUDGET_MIN_TOKENS,
        "maximum": ANSWER_BUDGET_MAX_TOKENS,
        "description": (
            "Approximate token ceiling for this answer (len/4, not a "
            "tokenizer). Drops opaque identifiers, then derivable fields, "
            "then rows from the tail, and names everything it dropped; a "
            "budget too small for the answer frame is refused by name, never "
            "silently shortened"
        ),
    },
    "include_node_ids": {
        "type": "boolean",
        "default": False,
        "description": (
            "Emit the opaque code:node:<hash> graph identifiers. Off by "
            "default: no surface in this product accepts one as input"
        ),
    },
}

TOOL_INPUT_SCHEMAS = {
    "recall": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "maxLength": MAX_MCP_QUERY_LENGTH,
                "description": "Search query",
            },
            "limit": {
                "type": "integer",
                "default": 8,
                "description": "Requested result count; execution clamps it to 1-20",
            },
            "grounded": {
                "type": "boolean",
                "default": False,
                "description": "Return a verified evidence-grounded answer",
            },
            "profile": {
                "type": "string",
                "enum": list(QA_PROFILES),
                "description": "Grounded QA retrieval profile",
            },
        },
        "required": ["query"],
        "allOf": [
            {
                "if": {"required": ["profile"]},
                "then": {
                    "properties": {"grounded": {"const": True}},
                    "required": ["grounded"],
                },
            }
        ],
    },
    "read_page": {
        "type": "object",
        "properties": {
            "slug": {
                "type": "string",
                "maxLength": MAX_MCP_SLUG_LENGTH,
                "description": "Page slug (e.g. 'auth-decision')",
            },
        },
        "required": ["slug"],
    },
    "wiki_overview": {"type": "object", "properties": {}, "required": []},
    "vault_status": {"type": "object", "properties": {}, "required": []},
    "get_decisions": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "maxLength": MAX_MCP_QUERY_LENGTH,
                "description": "Optional filter query",
            },
            "limit": {
                "type": "integer",
                "default": 10,
                "description": "Requested result count; execution clamps it to 1-20",
            },
        },
        "required": [],
    },
    "get_context": {
        "type": "object",
        "properties": {
            "slugs": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_MCP_CONTEXT_SLUGS,
                "uniqueItems": True,
                "items": {"type": "string", "maxLength": MAX_MCP_SLUG_LENGTH},
                "description": "List of page slugs",
            },
            "include": {
                "type": "array",
                "maxItems": MAX_MCP_CONTEXT_INCLUDE,
                "uniqueItems": True,
                "items": {"type": "string", "maxLength": MAX_MCP_INCLUDE_LENGTH},
                "description": "Optional strings; 'frontmatter' adds content_preview for backward compatibility",
            },
            "token_budget": {
                "type": "integer",
                "minimum": 256,
                "maximum": MAX_MCP_CONTEXT_TOKENS,
                "default": 8192,
            },
        },
        "required": ["slugs"],
    },
    "check_contradiction": {
        "type": "object",
        "properties": {
            "claim": {"type": "string", "description": "The claim to check"},
        },
        "required": ["claim"],
    },
    "log_decision": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "Decision summary"},
            "rationale": {
                "type": "string",
                "description": "Why this decision was made",
            },
        },
        "required": ["summary"],
    },
    "compile": {"type": "object", "properties": {}, "required": []},
    "find_dead_code": {
        "type": "object",
        "properties": {
            "directory": {
                "type": "string",
                "description": "Project directory to analyze",
            },
            "live": {
                "type": "boolean",
                "default": False,
                "description": "Bypass the active generation and run live extraction",
            },
            **ANSWER_BUDGET_SCHEMA_FIELDS,
        },
        "required": ["directory"],
    },
    "get_architecture": {
        "type": "object",
        "properties": {
            "directory": {
                "type": "string",
                "description": "Project directory to analyze",
            },
            "mode": {
                "type": "string",
                "maxLength": 32,
                "enum": [
                    "summary",
                    "symbol",
                    "callers",
                    "callees",
                    "dependencies",
                    "path",
                    "community",
                    "impact",
                    "definition",
                    "references",
                    "implementations",
                    "type",
                    "diagnostics",
                    "query",
                    "provenance",
                    "snippet",
                    "coverage",
                ],
                "description": "Bounded architecture query mode",
            },
            "symbol": {
                "type": "string",
                "minLength": 1,
                "maxLength": 1024,
                "description": "Symbol for symbol, callers, callees, dependencies, path, provenance, and snippet modes",
            },
            "query": {
                "type": "string",
                "minLength": 2,
                "maxLength": 4096,
                "description": "Bounded JSON hop pipeline for mode=query",
            },
            "reverse": {"type": "boolean", "default": False},
            "comparison": {
                "type": "string",
                "maxLength": 32,
                "description": "Impact endpoint comparison: dirty, worktree-index, index-HEAD, two-commits, or merge-base-branch",
            },
            "base": {"type": "string", "maxLength": 1024},
            "target": {"type": "string", "minLength": 1, "maxLength": 1024},
            "branch": {"type": "string", "maxLength": 1024},
            "live": {
                "type": "boolean",
                "default": False,
                "description": "Bypass the active generation and run live extraction",
            },
            "path": {
                "type": "string",
                "minLength": 1,
                "maxLength": 4096,
                "description": "Repository-relative path for coverage mode and for precise or positioned calls",
            },
            "line": {"type": "integer", "minimum": 1},
            "character": {"type": "integer", "minimum": 0},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10},
            **ANSWER_BUDGET_SCHEMA_FIELDS,
        },
        "required": ["directory"],
    },
    "doctor": DOCTOR_INPUT_SCHEMA,
}


def _search_vault(
    query: str, limit: int = 8, *, deadline: float | None = None
) -> list[dict]:
    """Run hybrid search on the vault."""
    if not isinstance(query, str) or len(query) > MAX_MCP_QUERY_LENGTH:
        raise ValueError("query exceeds the MCP retrieval bound")
    operation_deadline = _search_deadline(deadline)
    try:
        return _run_vault_search(query, limit, operation_deadline, semantic=True)
    except TimeoutError:
        lexical = _run_vault_search(
            query, limit, operation_deadline, semantic=False, graph=False, rerank=False
        )
        return [_lexical_fallback_row(row) for row in lexical]


def _search_deadline(deadline: float | None) -> float:
    operation_deadline = (
        _SEARCH_OPERATION_DEADLINE.get() if deadline is None else deadline
    )
    if operation_deadline is None:
        return time.monotonic() + MCP_OPERATION_SECONDS
    return operation_deadline


def _run_vault_search(
    query: str,
    limit: int,
    operation_deadline: float,
    *,
    semantic: bool,
    graph: bool = True,
    rerank: bool = True,
) -> list[dict]:
    from search_memory import search

    # No `max_candidates`: that is a per-backend resource cap, not the answer
    # size. Passing the answer size collapsed each leg's pool to the number of
    # rows asked for, and one page owns many chunks -- measured on the live
    # vault, five rows for the quarantine-retry question were three pages, the
    # decision page absent; without the cap it came first.
    return search(
        query,
        limit=limit,
        semantic=semantic,
        graph=graph,
        rerank=rerank,
        source_tool="mcp.recall",
        deadline_monotonic=operation_deadline,
    )


def _lexical_fallback_row(row: dict) -> dict:
    return {
        **row,
        "requested_mode": "HYBRID",
        "fallback_reason": "retrieval_deadline_exceeded",
        "partial": True,
    }


def _retrieval_trace(query: str, results: list[dict]) -> dict[str, object]:
    """Recover the planner trace from compatibility rows and validate it closed."""
    if results and all(
        key in results[0]
        for key in ("requested_mode", "effective_mode", "signals_used", "generation")
    ):
        first = results[0]
        trace: dict[str, object] = {
            "schema_version": "retrieval-trace/v1",
            "requested_mode": first.get("requested_mode"),
            "effective_mode": first.get("effective_mode"),
            "signals_used": first.get("signals_used", []),
            "fallback_reason": first.get("fallback_reason"),
            "corpus_generation": first.get("generation", "legacy"),
            "partial": bool(first.get("partial", False)),
            "reranker_applied": bool(first.get("reranker_applied", False)),
            "reranker_model_id": first.get("reranker_model_id"),
            "reranker_model_revision": first.get("reranker_model_revision"),
            "reranker_depth": first.get("reranker_depth"),
            "reranker_duration_ms": first.get("reranker_duration_ms"),
            "reranker_fallback_reason": first.get("reranker_fallback_reason"),
        }
    else:
        from retrieval import analyze_query

        requested = analyze_query(query).recommended_profile
        trace = {
            "schema_version": "retrieval-trace/v1",
            "requested_mode": requested,
            "effective_mode": "BASE",
            "signals_used": [],
            "fallback_reason": "trace_unavailable",
            "corpus_generation": "legacy",
            "partial": True,
            "reranker_applied": False,
            "reranker_model_id": None,
            "reranker_model_revision": None,
            "reranker_depth": None,
            "reranker_duration_ms": None,
            "reranker_fallback_reason": None,
        }
    from reliable_memory import validate_schema

    validate_schema(trace, RETRIEVAL_TRACE_SCHEMA)
    return trace


def _read_page(
    slug: str,
    *,
    emit_telemetry: bool = True,
    resolve_evidence: bool = True,
    deadline: float | None = None,
) -> dict:
    """Read a full page by slug."""
    from memory_state import ROOT, STATE_ROOT

    page_path = _readable_page_path(ROOT, slug, deadline)
    if isinstance(page_path, dict):
        return page_path
    content = _page_content(page_path)
    if isinstance(content, dict):
        return content
    return _page_with_evidence(
        ROOT,
        STATE_ROOT,
        slug,
        page_path,
        content,
        emit_telemetry=emit_telemetry,
        resolve_evidence=resolve_evidence,
        deadline=deadline,
    )


def _page_with_evidence(
    root,
    state_root,
    slug: str,
    page_path,
    content: str,
    *,
    emit_telemetry: bool,
    resolve_evidence: bool,
    deadline: float | None,
) -> dict:
    """The page and its resolved evidence, or the evidence error envelope."""
    evidence = _page_evidence(root, state_root, content, resolve_evidence, deadline)
    if isinstance(evidence, dict):
        return evidence
    if emit_telemetry:
        _record_page_reads(slug, evidence)
    return {
        "slug": slug,
        "path": str(page_path.relative_to(root)),
        "content": content,
        "evidence": evidence,
    }


def _readable_page_path(root: Path, slug, deadline):
    """Return the page path, or the error dict the caller must hand back."""
    invalid = _invalid_slug_error(slug)
    if invalid is not None:
        return invalid
    page_path = root / "knowledge" / "notes" / f"{slug}.md"
    _check_deadline(deadline)
    if not page_path.exists():
        return {"error": f"Page not found: {slug}"}
    return page_path


def _slug_is_pathlike(slug: str) -> bool:
    if slug in {".", ".."}:
        return True
    if "/" in slug or "\\" in slug:
        return True
    return bool(PureWindowsPath(slug).drive) or Path(slug).is_absolute()


def _invalid_slug_error(slug) -> dict | None:
    if not isinstance(slug, str) or len(slug) > MAX_MCP_SLUG_LENGTH:
        return {"error": "Invalid page slug"}
    if _slug_is_pathlike(slug):
        return {"error": f"Invalid page slug: {slug}"}
    return None


def _page_content(page_path: Path):
    """Return the decoded page, or the error dict the caller must hand back."""
    try:
        return read_stable_bytes(
            page_path, MAX_MCP_PAGE_BYTES, label="MCP page"
        ).decode("utf-8", errors="strict")
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return {"error": f"Page read failed: {_safe_page_read_error(exc)}"}


def _require_evidence_within_bounds(resolved, evidence_bytes: int, error_class) -> int:
    if len(resolved.bytes) > MAX_MCP_EVIDENCE_BYTES:
        raise error_class(f"evidence slice exceeds {MAX_MCP_EVIDENCE_BYTES} bytes")
    if evidence_bytes + len(resolved.bytes) > MAX_MCP_TOTAL_EVIDENCE_BYTES:
        raise error_class(
            f"total evidence exceeds {MAX_MCP_TOTAL_EVIDENCE_BYTES} bytes"
        )
    return len(resolved.bytes)


def _resolved_evidence(resolver, references, deadline, error_class) -> list:
    evidence = []
    evidence_bytes = 0
    for reference in references:
        _check_deadline(deadline)
        resolved = resolver.resolve(reference)
        evidence_bytes += _require_evidence_within_bounds(
            resolved, evidence_bytes, error_class
        )
        evidence.append(
            {
                "reference": str(reference),
                "sha256": resolved.sha256,
                "text": resolved.bytes.decode("utf-8", errors="strict"),
            }
        )
    return evidence


def _page_evidence(root, state_root, content: str, resolve_evidence: bool, deadline):
    """Return resolved evidence, or the error dict the caller must hand back."""
    from evidence_resolver import (
        EvidenceResolutionError,
        EvidenceResolver,
        extract_evidence_references,
    )

    resolver = EvidenceResolver(root, state_root=state_root)
    try:
        references = extract_evidence_references(content) if resolve_evidence else []
        return _resolved_evidence(
            resolver, references, deadline, EvidenceResolutionError
        )
    except (EvidenceResolutionError, OSError, UnicodeDecodeError, ValueError) as exc:
        return {"error": f"Evidence resolution failed: {_safe_evidence_error(exc)}"}


def _page_read_events(make_event, kinds) -> list:
    events = []
    for kind, candidate_id in kinds:
        event = make_event(
            event_kind=kind,
            query=None,
            retrieval_mode="direct",
            candidate_id=candidate_id,
            rank=None,
            generation="legacy",
            source_tool="mcp.read_page",
        )
        if event is not None:
            events.append(event)
    return events


def _record_page_reads(slug: str, evidence: list) -> None:
    try:
        from retrieval_telemetry import (
            best_effort_make_event,
            best_effort_record_events,
        )

        kinds = [
            ("page_read", slug),
            *(("evidence_read", item["sha256"]) for item in evidence),
        ]
        events = _page_read_events(best_effort_make_event, kinds)
        if events:
            best_effort_record_events(events)
    except Exception:
        pass


def _wiki_overview(*, deadline: float | None = None) -> dict:
    """Get vault statistics and retrieval tier recommendation."""
    from lookup_mode import count_wiki_pages, tier_for
    from memory_state import ROOT

    _check_deadline(deadline)
    count = count_wiki_pages()
    _check_deadline(deadline)
    tier = tier_for(count)
    return {
        "page_count": count,
        "retrieval_tier": tier,
        "vault_root": str(ROOT),
    }


def _vault_status(*, deadline: float | None = None) -> dict:
    """Return compile status and current daily-file backlog."""
    from memory_state import ROOT, file_hash, load_state

    _check_deadline(deadline)
    state = load_state()
    compiled = state.get("compiled_daily_hashes", {}) or {}
    return {
        "last_compile": state.get("last_compile_at", "never"),
        "last_compile_status": state.get("last_compile_status", "unknown"),
        "compile_backlog": _compile_backlog(ROOT, file_hash, compiled, deadline),
    }


def _daily_files(root: Path) -> list[Path]:
    try:
        return list((root / "knowledge" / "daily").glob("*.md"))
    except OSError:
        return []


def _daily_needs_compile(daily_path: Path, file_hash, compiled: dict) -> bool:
    try:
        current_hash = file_hash(daily_path)
    except OSError:
        return True
    return compiled.get(daily_path.name) != current_hash


def _compile_backlog(root: Path, file_hash, compiled: dict, deadline) -> int:
    backlog = 0
    for daily_path in _daily_files(root):
        _check_deadline(deadline)
        if _daily_needs_compile(daily_path, file_hash, compiled):
            backlog += 1
    return backlog


def _get_decisions(
    query: str | None = None, limit: int = 10, *, deadline: float | None = None
) -> list[dict]:
    """Get active decisions from the vault."""
    from search_memory import search

    _require_decision_query(query)
    effective_query = query or "decision"
    candidates = search(
        effective_query,
        limit=limit,
        source_tool="mcp.get_decisions",
        emit_telemetry=False,
        deadline_monotonic=deadline,
    )
    results = [result for result in candidates if _is_decision_result(result)]
    _record_decision_impressions(effective_query, results)
    return results


def _require_decision_query(query) -> None:
    if query is None:
        return
    if not isinstance(query, str) or len(query) > MAX_MCP_QUERY_LENGTH:
        raise ValueError("query exceeds the MCP retrieval bound")


def _is_decision_result(result: dict) -> bool:
    if result.get("type") == "decision":
        return True
    return "decision" in result.get("path", "").lower()


def _decision_impression_events(make_event, effective_query: str, results: list) -> list:
    events = []
    for rank, result in enumerate(results, start=1):
        event = make_event(
            event_kind="impression",
            query=effective_query,
            retrieval_mode="decision-filter",
            candidate_id=result.get("slug") or Path(result.get("path", "")).stem,
            rank=rank,
            generation="legacy",
            source_tool="mcp.get_decisions",
        )
        if event is not None:
            events.append(event)
    return events


def _record_decision_impressions(effective_query: str, results: list) -> None:
    if not results:
        return
    try:
        from retrieval_telemetry import (
            best_effort_make_event,
            best_effort_record_events,
        )

        events = _decision_impression_events(
            best_effort_make_event, effective_query, results
        )
        if events:
            best_effort_record_events(events)
    except Exception:
        pass


def _get_context(
    slugs: list[str],
    include: list[str] | None = None,
    *,
    token_budget: int = 8192,
    deadline: float | None = None,
) -> dict:
    """Return one compiler package under one shared token budget."""
    from corpus_snapshot import collect_corpus
    from memory_state import ROOT

    slugs, include = _validated_context_request(slugs, include, token_budget)
    operation_deadline = _operation_deadline(deadline)
    snapshot = collect_corpus(ROOT, deadline=operation_deadline)
    selection = _context_selection(snapshot, set(slugs))
    compiled = _compiled_context(
        snapshot, selection, token_budget, operation_deadline
    )
    _record_context_injections(selection["selected_paths"])
    return _context_result(compiled, snapshot, selection, token_budget, include)


def _slug_exceeds_bound(slug) -> bool:
    return not isinstance(slug, str) or len(slug) > MAX_MCP_SLUG_LENGTH


def _include_exceeds_bound(item) -> bool:
    return not isinstance(item, str) or len(item) > MAX_MCP_INCLUDE_LENGTH


def _require_context_slugs(slugs) -> None:
    if not isinstance(slugs, list) or not 1 <= len(slugs) <= MAX_MCP_CONTEXT_SLUGS:
        raise ValueError("slugs exceed the MCP context bound")
    if any(_slug_exceeds_bound(slug) for slug in slugs):
        raise ValueError("slug exceeds the MCP context bound")


def _require_context_include(include) -> None:
    if not isinstance(include, list) or len(include) > MAX_MCP_CONTEXT_INCLUDE:
        raise ValueError("include exceeds the MCP context bound")
    if any(_include_exceeds_bound(item) for item in include):
        raise ValueError("include item exceeds the MCP context bound")


def _require_context_token_budget(token_budget) -> None:
    if isinstance(token_budget, bool) or not isinstance(token_budget, int):
        raise ValueError("token_budget exceeds the MCP context bound")
    if not 256 <= token_budget <= MAX_MCP_CONTEXT_TOKENS:
        raise ValueError("token_budget exceeds the MCP context bound")


def _validated_context_request(slugs, include, token_budget):
    _require_context_slugs(slugs)
    include = include or []
    _require_context_include(include)
    _require_context_token_budget(token_budget)
    return list(dict.fromkeys(slugs)), list(dict.fromkeys(include))


def _source_identities(source) -> set:
    return {
        Path(source.record.relative_path).stem,
        source.record.logical_id,
        source.record.relative_path,
    }


def _source_matches(source, requested: set) -> bool:
    return bool(_source_identities(source) & requested)


def _selected_sources(snapshot, requested: set) -> tuple:
    return tuple(
        source for source in snapshot.sources if _source_matches(source, requested)
    )


def _selected_chunks(snapshot, selected_paths: set) -> tuple:
    return tuple(
        chunk for chunk in snapshot.chunks if chunk.parent_page in selected_paths
    )


def _missing_context_slugs(sources, requested: set) -> list:
    found = set()
    for source in sources:
        found |= _source_identities(source) & requested
    return sorted(requested - found)


def _context_selection(snapshot, requested: set) -> dict:
    sources = _selected_sources(snapshot, requested)
    selected_paths = {source.record.relative_path for source in sources}
    return {
        "sources": sources,
        "selected_paths": selected_paths,
        "missing": _missing_context_slugs(sources, requested),
        "chunks": _selected_chunks(snapshot, selected_paths),
    }


def _compiled_context(snapshot, selection: dict, token_budget: int, deadline):
    from context_budget import BudgetExceededError, ContextBudget
    from context_compiler import compile_context
    from corpus_snapshot import CorpusSnapshot

    narrow = CorpusSnapshot(
        selection["sources"],
        selection["chunks"],
        snapshot.corpus_sha256,
        snapshot.policy,
        snapshot.collector_version,
        snapshot.extractor_version,
    )
    budget = ContextBudget(None, token_budget, 0, 0)
    shortlist = tuple(source.record.logical_id for source in selection["sources"])
    try:
        return compile_context(
            narrow,
            shortlist=shortlist,
            evidence_chunk_ids=(chunk.id for chunk in selection["chunks"]),
            budget=budget,
            deadline=deadline,
        )
    except BudgetExceededError:
        return compile_context(
            narrow,
            shortlist=shortlist,
            evidence_chunk_ids=(),
            budget=budget,
            deadline=deadline,
        )


def _page_items(items: list) -> list:
    return [item for item in items if item["source"].endswith(".md")]


def _symbol_items(items: list) -> list:
    return [item for item in items if not item["source"].endswith(".md")]


def _items_of_type(items: list, types: set) -> list:
    return [item for item in items if item["type"] in types]


def _items_of_representation(items: list, representation: str) -> list:
    return [item for item in items if item["representation"] == representation]


def _materialization_trace(compiled) -> list:
    return [asdict(item) for item in compiled.trace.materializations]


def _context_result(compiled, snapshot, selection: dict, token_budget: int, include):
    items = [asdict(item) for item in compiled.items]
    return {
        "text": compiled.text,
        "packed_tokens": compiled.packed_tokens,
        "token_budget": token_budget,
        "corpus_generation": snapshot.corpus_sha256,
        "repo_map": sorted(selection["selected_paths"]),
        "pages": _page_items(items),
        "symbols": _symbol_items(items),
        "decisions": _items_of_type(items, {"decision"}),
        "incidents": _items_of_type(items, {"debugging", "incident"}),
        "active_task": _items_of_type(items, {"project-state"}),
        "evidence": _items_of_representation(items, "l2"),
        "retrieval_trace": asdict(compiled.trace.retrieval),
        "materialization_trace": _materialization_trace(compiled),
        "packing_trace": asdict(compiled.trace.packing),
        "missing_slugs": selection["missing"],
        "include": include,
    }


def _context_injection_events(make_event, selected_paths: set) -> list:
    events = []
    for slug in sorted({Path(path).stem for path in selected_paths}):
        event = make_event(
            event_kind="context_injected",
            query=None,
            retrieval_mode="direct",
            candidate_id=slug,
            rank=None,
            generation="legacy",
            source_tool="mcp.get_context",
        )
        if event is not None:
            events.append(event)
    return events


def _record_context_injections(selected_paths: set) -> None:
    if not selected_paths:
        return
    try:
        from retrieval_telemetry import (
            best_effort_make_event,
            best_effort_record_events,
        )

        events = _context_injection_events(best_effort_make_event, selected_paths)
        if events:
            best_effort_record_events(events)
    except Exception:
        pass


def _assess_contradiction_text(
    claim: str, *, deadline: float | None = None
) -> dict:
    from contradiction_pipeline import assess_text

    _check_deadline(deadline)
    return assess_text(claim)


def _check_contradiction(claim: str, *, deadline: float | None = None) -> dict:
    """Assess a claim and return evidence, validity, and lifecycle advice."""
    return _call_with_deadline(_assess_contradiction_text, claim, deadline=deadline)


def _log_decision(
    summary: str, rationale: str = "", *, deadline: float | None = None
) -> dict:
    """Append a decision to the daily log."""
    from datetime import datetime

    from daily_log_append import append_daily
    from memory_state import ROOT

    slug = "manual-decision"
    now = datetime.now()
    block = f"\n## [{now.strftime('%H:%M:%S')}] manual decision\n"
    block += f"Trigger: manual\nslug: {slug}\nroot: {ROOT}\n\n"
    block += f"Decision: {summary}\n"
    if rationale:
        block += f"Rationale: {rationale}\n"

    try:
        _check_deadline(deadline)
        path = append_daily(
            now.strftime("%Y-%m-%d"),
            slug,
            block,
            deadline=_operation_deadline(deadline),
            cancelled=_operation_cancelled(),
        )
        return {"status": "logged", "path": str(path)}
    except Exception as e:
        return {"error": _safe_exception_text(e, "mcp.log_decision")}


def _trigger_compile(*, deadline: float | None = None) -> dict:
    """Compile pending daily logs under the MCP operation bounds."""
    from compile_memory import run_pending_compile

    operation_deadline = _operation_deadline(deadline)
    _check_deadline(operation_deadline)
    returncode = run_pending_compile(
        trigger="manual",
        deadline=operation_deadline,
        cancelled=_operation_cancelled(),
    )
    # A non-zero return code is a compile that did not happen. Reporting it as
    # completed told the caller the opposite of what occurred.
    status = "completed" if returncode == 0 else "failed"
    return {"status": status, "returncode": returncode}


def _find_dead_code(
    directory: str, *, live: bool = False, deadline: float | None = None
) -> dict:
    """Find conservative dead-code candidates in a project directory."""
    from code_graph import find_dead_code

    operation_deadline = _operation_deadline(deadline)
    resolved, error = _validated_code_directory(
        directory, deadline=operation_deadline
    )
    if error:
        return {"error": error}
    try:
        result = _bounded_code_graph_call(
            find_dead_code,
            resolved,
            live=live,
            with_report=True,
            deadline=operation_deadline,
        )
    except TimeoutError as reason:
        return _code_graph_timeout_data(
            str(resolved), reason, completed=("directory_validation",)
        )
    return _dead_code_result_data(resolved, result)


def _dead_code_result_data(resolved: Path, result) -> dict:
    if isinstance(result, list):
        return {
            "directory": str(resolved),
            "candidates": result,
            "source_generation": None,
            "graph_complete": False,
            "unresolved_count": None,
            "fallback": True,
        }
    return {"directory": str(resolved), **result}


def _get_architecture(
    directory: str, *, live: bool = False, deadline: float | None = None
) -> dict:
    """Summarize the statically visible architecture of a project directory."""
    from code_graph import get_architecture

    operation_deadline = _operation_deadline(deadline)
    resolved, error = _validated_code_directory(
        directory, deadline=operation_deadline
    )
    if error:
        return {"error": error}
    try:
        architecture = _bounded_code_graph_call(
            get_architecture,
            resolved,
            live=live,
            with_report=True,
            deadline=operation_deadline,
        )
    except TimeoutError as reason:
        return _code_graph_timeout_data(
            str(resolved), reason, completed=("directory_validation",)
        )
    return _architecture_summary_data(resolved, architecture)


def _architecture_summary_data(resolved: Path, architecture: dict) -> dict:
    report = {
        "source_generation": architecture.get("source_generation"),
        "graph_complete": architecture.get("graph_complete", False),
        "unresolved_count": architecture.get("unresolved_count"),
        "fallback": architecture.get("fallback", True),
    }
    return {
        "directory": str(resolved),
        "mode": "summary",
        "architecture": architecture,
        **report,
    }


_ARCHITECTURE_SYMBOL_MODES = {"symbol", "callers", "callees", "dependencies"}

_ARCHITECTURE_REPORT_KEYS = (
    "source_generation",
    "graph_complete",
    "unresolved_count",
    "fallback",
)


def _architecture_path_mode_error(mode: str, symbol, target) -> str | None:
    if mode != "path":
        return None
    if symbol and target:
        return None
    return "symbol and target are required for path mode"


def _architecture_argument_error(mode: str, symbol, target) -> str | None:
    if mode in _ARCHITECTURE_SYMBOL_MODES and not symbol:
        return f"symbol is required for {mode} mode"
    return _architecture_path_mode_error(mode, symbol, target)


def _architecture_callers(request: dict):
    from code_graph import find_callers

    return find_callers(
        request["symbol"], request["resolved"], live=request["live"], with_report=True
    )


def _architecture_callees(request: dict):
    from code_graph import find_callees

    return find_callees(
        request["symbol"], request["resolved"], live=request["live"], with_report=True
    )


def _architecture_dependencies(request: dict):
    from code_graph import find_dependencies

    return find_dependencies(
        request["symbol"],
        request["resolved"],
        reverse=request["reverse"],
        live=request["live"],
        with_report=True,
    )


def _architecture_path(request: dict):
    from code_graph import find_paths

    return find_paths(
        request["symbol"],
        request["target"],
        request["resolved"],
        live=request["live"],
        with_report=True,
    )


def _architecture_community(request: dict):
    from code_graph import detect_communities

    return detect_communities(
        request["resolved"], live=request["live"], with_report=True
    )


def _architecture_symbol_dependencies(request: dict):
    """The symbol view never reverses; only the dependencies mode takes that."""
    from code_graph import find_dependencies

    return find_dependencies(
        request["symbol"], request["resolved"], live=request["live"], with_report=True
    )


def _architecture_symbol(request: dict) -> dict:
    deadline = request["deadline"]
    callers = _architecture_callers(request)
    _check_deadline(deadline)
    callees = _architecture_callees(request)
    _check_deadline(deadline)
    dependencies = _architecture_symbol_dependencies(request)
    return {
        "symbol": request["symbol"],
        "callers": callers.get("callers", []),
        "callees": callees.get("callees", []),
        "dependencies": dependencies.get("dependencies", []),
        **{key: callers.get(key) for key in _ARCHITECTURE_REPORT_KEYS},
    }


_ARCHITECTURE_MODE_QUERIES = {
    "callers": _architecture_callers,
    "callees": _architecture_callees,
    "dependencies": _architecture_dependencies,
    "path": _architecture_path,
    "community": _architecture_community,
}


def _architecture_report(architecture) -> dict:
    if not isinstance(architecture, dict):
        return {}
    return {
        key: architecture[key]
        for key in _ARCHITECTURE_REPORT_KEYS
        if key in architecture
    }


def _get_architecture_mode(
    directory: str,
    *,
    mode: str,
    symbol: str | None = None,
    target: str | None = None,
    reverse: bool = False,
    live: bool = False,
    deadline: float | None = None,
) -> dict:
    """Dispatch bounded graph queries while retaining live/store reports."""
    resolved, error = _validated_code_directory(directory, deadline=deadline)
    if error:
        return {"error": error}
    argument_error = _architecture_argument_error(mode, symbol, target)
    if argument_error is not None:
        return {"error": argument_error}
    _check_deadline(deadline)
    query = _ARCHITECTURE_MODE_QUERIES.get(mode, _architecture_symbol)
    architecture = query(
        {
            "resolved": resolved,
            "symbol": symbol,
            "target": target,
            "reverse": reverse,
            "live": live,
            "deadline": deadline,
        }
    )
    return {
        "directory": str(resolved),
        "mode": mode,
        "architecture": architecture,
        **_architecture_report(architecture),
    }


def _analyze_impact(
    *,
    directory: str,
    comparison: str = "dirty",
    base: str | None = None,
    target: str | None = None,
    branch: str | None = None,
    deadline: float | None = None,
) -> dict:
    """Run bounded diff-to-graph impact through the architecture tool."""
    from impact_analysis import COMPARISONS, analyze_impact

    resolved, error = _validated_code_directory(directory, deadline=deadline)
    if error:
        return {"error": error}
    if comparison not in COMPARISONS:
        return {"error": "invalid impact comparison"}
    return analyze_impact(
        root=resolved,
        comparison=comparison,
        base=base,
        target=target,
        branch=branch,
        deadline=deadline,
    )


_NAVIGATION_MANAGER: object | None = None
_NAVIGATION_MANAGER_CLOSING: object | None = None
_NAVIGATION_MANAGER_EPOCH = 0
_NAVIGATION_MANAGER_LOCK = threading.Lock()

_PRECISE_MODE_CAPABILITY = {
    "definition": "DEFINITIONS",
    "references": "REFERENCES",
    "implementations": "IMPLEMENTATIONS",
    "type": "TYPES",
    "diagnostics": "DIAGNOSTICS",
}


def _acquire_navigation_manager_lock(deadline: float) -> None:
    _check_deadline(deadline)
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not _NAVIGATION_MANAGER_LOCK.acquire(timeout=remaining):
        raise TimeoutError("navigation session manager lock deadline expired")
    try:
        _check_deadline(deadline)
    except BaseException:
        _NAVIGATION_MANAGER_LOCK.release()
        raise


def _check_navigation_manager_stop(deadline: float, cancelled) -> None:
    _check_deadline(deadline)
    if cancelled():
        raise TimeoutError("navigation operation cancelled")
    _check_deadline(deadline)


def _require_navigation_manager_lifecycle(expected_epoch: int) -> None:
    """The manager about to be handed out must still be the one asked for."""
    if expected_epoch != _NAVIGATION_MANAGER_EPOCH:
        raise TimeoutError("navigation session manager lifecycle changed")
    if _NAVIGATION_MANAGER_CLOSING is not None:
        raise TimeoutError("navigation session manager is closing")


def _navigation_session_manager(
    deadline: float,
    expected_epoch: int,
    cancelled,
):
    global _NAVIGATION_MANAGER
    _check_navigation_manager_stop(deadline, cancelled)
    _acquire_navigation_manager_lock(deadline)
    try:
        _check_navigation_manager_stop(deadline, cancelled)
        _require_navigation_manager_lifecycle(expected_epoch)
        if _NAVIGATION_MANAGER is None:
            from memory_state import STATE_ROOT
            from pyright_session import PyrightSessionManager

            _check_navigation_manager_stop(deadline, cancelled)
            manager = PyrightSessionManager(state_root=STATE_ROOT)
            _check_navigation_manager_stop(deadline, cancelled)
            _NAVIGATION_MANAGER = manager
        _check_navigation_manager_stop(deadline, cancelled)
        return _NAVIGATION_MANAGER
    finally:
        _NAVIGATION_MANAGER_LOCK.release()


def _detach_navigation_manager():
    """Claim the manager that must be closed. Caller holds the manager lock."""
    global _NAVIGATION_MANAGER, _NAVIGATION_MANAGER_CLOSING, _NAVIGATION_MANAGER_EPOCH
    _NAVIGATION_MANAGER_EPOCH += 1
    if _NAVIGATION_MANAGER_CLOSING is not None:
        return _NAVIGATION_MANAGER_CLOSING
    manager = _NAVIGATION_MANAGER
    if manager is not None:
        _NAVIGATION_MANAGER = None
        _NAVIGATION_MANAGER_CLOSING = manager
    return manager


def _clear_navigation_manager_closing(manager) -> None:
    global _NAVIGATION_MANAGER_CLOSING
    if _NAVIGATION_MANAGER_CLOSING is manager:
        _NAVIGATION_MANAGER_CLOSING = None


def _navigation_deadline_error(deadline: float):
    try:
        _check_deadline(deadline)
    except TimeoutError as error:
        return error
    return None


def _reacquire_navigation_manager_lock(deadline: float, deadline_error) -> None:
    """Past the deadline the lock is taken only if it is free right now."""
    if deadline_error is None:
        _acquire_navigation_manager_lock(deadline)
        return
    if not _NAVIGATION_MANAGER_LOCK.acquire(blocking=False):
        raise deadline_error


def _close_navigation_session_manager(deadline: float) -> None:
    _acquire_navigation_manager_lock(deadline)
    try:
        manager = _detach_navigation_manager()
    finally:
        _NAVIGATION_MANAGER_LOCK.release()
    if manager is None:
        return
    _check_deadline(deadline)
    manager.close_all(deadline=deadline)
    deadline_error = _navigation_deadline_error(deadline)
    _reacquire_navigation_manager_lock(deadline, deadline_error)
    try:
        _clear_navigation_manager_closing(manager)
    finally:
        _NAVIGATION_MANAGER_LOCK.release()
    if deadline_error is not None:
        raise deadline_error


def _is_precise_architecture_request(arguments: dict) -> bool:
    mode = arguments.get("mode", "summary")
    if mode in PRECISE_ARCHITECTURE_MODES:
        return True
    if mode in {"callers", "callees"} and all(
        key in arguments for key in ("path", "line", "character")
    ):
        return True
    return False


def _same_filesystem_path(
    left: Path,
    right: Path,
    *,
    deadline: float | None = None,
) -> bool:
    _check_deadline(deadline)
    try:
        same = Path(left).samefile(Path(right))
    except OSError:
        same = False
    _check_deadline(deadline)
    return same


def _navigation_capability(mode: str):
    from code_intelligence import Capability

    if mode in _PRECISE_MODE_CAPABILITY:
        return Capability[_PRECISE_MODE_CAPABILITY[mode]]
    if mode in {"callers", "callees"}:
        return Capability.CALLS
    raise ValueError("mode is not a precise navigation mode")


def _normalized_navigation_failure(
    *,
    directory: str | None,
    mode: str,
    status: str,
    warning: str,
    offset: int,
    limit: int,
    scope=None,
    provider: str | None = None,
    provider_version: str | None = None,
    readiness: str = "not_ready",
) -> dict:
    capability = _navigation_capability(mode)
    return {
        "directory": directory,
        "mode": mode,
        "status": status,
        "freshness": {
            "workspace_revision_before": "",
            "workspace_revision_after": "",
            "current": "",
        },
        "provider": {"name": provider, "version": provider_version},
        "symbol": None,
        "total": 0,
        "requested_capability": capability.value,
        "effective_capability": None,
        "position_encoding": None,
        "readiness": readiness,
        "repository": {
            "repository_id": None if scope is None else scope.repository_id,
            "checkout_id": None if scope is None else scope.checkout_id,
        },
        "document_version": None,
        "offset": offset,
        "limit": limit,
        "truncated": False,
        "omitted": 0,
        "next_offset": None,
        "resolution": "unresolved",
        "groups": [],
        "diagnostics": [],
        "hover": None,
        "provenance": [],
        "warnings": (warning,),
    }


def _check_navigation_stop(deadline: float | None) -> None:
    _check_deadline(deadline)
    if _operation_cancelled()():
        raise TimeoutError("navigation operation cancelled")
    _check_deadline(deadline)


def _open_navigation_graph(scope, deadline: float | None):
    import code_graph

    _check_navigation_stop(deadline)
    graph = code_graph._active_evidence_graph(
        Path(scope.checkout_root),
        read_only=True,
        deadline=deadline,
        cancelled=_operation_cancelled(),
    )
    _check_navigation_stop(deadline)
    if graph is None:
        return None
    graph_scope = getattr(graph, "repository_scope", None)
    if (
        graph_scope is None
        or graph_scope.repository_id != scope.repository_id
        or graph_scope.checkout_id != scope.checkout_id
    ):
        graph.close()
        _check_navigation_stop(deadline)
        return None
    return graph


def _navigation_relative_path(
    scope,
    span: dict,
    *,
    deadline: float | None,
) -> str:
    from lsp_security import validate_repository_relative_path

    _check_navigation_stop(deadline)
    relative = span.get("relative_path")
    if not isinstance(relative, str):
        file_value = span.get("file")
        if not isinstance(file_value, str):
            raise ValueError("graph span has no source path")
        file_path = Path(file_value)
        if file_path.is_absolute():
            root = Path(scope.checkout_root).resolve(strict=True)
            _check_navigation_stop(deadline)
            source = file_path.resolve(strict=True)
            _check_navigation_stop(deadline)
            relative = source.relative_to(root).as_posix()
        else:
            relative = file_value
    normalized = validate_repository_relative_path(relative)
    _check_navigation_stop(deadline)
    return normalized


def _navigation_source_bytes(
    scope,
    relative_path: str,
    *,
    deadline: float | None,
) -> bytes:
    from lsp_security import read_repository_source_bytes

    _check_navigation_stop(deadline)
    content = read_repository_source_bytes(
        scope,
        relative_path,
        max_bytes=MAX_NAVIGATION_SOURCE_BYTES,
        deadline=deadline,
    )
    _check_navigation_stop(deadline)
    content.decode("utf-8", errors="strict")
    _check_navigation_stop(deadline)
    return content


class _NavigationSourceCache:
    __slots__ = ("_bytes", "_values")

    def __init__(self) -> None:
        self._values: dict[tuple[str, str, str], tuple[bytes, str] | None] = {}
        self._bytes = 0

    def read(self, scope, relative_path: str, *, deadline: float | None):
        key = (scope.repository_id, scope.checkout_id, relative_path)
        if key in self._values:
            return self._values[key]
        if len(self._values) >= MAX_NAVIGATION_GRAPH_FACTS:
            return None
        content = _navigation_source_bytes(scope, relative_path, deadline=deadline)
        return self._remember(key, content)

    def _remember(self, key, content: bytes):
        """Cache the bytes unless they would push the cache past its ceiling."""
        if self._bytes + len(content) > MAX_NAVIGATION_SOURCE_CACHE_BYTES:
            self._values[key] = None
            return None
        cached = (content, hashlib.sha256(content).hexdigest())
        self._values[key] = cached
        self._bytes += len(content)
        return cached


def _navigation_digest(value: object) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
        return value
    return None


def _navigation_location_from_span(
    scope,
    span: dict,
    *,
    source_kind: str,
    require_span_hash: bool,
    metadata: dict | None,
    graph_version: str,
    deadline: float | None,
    source_cache: _NavigationSourceCache | None = None,
):
    try:
        return _resolved_navigation_location(
            scope,
            span,
            source_kind=source_kind,
            require_span_hash=require_span_hash,
            metadata=metadata,
            graph_version=graph_version,
            deadline=deadline,
            source_cache=_navigation_cache(source_cache),
        )
    except TimeoutError:
        raise
    except (OSError, TypeError, UnicodeError, ValueError):
        return None


def _navigation_span_source(scope, span: dict, deadline, source_cache):
    """Return (relative path, bytes) while the recorded source digest holds."""
    relative_path = _navigation_relative_path(scope, span, deadline=deadline)
    expected_source_sha256 = _navigation_digest(span.get("source_sha256"))
    if expected_source_sha256 is None:
        return None
    cached = source_cache.read(scope, relative_path, deadline=deadline)
    if cached is None or cached[1] != expected_source_sha256:
        return None
    return relative_path, cached[0]


def _span_range_invalid(byte_start: int, byte_end: int, size: int) -> bool:
    if byte_start < 0 or byte_end <= byte_start:
        return True
    return byte_end > size


def _span_bounds(span: dict, content: bytes):
    byte_start = span.get("byte_start")
    byte_end = span.get("byte_end")
    if not _is_integer_value(byte_start) or not _is_integer_value(byte_end):
        return None
    if _span_range_invalid(byte_start, byte_end, len(content)):
        return None
    return byte_start, byte_end


def _span_kind_valid(source_kind: str, require_span_hash: bool) -> bool:
    if source_kind not in {"evidence", "occurrence"}:
        return False
    return require_span_hash == (source_kind == "evidence")


def _span_hash_matches(span: dict, content: bytes, bounds) -> bool:
    expected = span.get("span_sha256")
    if _navigation_digest(expected) is None:
        return False
    return hashlib.sha256(content[bounds[0]:bounds[1]]).hexdigest() == expected


def _validated_span_bounds(
    span: dict, content: bytes, source_kind: str, require_span_hash: bool
):
    if not _span_kind_valid(source_kind, require_span_hash):
        return None
    bounds = _span_bounds(span, content)
    if bounds is None or (
        require_span_hash and not _span_hash_matches(span, content, bounds)
    ):
        return None
    return bounds


def _span_line_position(document, content: bytes, byte_start: int, deadline):
    """Return the 1-based line and byte column that hold byte_start."""
    for line_number, (line_start, line_end) in enumerate(document.line_spans, 1):
        _check_navigation_stop(deadline)
        if line_start <= byte_start <= line_end:
            content[line_start:byte_start].decode("utf-8", errors="strict")
            return line_number, byte_start - line_start
    return None


def _reported_line_disagrees(span: dict, line: int) -> bool:
    reported_line = span.get("line_start")
    if reported_line is None:
        return False
    if not _is_integer_value(reported_line):
        return True
    return reported_line != line


def _span_position(relative_path: str, content: bytes, byte_start: int, span, deadline):
    from lsp_positions import SourceDocument

    _check_navigation_stop(deadline)
    document = SourceDocument.from_bytes(relative_path, content)
    _check_navigation_stop(deadline)
    position = _span_line_position(document, content, byte_start, deadline)
    if position is None:
        return None
    if _reported_line_disagrees(span, position[0]):
        return None
    return position


def _containing_symbol(metadata) -> str | None:
    if not isinstance(metadata, dict):
        return None
    owner = metadata.get("owner")
    if isinstance(owner, str) and owner:
        return owner
    return None


def _span_signature(span: dict, content: bytes, bounds) -> str | None:
    if span.get("role") not in {"definition", "declaration"}:
        return None
    return content[bounds[0]:bounds[1]].decode("utf-8", errors="strict")


def _navigation_location(
    relative_path, content, bounds, position, span, metadata, graph_version
):
    from code_intelligence import PositionRange
    from code_navigation import NavigationLocation, Provenance, ResolutionLabel

    line, character = position
    return NavigationLocation(
        relative_path,
        PositionRange(bounds[0], bounds[1]),
        line,
        character,
        _containing_symbol(metadata),
        _span_signature(span, content, bounds),
        ResolutionLabel.GRAPH_CANDIDATE,
        (Provenance("graph", "evidence-graph", graph_version, "graph_candidate"),),
    )


def _resolved_navigation_location(
    scope,
    span: dict,
    *,
    source_kind: str,
    require_span_hash: bool,
    metadata: dict | None,
    graph_version: str,
    deadline: float | None,
    source_cache,
):
    source = _navigation_span_source(scope, span, deadline, source_cache)
    if source is None:
        return None
    relative_path, content = source
    bounds = _validated_span_bounds(span, content, source_kind, require_span_hash)
    if bounds is None:
        return None
    return _located_navigation_span(
        relative_path, content, bounds, span, metadata, graph_version, deadline
    )


def _located_navigation_span(
    relative_path, content, bounds, span, metadata, graph_version, deadline
):
    """The location for a validated span, or None when its position is unreadable."""
    position = _span_position(relative_path, content, bounds[0], span, deadline)
    if position is None:
        return None
    _check_navigation_stop(deadline)
    return _navigation_location(
        relative_path, content, bounds, position, span, metadata, graph_version
    )


def _bounded_navigation_locations(locations, deadline: float | None):
    unique = {}
    for location in locations:
        _check_navigation_stop(deadline)
        if location is None:
            continue
        key = (
            location.path,
            location.range.byte_start,
            location.range.byte_end,
            location.line,
            location.character,
        )
        unique.setdefault(key, location)
        if len(unique) >= MAX_NAVIGATION_GRAPH_FACTS:
            break
    _check_navigation_stop(deadline)
    return tuple(unique[key] for key in sorted(unique))


def _graph_nodes_for_symbol(graph, symbol: str, deadline: float | None):
    _check_navigation_stop(deadline)
    nodes = graph.find_nodes(
        kinds=("class", "function", "method"),
        name=symbol,
        max_rows=MAX_NAVIGATION_GRAPH_FACTS,
        deadline=deadline,
    )
    _check_navigation_stop(deadline)
    return tuple(nodes[:MAX_NAVIGATION_GRAPH_FACTS])


def _navigation_cache(source_cache: _NavigationSourceCache | None):
    if source_cache is None:
        return _NavigationSourceCache()
    return source_cache


def _graph_generation_version(graph) -> str:
    return str(getattr(graph, "generation_id", None) or "structural")


def _graph_node_metadata(node):
    metadata = node.get("metadata")
    if isinstance(metadata, dict):
        return metadata
    return None


def _declaration_locations_for_node(
    graph, node, scope, version, deadline, source_cache, remaining
):
    """Return this node's declaration locations and how much budget they spent."""
    occurrences = graph.occurrences(
        node["node_id"],
        max_rows=remaining,
        deadline=deadline,
    )
    _check_navigation_stop(deadline)
    metadata = _graph_node_metadata(node)
    bounded = occurrences[:remaining]
    locations = []
    for occurrence in bounded:
        _check_navigation_stop(deadline)
        if occurrence.get("role") not in {"definition", "declaration"}:
            continue
        locations.append(
            _navigation_location_from_span(
                scope,
                occurrence,
                source_kind="occurrence",
                require_span_hash=False,
                metadata=metadata,
                graph_version=version,
                deadline=deadline,
                source_cache=source_cache,
            )
        )
    return locations, len(bounded)


def _collected_declaration_locations(graph, symbol, scope, deadline, source_cache):
    version = _graph_generation_version(graph)
    locations = []
    remaining = MAX_NAVIGATION_GRAPH_FACTS
    for node in _graph_nodes_for_symbol(graph, symbol, deadline):
        _check_navigation_stop(deadline)
        if remaining <= 0:
            break
        found, spent = _declaration_locations_for_node(
            graph, node, scope, version, deadline, source_cache, remaining
        )
        locations.extend(found)
        remaining -= spent
    return _bounded_navigation_locations(locations, deadline)


def _graph_declaration_locations(
    symbol: str,
    scope,
    deadline: float | None,
    source_cache: _NavigationSourceCache | None = None,
):
    graph = _open_navigation_graph(scope, deadline)
    if graph is None:
        return ()
    try:
        return _collected_declaration_locations(
            graph, symbol, scope, deadline, _navigation_cache(source_cache)
        )
    finally:
        graph.close()
        _check_navigation_stop(deadline)


def _call_edge_source_key(direction: str) -> str:
    if direction == "incoming":
        return "target_node_id"
    return "source_node_id"


def _matching_call_edges(edges, source_key: str, node_ids: set, deadline):
    for edge in edges:
        _check_navigation_stop(deadline)
        if edge.get(source_key) in node_ids:
            yield edge


def _call_evidence_locations(
    graph, edge, scope, version, deadline, source_cache, remaining
):
    """Return this edge's evidence locations and how much budget they spent."""
    evidence = graph.evidence_spans(
        assertion_id=edge["assertion_id"],
        max_rows=remaining,
        deadline=deadline,
    )
    _check_navigation_stop(deadline)
    bounded = evidence[:remaining]
    locations = []
    for span in bounded:
        _check_navigation_stop(deadline)
        locations.append(
            _navigation_location_from_span(
                scope,
                span,
                source_kind="evidence",
                require_span_hash=True,
                metadata=None,
                graph_version=version,
                deadline=deadline,
                source_cache=source_cache,
            )
        )
    return locations, len(bounded)


def _collected_call_locations(graph, symbol, scope, direction, deadline, source_cache):
    node_ids = {
        node["node_id"] for node in _graph_nodes_for_symbol(graph, symbol, deadline)
    }
    _check_navigation_stop(deadline)
    edges = graph.edges(
        edge_types=("CALLS",),
        max_rows=MAX_NAVIGATION_GRAPH_FACTS,
        deadline=deadline,
    )
    _check_navigation_stop(deadline)
    source_key = _call_edge_source_key(direction)
    version = _graph_generation_version(graph)
    locations = []
    remaining = MAX_NAVIGATION_GRAPH_FACTS
    for edge in _matching_call_edges(edges, source_key, node_ids, deadline):
        if remaining <= 0:
            break
        found, spent = _call_evidence_locations(
            graph, edge, scope, version, deadline, source_cache, remaining
        )
        locations.extend(found)
        remaining -= spent
    return _bounded_navigation_locations(locations, deadline)


def _graph_call_locations(
    symbol: str,
    scope,
    *,
    direction: str,
    deadline: float | None,
    source_cache: _NavigationSourceCache | None = None,
):
    graph = _open_navigation_graph(scope, deadline)
    if graph is None:
        return ()
    try:
        return _collected_call_locations(
            graph,
            symbol,
            scope,
            direction,
            deadline,
            _navigation_cache(source_cache),
        )
    finally:
        graph.close()
        _check_navigation_stop(deadline)


def _navigation_anchor_symbol(
    scope,
    path: str,
    line: int,
    character: int,
    *,
    byte_offset: int | None = None,
    deadline: float | None,
    source_cache: _NavigationSourceCache | None = None,
) -> str | None:
    try:
        return _resolved_anchor_symbol(
            scope,
            path,
            line,
            character,
            byte_offset,
            deadline,
            _navigation_cache(source_cache),
        )
    except TimeoutError:
        raise
    except (OSError, TypeError, UnicodeError, ValueError):
        return None


def _anchor_identifier(line_bytes: bytes, character: int, deadline) -> str | None:
    for match in re.finditer(rb"[A-Za-z_][A-Za-z0-9_]*", line_bytes):
        _check_navigation_stop(deadline)
        if match.start() <= character <= match.end():
            return match.group().decode("ascii")
    return None


def _resolved_anchor_symbol(
    scope, path, line, character, byte_offset, deadline, source_cache
) -> str | None:
    from lsp_positions import SourceDocument

    cached = source_cache.read(scope, path, deadline=deadline)
    if cached is None:
        return None
    content, _source_sha256 = cached
    _check_navigation_stop(deadline)
    document = SourceDocument.from_bytes(path, content)
    _check_navigation_stop(deadline)
    anchor = document.validate_anchor(line=line, character=character)
    if byte_offset is not None and anchor.byte_offset != byte_offset:
        return None
    line_start, line_end = document.line_spans[line - 1]
    return _anchor_identifier(content[line_start:line_end], character, deadline)


def _navigation_structural_candidates(request, deadline: float):
    from code_intelligence import Capability

    _check_navigation_stop(deadline)
    source_cache = _NavigationSourceCache()
    symbol = _navigation_anchor_symbol(
        request.repository,
        request.path,
        request.line,
        request.character,
        deadline=deadline,
        source_cache=source_cache,
    )
    if symbol is None:
        return ()
    return _structural_candidates_for(
        Capability, request, symbol, deadline, source_cache
    )


def _structural_call_candidates(request, symbol, deadline, source_cache):
    if request.direction not in {"incoming", "outgoing"}:
        return ()
    return _graph_call_locations(
        symbol,
        request.repository,
        direction=request.direction,
        deadline=deadline,
        source_cache=source_cache,
    )


def _structural_candidates_for(Capability, request, symbol, deadline, source_cache):
    handlers = {
        Capability.DEFINITIONS: lambda: _graph_declaration_locations(
            symbol,
            request.repository,
            deadline,
            source_cache,
        ),
        Capability.REFERENCES: lambda: _graph_call_locations(
            symbol,
            request.repository,
            direction="incoming",
            deadline=deadline,
            source_cache=source_cache,
        ),
        Capability.CALLS: lambda: _structural_call_candidates(
            request, symbol, deadline, source_cache
        ),
    }
    handler = handlers.get(request.capability)
    if handler is None:
        return ()
    return handler()


def _navigation_symbol_resolver(symbol, scope, deadline):
    if not isinstance(symbol, str) or not symbol:
        return ()
    return _graph_declaration_locations(
        symbol,
        scope,
        deadline,
        _NavigationSourceCache(),
    )


def _graph_node_ids_at_anchor(
    graph,
    symbol,
    anchor,
    scope,
    deadline,
    source_cache: _NavigationSourceCache | None = None,
):
    source_cache = _navigation_cache(source_cache)
    version = _graph_generation_version(graph)
    identifiers = set()
    remaining = MAX_NAVIGATION_GRAPH_FACTS
    for node in _graph_nodes_for_symbol(graph, symbol, deadline):
        _check_navigation_stop(deadline)
        if remaining <= 0:
            break
        covers, spent = _node_covers_anchor(
            graph, node, anchor, scope, version, deadline, source_cache, remaining
        )
        remaining -= spent
        if covers:
            identifiers.add(node["node_id"])
    return identifiers


def _location_covers_anchor(location, anchor) -> bool:
    if location is None or location.path != anchor.path:
        return False
    return location.range.byte_start <= anchor.byte_offset < location.range.byte_end


def _node_covers_anchor(
    graph, node, anchor, scope, version, deadline, source_cache, remaining
):
    """Report whether this node declares the anchor, and what budget it spent."""
    occurrences = graph.occurrences(
        node["node_id"],
        max_rows=remaining,
        deadline=deadline,
    )
    _check_navigation_stop(deadline)
    bounded = occurrences[:remaining]
    covers = False
    for occurrence in bounded:
        _check_navigation_stop(deadline)
        if occurrence.get("role") not in {"definition", "declaration"}:
            continue
        location = _navigation_location_from_span(
            scope,
            occurrence,
            source_kind="occurrence",
            require_span_hash=False,
            metadata=None,
            graph_version=version,
            deadline=deadline,
            source_cache=source_cache,
        )
        covers = covers or _location_covers_anchor(location, anchor)
    return covers, len(bounded)


def _navigation_edge_verifier(source, target, scope, deadline):
    source_cache = _NavigationSourceCache()
    source_symbol = _navigation_anchor_symbol(
        scope,
        source.path,
        source.line,
        source.utf8_character,
        byte_offset=source.byte_offset,
        deadline=deadline,
        source_cache=source_cache,
    )
    target_symbol = _navigation_anchor_symbol(
        scope,
        target.path,
        target.line,
        target.utf8_character,
        byte_offset=target.byte_offset,
        deadline=deadline,
        source_cache=source_cache,
    )
    if source_symbol is None or target_symbol is None:
        return False
    graph = _open_navigation_graph(scope, deadline)
    if graph is None:
        return False
    try:
        return _verified_call_edge(
            graph,
            (source, source_symbol),
            (target, target_symbol),
            scope,
            deadline,
            source_cache,
        )
    finally:
        graph.close()
        _check_navigation_stop(deadline)


def _edge_connects(edges, source_ids: set, target_ids: set, deadline) -> bool:
    for edge in edges:
        _check_navigation_stop(deadline)
        if (
            edge.get("source_node_id") in source_ids
            and edge.get("target_node_id") in target_ids
        ):
            return True
    return False


def _verified_call_edge(graph, source, target, scope, deadline, source_cache) -> bool:
    source_anchor, source_symbol = source
    target_anchor, target_symbol = target
    source_ids = _graph_node_ids_at_anchor(
        graph, source_symbol, source_anchor, scope, deadline, source_cache
    )
    target_ids = _graph_node_ids_at_anchor(
        graph, target_symbol, target_anchor, scope, deadline, source_cache
    )
    _check_navigation_stop(deadline)
    edges = graph.edges(
        edge_types=("CALLS",),
        max_rows=MAX_NAVIGATION_GRAPH_FACTS,
        deadline=deadline,
    )
    _check_navigation_stop(deadline)
    return _edge_connects(edges, source_ids, target_ids, deadline)


def _get_precise_architecture(
    directory: str,
    *,
    mode: str,
    path: str,
    line: int,
    character: int,
    offset: int = 0,
    limit: int = 10,
    deadline: float | None = None,
) -> dict:
    """Route precise modes through the owned CodeNavigation facade."""
    effective_deadline = _navigation_effective_deadline(deadline)
    progress = {"stage": "directory", "scope": None, "resolved": None}
    try:
        return _precise_architecture_result(
            progress,
            directory,
            mode,
            {"path": path, "line": line, "character": character},
            offset,
            limit,
            effective_deadline,
        )
    except TimeoutError:
        return _normalized_navigation_failure(
            directory=_progress_directory(progress),
            mode=mode,
            status="timeout",
            warning="navigation_timeout",
            offset=offset,
            limit=limit,
            scope=progress["scope"],
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _navigation_stage_failure(progress, mode, offset, limit)


def _navigation_effective_deadline(deadline: float | None) -> float:
    if deadline is not None:
        return deadline
    return time.monotonic() + MCP_LSP_STARTUP_SECONDS


def _progress_directory(progress: dict) -> str | None:
    resolved = progress["resolved"]
    if resolved is None:
        return None
    return str(resolved)


_NAVIGATION_STAGE_WARNINGS = {
    "manager": "navigation_provider_not_ready",
    "renderer": "navigation_render_failed",
    "source": "navigation_source_unavailable",
}

_NAVIGATION_PROVIDER_STAGES = {"manager", "facade", "renderer"}


def _navigation_stage_status(stage: str) -> str:
    if stage == "manager":
        return "not_ready"
    return "error"


def _navigation_stage_provider(stage: str) -> str | None:
    if stage in _NAVIGATION_PROVIDER_STAGES:
        return "pyright"
    return None


def _navigation_stage_failure(progress: dict, mode, offset, limit) -> dict:
    stage = progress["stage"]
    return _normalized_navigation_failure(
        directory=_progress_directory(progress),
        mode=mode,
        status=_navigation_stage_status(stage),
        warning=_NAVIGATION_STAGE_WARNINGS.get(stage, "navigation_setup_failed"),
        offset=offset,
        limit=limit,
        scope=progress["scope"],
        provider=_navigation_stage_provider(stage),
    )


def _navigation_manager_epoch(deadline: float, cancelled) -> int:
    _check_navigation_manager_stop(deadline, cancelled)
    _acquire_navigation_manager_lock(deadline)
    try:
        _check_navigation_manager_stop(deadline, cancelled)
        epoch = _NAVIGATION_MANAGER_EPOCH
    finally:
        _NAVIGATION_MANAGER_LOCK.release()
    _check_navigation_manager_stop(deadline, cancelled)
    return epoch


def _navigation_scope(resolved: Path, deadline: float, cancelled):
    from repository_scope import resolve_repository_scope

    _check_deadline(deadline)
    scope = resolve_repository_scope(resolved, deadline=deadline, cancelled=cancelled)
    _check_deadline(deadline)
    return scope


def _validated_navigation_source(scope, path: str, deadline: float) -> str:
    from lsp_security import (
        resolve_repository_source,
        validate_repository_relative_path,
    )

    _check_deadline(deadline)
    normalized_path = validate_repository_relative_path(path)
    _check_deadline(deadline)
    resolve_repository_source(scope, normalized_path, must_exist=True)
    _check_deadline(deadline)
    return normalized_path


def _navigation_session(scope, deadline: float, manager_epoch: int, cancelled):
    manager = _navigation_session_manager(deadline, manager_epoch, cancelled)
    _check_deadline(deadline)
    session = manager.get(scope, deadline=deadline)
    _check_deadline(deadline)
    return session


_NAVIGATION_MODE_DIRECTIONS = {"callers": "incoming", "callees": "outgoing"}


def _navigation_query_result(
    scope, session, mode, normalized_path, anchor, offset, limit, deadline
):
    from code_navigation import CodeNavigation, NavigationRequest

    navigation = CodeNavigation(
        scope,
        session,
        session.identity,
        structural_candidates=_navigation_structural_candidates,
        symbol_resolver=_navigation_symbol_resolver,
        edge_verifier=_navigation_edge_verifier,
    )
    _check_deadline(deadline)
    request = NavigationRequest(
        scope,
        _navigation_capability(mode),
        normalized_path,
        anchor["line"],
        anchor["character"],
        offset=offset,
        limit=limit,
        direction=_NAVIGATION_MODE_DIRECTIONS.get(mode),
    )
    _check_deadline(deadline)
    result = navigation.query(request, deadline=deadline)
    _check_deadline(deadline)
    return result


def _rendered_navigation_result(resolved: Path, mode, result, deadline: float) -> dict:
    from code_navigation_renderer import render_navigation

    rendered = render_navigation(result)
    _check_deadline(deadline)
    data = {"directory": str(resolved), "mode": mode, **rendered}
    _check_deadline(deadline)
    data = _sanitize_navigation_data(data)
    _check_deadline(deadline)
    return data


def _precise_architecture_result(
    progress: dict, directory, mode, anchor: dict, offset, limit, deadline: float
) -> dict:
    cancelled = _operation_cancelled()
    manager_epoch = _navigation_manager_epoch(deadline, cancelled)
    resolved, error = _validated_code_directory(directory, deadline=deadline)
    progress["resolved"] = resolved
    _check_deadline(deadline)
    if error or resolved is None:
        return _normalized_navigation_failure(
            directory=None,
            mode=mode,
            status="error",
            warning="navigation_directory_invalid",
            offset=offset,
            limit=limit,
        )
    progress["stage"] = "scope"
    scope = _navigation_scope(resolved, deadline, cancelled)
    progress["scope"] = scope
    if not _same_filesystem_path(
        resolved, Path(scope.checkout_root), deadline=deadline
    ):
        return _normalized_navigation_failure(
            directory=str(resolved),
            mode=mode,
            status="error",
            warning="navigation_directory_not_checkout_root",
            offset=offset,
            limit=limit,
            scope=scope,
        )
    progress["stage"] = "source"
    normalized_path = _validated_navigation_source(scope, anchor["path"], deadline)
    progress["stage"] = "manager"
    session = _navigation_session(scope, deadline, manager_epoch, cancelled)
    progress["stage"] = "facade"
    result = _navigation_query_result(
        scope, session, mode, normalized_path, anchor, offset, limit, deadline
    )
    progress["stage"] = "renderer"
    return _rendered_navigation_result(resolved, mode, result, deadline)


def _operator_result(
    action: str,
    *,
    ids: list[str] | None = None,
    states: list[str] | None = None,
    codes: list[str] | None = None,
    counts: dict[str, int] | None = None,
    overall_status: str | None = None,
) -> dict:
    identifiers = list(ids or [])
    state_values = set(states or [])
    return {
        "action": action,
        "overall_status": overall_status or _default_overall_status(state_values),
        "ids": identifiers[:100],
        "counts": _operator_counts(counts, identifiers),
        "states": sorted(state_values),
        "codes": sorted(set(codes or [])),
    }


def _operator_counts(counts: dict | None, identifiers: list) -> dict:
    if counts is not None:
        return counts
    return {"items": len(identifiers)}


def _default_overall_status(state_values: set) -> str:
    if "error" in state_values:
        return "error"
    if "degraded" in state_values:
        return "degraded"
    return "ok"


_DOCTOR_MUTATIONS = {
    "queue-cancel",
    "queue-redrive",
    "transaction-recover",
    "transaction-undo",
}

_DOCTOR_FAILURE_CODES = {
    "KeyError": "unknown_target",
    "TimeoutError": "owner_busy",
    "MigrationBusy": "owner_busy",
}


def _doctor_failure_code(error: BaseException) -> str:
    return _DOCTOR_FAILURE_CODES.get(type(error).__name__, "operation_failed")


def _doctor_report_codes(report: dict) -> list:
    codes = [item["code"] for item in report["run_deletion"]["blockers"]]
    for check in report["checks"]:
        codes.extend(
            str(code) for code in check.get("details", {}).get("codes", [])
        )
    return codes


def _doctor_status(context: dict) -> dict:
    from doctor import run_doctor

    report = run_doctor(
        root=context["root"],
        state_root=context["state_root"],
        deadline=context["deadline"],
    )
    return _operator_result(
        "status",
        ids=[str(check["id"]) for check in report["checks"]][: context["limit"]],
        states=[str(report["overall_status"])],
        codes=_doctor_report_codes(report),
        counts={key: int(value) for key, value in report["counts"].items()},
        overall_status=str(report["overall_status"]),
    )


def _queue_database_path(context: dict) -> Path:
    """The queue database in force; adoption leaves a tombstone at the legacy path."""
    from installed_memory_repair import adopted_database_path

    return adopted_database_path(
        database_name="queue", state_root=context["state_root"]
    )


def _missing_queue_result(action: str) -> dict:
    if action == "queue-dead-list":
        return _operator_result(action, codes=["queue_missing"])
    return _operator_result(
        action, states=["error"], codes=["queue_missing"], overall_status="error"
    )


def _open_queue_reader(context: dict):
    from reliable_memory import open_readonly_operational_db

    return open_readonly_operational_db(
        _queue_database_path(context),
        context["state_root"],
        max_bytes=256 * 1024 * 1024,
        busy_ms=QUEUE_READ_BUSY_MS,
    )


def _row_error_codes(row) -> list:
    if row["error_code"]:
        return [str(row["error_code"])]
    return []


def _queue_inspect_result(connection, target_id) -> dict:
    row = connection.execute(
        "SELECT id,state,error_code FROM tasks WHERE id=?", (target_id,)
    ).fetchone()
    if row is None:
        return _operator_result(
            "queue-inspect",
            states=["error"],
            codes=["unknown_task"],
            overall_status="error",
        )
    return _operator_result(
        "queue-inspect",
        ids=[str(row["id"])],
        states=[str(row["state"])],
        codes=_row_error_codes(row),
    )


def _queue_dead_list_result(connection, limit: int) -> dict:
    rows = connection.execute(
        "SELECT id,state,error_code FROM tasks WHERE state='dead' "
        "ORDER BY updated_at,id LIMIT ?",
        (limit,),
    ).fetchall()
    return _operator_result(
        "queue-dead-list",
        ids=[str(row["id"]) for row in rows],
        states=[str(row["state"]) for row in rows],
        codes=[str(row["error_code"]) for row in rows if row["error_code"]],
    )


def _doctor_queue_read(context: dict) -> dict:
    action = context["action"]
    if not _queue_database_path(context).is_file():
        return _missing_queue_result(action)
    connection = _open_queue_reader(context)
    try:
        if action == "queue-inspect":
            return _queue_inspect_result(connection, context["target_id"])
        return _queue_dead_list_result(connection, context["limit"])
    finally:
        connection.close()


def _doctor_queue_cancel(context: dict) -> dict:
    from memory_queue import active_or_legacy_memory_queue

    queue = active_or_legacy_memory_queue(context["root"], context["state_root"])
    changed = queue.cancel(
        str(context["target_id"]),
        deadline=context["deadline"],
        cancelled=context["cancelled"],
    )
    if not changed:
        return _operator_result(
            "queue-cancel",
            states=["error"],
            codes=["unknown_or_terminal_task"],
            overall_status="error",
        )
    return _operator_result(
        "queue-cancel", ids=[str(context["target_id"])], states=["cancelled"]
    )


def _redrive_error_code(error) -> str:
    if str(error) == "redrive_requires_dead":
        return str(error)
    return "redrive_invalid"


def _doctor_queue_redrive(context: dict) -> dict:
    from memory_queue import QueueOperationError, active_or_legacy_memory_queue

    queue = active_or_legacy_memory_queue(context["root"], context["state_root"])
    try:
        replacement = queue.redrive(
            str(context["target_id"]),
            deadline=context["deadline"],
            cancelled=context["cancelled"],
        )
    except KeyError:
        return _operator_result(
            "queue-redrive",
            states=["error"],
            codes=["unknown_task"],
            overall_status="error",
        )
    except QueueOperationError as error:
        return _operator_result(
            "queue-redrive",
            states=["error"],
            codes=[_redrive_error_code(error)],
            overall_status="error",
        )
    return _operator_result("queue-redrive", ids=[replacement], states=["ready"])


def _transaction_result(action: str, records) -> dict:
    return _operator_result(
        action,
        ids=[record.id for record in records],
        states=[record.state for record in records],
        codes=[record.error_code for record in records if record.error_code],
    )


def _transaction_coordinator(context: dict):
    from markdown_transaction import active_or_legacy_coordinator

    return active_or_legacy_coordinator(context["root"], context["state_root"])


def _doctor_transaction_recover(context: dict) -> dict:
    records = _transaction_coordinator(context).recover(
        max_transactions=context["limit"],
        deadline=context["deadline"],
        cancelled=context["cancelled"],
    )
    return _transaction_result("transaction-recover", records)


def _doctor_transaction_undo(context: dict) -> dict:
    coordinator = _transaction_coordinator(context)
    prepared = coordinator.undo(
        str(context["target_id"]),
        deadline=context["deadline"],
        cancelled=context["cancelled"],
    )
    record = coordinator.apply(
        prepared.id, deadline=context["deadline"], cancelled=context["cancelled"]
    )
    return _transaction_result("transaction-undo", [record])


def _check_counts(details: dict) -> dict:
    return {
        key: value
        for key, value in details.items()
        if isinstance(value, int) and not isinstance(value, bool)
    }


def _check_result(action: str, check: dict) -> dict:
    details = check["details"]
    return _operator_result(
        action,
        states=[str(check["status"])],
        codes=[str(code) for code in details.get("codes", [])],
        counts=_check_counts(details),
    )


def _doctor_archive_status(context: dict) -> dict:
    from doctor import _archive_check

    check = _archive_check(context["root"], context["state_root"], context["deadline"])
    return _check_result("archive-status", check)


def _doctor_claim_status(context: dict) -> dict:
    from doctor import _claim_check

    check = _claim_check(context["root"], context["state_root"], context["deadline"])
    return _check_result("claim-status", check)


_DOCTOR_ACTIONS = {
    "status": _doctor_status,
    "queue-inspect": _doctor_queue_read,
    "queue-dead-list": _doctor_queue_read,
    "queue-cancel": _doctor_queue_cancel,
    "queue-redrive": _doctor_queue_redrive,
    "transaction-recover": _doctor_transaction_recover,
    "transaction-undo": _doctor_transaction_undo,
    "archive-status": _doctor_archive_status,
    "claim-status": _doctor_claim_status,
}


def _doctor(
    *,
    action: str,
    target_id: str | None = None,
    limit: int = 20,
    repair: bool = False,
    deadline: float | None = None,
) -> dict:
    """Dispatch bounded health and recovery actions with redacted output."""
    from memory_state import ROOT, STATE_ROOT

    if action in _DOCTOR_MUTATIONS and repair is not True:
        return _operator_result(
            action, states=["error"], codes=["repair_required"], overall_status="error"
        )
    handler = _DOCTOR_ACTIONS.get(action)
    if handler is None:
        return _operator_result(
            action, states=["error"], codes=["unknown_action"], overall_status="error"
        )
    context = {
        "action": action,
        "target_id": target_id,
        "limit": limit,
        "root": ROOT,
        "state_root": STATE_ROOT,
        "deadline": _operation_deadline(deadline),
        "cancelled": _operation_cancelled(),
    }
    try:
        return handler(context)
    except Exception as error:  # noqa: BLE001 - stable operator failure boundary
        return _operator_result(
            action,
            states=["error"],
            codes=[_doctor_failure_code(error)],
            overall_status="error",
        )


def _validated_code_directory(
    directory: str, *, deadline: float | None = None
) -> tuple[Path | None, str | None]:
    """Validate an explicitly supplied, bounded local project directory."""
    if not isinstance(directory, str) or not directory.strip():
        return None, "directory is required"
    _check_deadline(deadline)
    candidate = Path(directory).expanduser()
    _check_deadline(deadline)
    if not candidate.is_absolute():
        return None, "directory must be an absolute local path"
    resolved = candidate.resolve()
    _check_deadline(deadline)
    error = _code_directory_error(resolved)
    _check_deadline(deadline)
    return (None, error) if error else (resolved, None)


def _code_directory_error(resolved: Path) -> str | None:
    reasons = (
        (not resolved.exists(), f"directory does not exist: {resolved}"),
        (not resolved.is_dir(), f"directory is not a directory: {resolved}"),
        (resolved == Path(resolved.anchor), "directory must not be a filesystem root"),
    )
    for failed, message in reasons:
        if failed:
            return message
    return None


def _build_tool_definitions() -> list:
    """Build MCP tool definitions."""
    if not MCP_AVAILABLE:
        return []

    return [
        _make_tool(
            name="recall",
            description="Search the knowledge vault. Returns ranked results with titles, summaries, and paths. Use this to find relevant knowledge pages.",
            inputSchema=TOOL_INPUT_SCHEMAS["recall"],
        ),
        _make_tool(
            name="read_page",
            description="Read a full knowledge page by its slug (filename without .md). Use this when you need the complete content of a specific page.",
            inputSchema=TOOL_INPUT_SCHEMAS["read_page"],
        ),
        _make_tool(
            name="wiki_overview",
            description="Get vault statistics: page count, retrieval tier recommendation, vault root path. Call this first in any session.",
            inputSchema=TOOL_INPUT_SCHEMAS["wiki_overview"],
        ),
        _make_tool(
            name="vault_status",
            description="Get compile status and current daily-file backlog.",
            inputSchema=TOOL_INPUT_SCHEMAS["vault_status"],
        ),
        _make_tool(
            name="get_decisions",
            description="Find active architectural decisions in the vault. Optionally filter by query.",
            inputSchema=TOOL_INPUT_SCHEMAS["get_decisions"],
        ),
        _make_tool(
            name="get_context",
            description="Get context for multiple pages at once. Batch operation for efficiency.",
            inputSchema=TOOL_INPUT_SCHEMAS["get_context"],
        ),
        _make_tool(
            name="check_contradiction",
            description="Check if a claim contradicts existing knowledge. Returns potentially conflicting pages.",
            inputSchema=TOOL_INPUT_SCHEMAS["check_contradiction"],
        ),
        _make_tool(
            name="log_decision",
            description="Log a new decision to the daily log. Triggers compile on next session.",
            inputSchema=TOOL_INPUT_SCHEMAS["log_decision"],
        ),
        _make_tool(
            name="compile",
            description="Compile pending daily logs into knowledge pages within the operation deadline.",
            inputSchema=TOOL_INPUT_SCHEMAS["compile"],
        ),
        _make_tool(
            name="find_dead_code",
            description="Find functions with zero confirmed incoming calls. Results are conservative candidates because static call graphs are incomplete.",
            inputSchema=TOOL_INPUT_SCHEMAS["find_dead_code"],
        ),
        _make_tool(
            name="get_architecture",
            description="Summarize statically visible entry points, framework routes, incoming-caller hotspots, and code communities.",
            inputSchema=TOOL_INPUT_SCHEMAS["get_architecture"],
        ),
        _make_tool(
            name="doctor",
            description="Check local vault health. Read-only unless repair is explicitly true.",
            inputSchema=TOOL_INPUT_SCHEMAS["doctor"],
        ),
    ]


def _make_tool(**kwargs):
    """Create a tool with structured output metadata when the SDK supports it."""
    if MCP_STRUCTURED_OUTPUT_AVAILABLE:
        kwargs["outputSchema"] = envelope_schema()
    return Tool(**kwargs)


def _meta(*, deadline: float | None = None) -> dict:
    """Build the legacy payload metadata retained inside envelope data."""
    from datetime import datetime

    from lookup_mode import count_wiki_pages
    from memory_state import load_state

    _check_deadline(deadline)
    state = load_state()
    _check_deadline(deadline)
    return {
        "page_count": count_wiki_pages(),
        "last_compile": state.get("last_compile_at", "never"),
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }


def _validate_tool_arguments(name: str, arguments) -> str | None:
    """Validate the small declared schema subset before helper dispatch."""
    if not isinstance(arguments, dict):
        return "arguments must be an object"
    schema = TOOL_INPUT_SCHEMAS[name]
    if "oneOf" in schema:
        return _validate_one_of(schema, arguments)
    return _validate_object_then_specific(name, schema, arguments)


def _validate_object_then_specific(name: str, schema: dict, arguments: dict):
    """The declared object schema first, then the tool's own argument rules."""
    error = _validate_object_schema(
        schema,
        arguments,
        reject_unknown=name == "get_architecture",
    )
    if error is not None:
        return error
    return _validate_tool_specific_arguments(name, arguments)


def _validate_one_of(schema: dict, arguments: dict) -> str | None:
    errors = [
        _validate_object_schema(branch, arguments) for branch in schema["oneOf"]
    ]
    if sum(error is None for error in errors) == 1:
        return None
    return "arguments do not match exactly one allowed action"


def _validate_recall_arguments(arguments: dict) -> str | None:
    if "profile" in arguments and arguments.get("grounded") is not True:
        return "argument 'profile' requires grounded=true"
    return None


def _validate_tool_specific_arguments(name: str, arguments: dict) -> str | None:
    if name == "recall":
        return _validate_recall_arguments(arguments)
    if name == "get_architecture":
        return _validate_architecture_arguments(arguments)
    return None


_ARCHITECTURE_POSITION_KEYS = ("path", "line", "character")

_ARCHITECTURE_CONTRACTS = {
    "summary": ({"directory"}, {"directory", "mode", "live"}),
    "symbol": (
        {"directory", "mode", "symbol"},
        {"directory", "mode", "symbol", "live"},
    ),
    "callers": (
        {"directory", "mode", "symbol"},
        {"directory", "mode", "symbol", "live"},
    ),
    "callees": (
        {"directory", "mode", "symbol"},
        {"directory", "mode", "symbol", "live"},
    ),
    "dependencies": (
        {"directory", "mode", "symbol"},
        {"directory", "mode", "symbol", "reverse", "live"},
    ),
    "path": (
        {"directory", "mode", "symbol", "target"},
        {"directory", "mode", "symbol", "target", "live"},
    ),
    "community": (
        {"directory", "mode"},
        {"directory", "mode", "live"},
    ),
    "provenance": (
        {"directory", "mode", "symbol"},
        {"directory", "mode", "symbol"},
    ),
    "snippet": (
        {"directory", "mode", "symbol"},
        {"directory", "mode", "symbol"},
    ),
    "coverage": (
        {"directory", "mode", "path"},
        {"directory", "mode", "path"},
    ),
    "query": (
        {"directory", "mode", "query"},
        {"directory", "mode", "query"},
    ),
    "impact": (
        {"directory", "mode"},
        {"directory", "mode", "comparison", "base", "target", "branch"},
    ),
}


def _positioned_architecture_arguments(arguments: dict, mode: str) -> bool:
    if mode not in {"callers", "callees"}:
        return False
    return any(
        key in arguments
        for key in (*_ARCHITECTURE_POSITION_KEYS, "offset", "limit")
    )


def _architecture_contract(mode: str, positioned: bool) -> tuple[set, set]:
    if mode in PRECISE_ARCHITECTURE_MODES or positioned:
        required = {"directory", "mode", *_ARCHITECTURE_POSITION_KEYS}
        return required, {*required, "offset", "limit"}
    return _ARCHITECTURE_CONTRACTS[mode]


def _missing_architecture_message(mode: str, missing: list, positioned: bool) -> str:
    if positioned:
        return (
            f"positioned {mode} require path, line, and character together; "
            f"missing: {', '.join(missing)}"
        )
    return f"required arguments are missing for {mode}: {', '.join(missing)}"


def _architecture_path_error(arguments: dict) -> str | None:
    try:
        from lsp_security import validate_repository_relative_path

        validate_repository_relative_path(arguments["path"])
    except (TypeError, ValueError):
        return "argument 'path' must be a canonical repository-relative path"
    return None


def _validate_architecture_arguments(arguments: dict) -> str | None:
    mode = arguments.get("mode", "summary")
    positioned = _positioned_architecture_arguments(arguments, mode)
    required, allowed = _architecture_contract(mode, positioned)
    missing = sorted(required.difference(arguments))
    if missing:
        return _missing_architecture_message(mode, missing, positioned)
    # CODE-06: shaping the answer is orthogonal to what a mode asks the graph,
    # so the two budget arguments are allowed on every mode rather than being
    # copied into thirteen closed key sets that would drift apart.
    forbidden = sorted(set(arguments).difference(allowed, ANSWER_BUDGET_SCHEMA_FIELDS))
    if forbidden:
        return f"arguments are not valid for {mode}: {', '.join(forbidden)}"
    return _positioned_architecture_path_error(arguments, mode, positioned)


def _positioned_architecture_path_error(arguments: dict, mode: str, positioned: bool):
    """Only a precise or positioned call carries a path to validate."""
    if mode in PRECISE_ARCHITECTURE_MODES or positioned:
        return _architecture_path_error(arguments)
    return None


def _is_string_value(value) -> bool:
    return isinstance(value, str)


def _is_integer_value(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_boolean_value(value) -> bool:
    return isinstance(value, bool)


def _is_array_value(value) -> bool:
    return isinstance(value, list)


_SCHEMA_TYPE_CHECKS = {
    "string": (_is_string_value, "a string"),
    "integer": (_is_integer_value, "an integer"),
    "boolean": (_is_boolean_value, "a boolean"),
    "array": (_is_array_value, "an array"),
}


def _validate_field_type(expected: str, key: str, value) -> str | None:
    check = _SCHEMA_TYPE_CHECKS.get(expected)
    if check is None:
        return None
    predicate, description = check
    if predicate(value):
        return None
    return f"argument '{key}' must be {description}"


def _validate_array_item_type(items: dict, key: str, value: list) -> str | None:
    if items.get("type") != "string":
        return None
    if all(isinstance(item, str) for item in value):
        return None
    return f"argument '{key}' items must be strings"


def _validate_array_length(field: dict, key: str, value: list) -> str | None:
    if "minItems" in field and len(value) < field["minItems"]:
        return f"argument '{key}' has too few items"
    if "maxItems" in field and len(value) > field["maxItems"]:
        return f"argument '{key}' has too many items"
    return None


def _validate_array_item_length(items: dict, key: str, value: list) -> str | None:
    item_max_length = items.get("maxLength")
    if item_max_length is None:
        return None
    if all(len(item) <= item_max_length for item in value):
        return None
    return f"argument '{key}' item is too long"


def _validate_array_items(items: dict, field: dict, key: str, value: list) -> str | None:
    if field.get("uniqueItems") and len(set(value)) != len(value):
        return f"argument '{key}' items must be unique"
    return _validate_array_item_length(items, key, value)


def _validate_array_field(field: dict, key: str, value: list) -> str | None:
    items = field.get("items", {})
    error = _validate_array_item_type(items, key, value)
    if error is not None:
        return error
    error = _validate_array_length(field, key, value)
    if error is not None:
        return error
    return _validate_array_items(items, field, key, value)


def _validate_numeric_bounds(field: dict, key: str, value) -> str | None:
    if "minimum" in field and value < field["minimum"]:
        return f"argument '{key}' must be at least {field['minimum']}"
    if "maximum" in field and value > field["maximum"]:
        return f"argument '{key}' must be at most {field['maximum']}"
    return None


def _validate_length_bounds(field: dict, key: str, value) -> str | None:
    if "minLength" in field and len(value) < field["minLength"]:
        return f"argument '{key}' is too short"
    if "maxLength" in field and len(value) > field["maxLength"]:
        return f"argument '{key}' is too long"
    return None


def _validate_value_choice(field: dict, key: str, value) -> str | None:
    if "const" in field and value != field["const"]:
        return f"argument '{key}' has an invalid value"
    if "enum" in field and value not in field["enum"]:
        return f"argument '{key}' has an invalid value"
    return None


def _validate_field_bounds(field: dict, key: str, value) -> str | None:
    error = _validate_numeric_bounds(field, key, value)
    if error is not None:
        return error
    error = _validate_length_bounds(field, key, value)
    if error is not None:
        return error
    return _validate_value_choice(field, key, value)


def _unknown_argument_error(schema: dict, key: str, reject_unknown: bool) -> str | None:
    if reject_unknown or schema.get("additionalProperties") is False:
        return f"unknown argument: {key}"
    return None


def _validate_schema_field(
    schema: dict, key: str, value, reject_unknown: bool
) -> str | None:
    field = schema["properties"].get(key)
    if field is None:
        return _unknown_argument_error(schema, key, reject_unknown)
    error = _validate_field_type(field["type"], key, value)
    if error is not None:
        return error
    return _validate_typed_field(field, key, value)


def _validate_typed_field(field: dict, key: str, value) -> str | None:
    """Bounds for every field, preceded by the item rules an array declares."""
    if field["type"] == "array":
        return _validate_array_field(field, key, value) or _validate_field_bounds(
            field, key, value
        )
    return _validate_field_bounds(field, key, value)


def _missing_required_argument(schema: dict, arguments: dict) -> str | None:
    for key in schema["required"]:
        if key not in arguments:
            return f"required argument is missing: {key}"
    return None


def _validate_object_schema(
    schema: dict, arguments: dict, *, reject_unknown: bool = True
) -> str | None:
    missing = _missing_required_argument(schema, arguments)
    if missing is not None:
        return missing
    for key, value in arguments.items():
        error = _validate_schema_field(schema, key, value, reject_unknown)
        if error is not None:
            return error
    return None


def _clamped_limit(value: int) -> tuple[int, bool]:
    effective = min(20, max(1, value))
    return effective, effective != value


def _degrade_quality(
    quality: dict,
    warning: str,
    *,
    coverage: float,
    confidence: float,
) -> dict:
    quality = quality.copy()
    quality["coverage"] = min(quality.get("coverage", 1.0), coverage)
    quality["confidence"] = min(quality.get("confidence", 1.0), confidence)
    quality["partial"] = True
    warnings = list(quality.get("warnings", []))
    if warning not in warnings:
        warnings.append(warning)
    quality["warnings"] = warnings
    return quality


def _record_tool_failure(operation: str, error: BaseException) -> None:
    """Leave the operator a trace of a failure the caller only sees a code for.

    The agent is told `operation_failed` and nothing else on purpose: exception
    text is untrusted and can carry paths or secrets. Dropping it entirely left
    nobody able to find out why, so it goes to the bounded, redacted failure
    trail that doctor already counts, under its own kind.
    """
    try:
        from capture_diagnostics import record_capture_failure

        record_capture_failure(
            "mcp_tool", f"{operation}: {type(error).__name__}: {error}"
        )
    except Exception:  # noqa: BLE001 - diagnostics never break a tool call
        pass


def _safe_exception_text(error: BaseException, operation: str = "mcp") -> str:
    """Return a bounded stable code without exposing untrusted exception details."""
    _record_tool_failure(operation, error)
    code = "operation_timeout" if isinstance(error, TimeoutError) else "operation_failed"
    return " ".join(redact_secrets(code).split())[:MAX_MCP_ERROR_CHARS]


def _safe_page_read_error(error: BaseException) -> str:
    message = " ".join(str(error).split())
    allowed = {
        f"MCP page exceeds {MAX_MCP_PAGE_BYTES} bytes",
        "MCP page parent must be a regular directory",
        "MCP page must be a regular non-symlink file",
        "MCP page changed before open",
        "MCP page changed during read",
        "MCP page was replaced during read",
    }
    return message if message in allowed else _safe_exception_text(error)


def _safe_evidence_error(error: BaseException) -> str:
    message = " ".join(str(error).split())
    allowed = {
        f"evidence slice exceeds {MAX_MCP_EVIDENCE_BYTES} bytes",
        f"total evidence exceeds {MAX_MCP_TOTAL_EVIDENCE_BYTES} bytes",
    }
    return message if message in allowed else _safe_exception_text(error)


_ABSOLUTE_PATH = re.compile(
    r"(?<![\w])(?:[A-Za-z]:[\\/](?:[^\\/\s]+[\\/])*[^\\/\s]+|/(?:[^/\s]+/)+[^/\s]+)"
)
_NAVIGATION_REDACTION_MARKER = re.compile(
    r"\[(?:PATH|REDACTED(?:_[A-Z_]+)?)\]"
)


def _sanitized_diagnostic_text(value: str) -> str:
    text = _ABSOLUTE_PATH.sub("[REDACTED_PATH]", redact_secrets(value))
    return " ".join(text.split())[:MAX_MCP_ERROR_CHARS]


def _sanitized_diagnostic_list(value: list) -> list:
    return [_sanitize_diagnostic(item) for item in value]


def _sanitized_diagnostic_mapping(value: dict) -> dict:
    return {key: _sanitize_diagnostic(item) for key, item in value.items()}


def _sanitize_diagnostic(value):
    handlers = (
        (str, _sanitized_diagnostic_text),
        (list, _sanitized_diagnostic_list),
        (dict, _sanitized_diagnostic_mapping),
    )
    for kind, handler in handlers:
        if isinstance(value, kind):
            return handler(value)
    return value


def _sanitized_error_value(key: str, item):
    if key == "error":
        return _sanitize_diagnostic(item)
    return _sanitize_error_fields(item)


def _sanitize_error_fields(value):
    if isinstance(value, list):
        return [_sanitize_error_fields(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _sanitized_error_value(key, item) for key, item in value.items()
        }
    return value


def _sanitize_navigation_text(value: str) -> str:
    sanitized = redact_secrets(value)
    sanitized = _ABSOLUTE_PATH.sub("[PATH]", sanitized)
    original_bytes = len(value.encode("utf-8", errors="strict"))
    if len(sanitized.encode("utf-8", errors="strict")) <= original_bytes:
        return sanitized
    fitted = []
    used = 0
    index = 0
    while index < len(sanitized):
        marker = _NAVIGATION_REDACTION_MARKER.match(sanitized, index)
        token = marker.group() if marker is not None else sanitized[index]
        size = len(token.encode("utf-8", errors="strict"))
        if used + size > original_bytes:
            break
        fitted.append(token)
        used += size
        index += len(token)
    return "".join(fitted)


def _sanitize_navigation_location(value):
    if not isinstance(value, dict):
        return value
    sanitized = dict(value)
    signature = sanitized.get("signature")
    if isinstance(signature, str):
        sanitized["signature"] = _sanitize_navigation_text(signature)
    return sanitized


def _sanitized_navigation_warning(item):
    if isinstance(item, str):
        return _sanitize_navigation_text(item)
    return item


def _sanitize_navigation_warnings(sanitized: dict) -> None:
    warnings = sanitized.get("warnings")
    if not isinstance(warnings, (list, tuple)):
        return
    values = tuple(_sanitized_navigation_warning(item) for item in warnings)
    sanitized["warnings"] = values if isinstance(warnings, tuple) else list(values)


def _sanitize_navigation_hover(sanitized: dict) -> None:
    hover = sanitized.get("hover")
    if isinstance(hover, str):
        sanitized["hover"] = _sanitize_navigation_text(hover)


def _sanitized_navigation_group(group):
    if not isinstance(group, dict):
        return group
    sanitized_group = dict(group)
    locations = group.get("locations")
    if isinstance(locations, list):
        sanitized_group["locations"] = [
            _sanitize_navigation_location(location) for location in locations
        ]
    return sanitized_group


def _sanitize_navigation_groups(sanitized: dict) -> None:
    groups = sanitized.get("groups")
    if isinstance(groups, list):
        sanitized["groups"] = [_sanitized_navigation_group(item) for item in groups]


def _sanitize_diagnostic_text_fields(diagnostic: dict, sanitized: dict) -> None:
    for field in ("message", "code"):
        value = diagnostic.get(field)
        if isinstance(value, str):
            sanitized[field] = _sanitize_navigation_text(value)


def _sanitized_navigation_diagnostic(diagnostic):
    if not isinstance(diagnostic, dict):
        return diagnostic
    sanitized = dict(diagnostic)
    _sanitize_diagnostic_text_fields(diagnostic, sanitized)
    related = diagnostic.get("related")
    if isinstance(related, list):
        sanitized["related"] = [
            _sanitize_navigation_location(location) for location in related
        ]
    return sanitized


def _sanitize_navigation_diagnostics(sanitized: dict) -> None:
    diagnostics = sanitized.get("diagnostics")
    if isinstance(diagnostics, list):
        sanitized["diagnostics"] = [
            _sanitized_navigation_diagnostic(item) for item in diagnostics
        ]


def _sanitize_navigation_data(data: dict) -> dict:
    sanitized = dict(data)
    _sanitize_navigation_warnings(sanitized)
    _sanitize_navigation_hover(sanitized)
    _sanitize_navigation_groups(sanitized)
    _sanitize_navigation_diagnostics(sanitized)
    return sanitized


_NAVIGATION_RENDER_KEYS = frozenset(
    {"requested_capability", "repository", "groups", "diagnostics"}
)


def _is_rendered_navigation_data(name: str, data) -> bool:
    if name != "get_architecture" or not isinstance(data, dict):
        return False
    if not isinstance(data.get("status"), str):
        return False
    return _NAVIGATION_RENDER_KEYS.issubset(data)


def _quality_for(
    name: str,
    data,
    arguments: dict | None = None,
    *,
    limit_clamped: bool = False,
) -> dict:
    """Infer conservative operation quality only from returned evidence.

    The rules are tried in order and the first one that recognises the result
    answers; order is behaviour, because `get_architecture` is claimed by the
    impact rule before the code-graph rule ever sees it.
    """
    for rule in _QUALITY_RULES:
        quality = rule(name, data, arguments, limit_clamped)
        if quality is not None:
            return quality
    return {}


def _quality_of_error(name, data, arguments, limit_clamped) -> dict | None:
    if not isinstance(data, dict) or "error" not in data:
        return None
    return {
        "coverage": 0.0,
        "confidence": 0.2,
        "partial": True,
        "warnings": [str(data["error"])],
    }


def _is_grounded_recall(name: str, arguments: dict | None) -> bool:
    if name != "recall" or not arguments:
        return False
    return arguments.get("grounded") is True


def _citation_ids(citations) -> set:
    identifiers = set()
    for citation in citations:
        if isinstance(citation, dict) and citation.get("citation_id"):
            identifiers.add(citation.get("citation_id"))
    return identifiers


def _is_verified_claim(claim: object, citation_ids: set) -> bool:
    if not isinstance(claim, dict) or not claim.get("citation_ids"):
        return False
    return set(claim["citation_ids"]).issubset(citation_ids)


def _verified_claim_ratio(data: dict) -> float:
    """The share of claims whose citations all came back with the answer."""
    claims = data.get("claims", [])
    citation_ids = _citation_ids(data.get("citations", []))
    verified = sum(1 for claim in claims if _is_verified_claim(claim, citation_ids))
    return verified / len(claims) if claims else 0.0


# The ceiling the grounded path has always applied: a verified citation says the
# span exists and shares content with the claim, never that it entails it.
GROUNDED_CONFIDENCE_CEILING = 0.8


def _cited_paths(data: dict) -> list[str]:
    citations = [item for item in data.get("citations", []) if isinstance(item, dict)]
    return sorted({str(item.get("relative_path") or "") for item in citations})


def _cited_page_facts(data: dict) -> list:
    """What the vault knows about each page this answer cites."""
    from memory_state import ROOT
    from page_facts import read_page_facts

    facts = [read_page_facts(ROOT, path) for path in _cited_paths(data)]
    return [item for item in facts if item is not None]


def _weakest_stated_confidence(facts: list) -> float:
    """A chain of claims is worth its weakest evidence, not its average."""
    from page_facts import UNSTATED_CONFIDENCE

    if not facts:
        return UNSTATED_CONFIDENCE
    return min(item.stated_confidence() for item in facts)


def _page_warnings(item) -> list[str]:
    aging = (
        f"{item.relative_path} is {item.age_days} days old; a {item.page_type} "
        f"page stays current for {item.age_limit_days}."
    )
    candidates = (
        (item.confidence == "low", f"{item.relative_path} states confidence: low."),
        (
            item.authority == "inferred",
            f"{item.relative_path} is inferred, not stated by anyone.",
        ),
        (bool(item.aging), aging),
    )
    return [message for flagged, message in candidates if flagged]


def _provenance_warnings(facts: list) -> list[str]:
    warnings: list[str] = []
    for item in facts:
        warnings.extend(_page_warnings(item))
    return warnings


def _grounded_answer_quality(data: dict) -> dict:
    """Coverage and confidence from the answer and its sources, not from a constant.

    Coverage is the share of claims whose citations all came back verified.
    Confidence multiplies that by the weakest cited page's own stated
    confidence, so an answer resting on a `confidence: low` page cannot report
    more than that page does. Warnings name the page and the reason; a warning
    that appears on every answer is one nobody reads.
    """
    ratio = _verified_claim_ratio(data)
    facts = _cited_page_facts(data)
    confidence = min(
        GROUNDED_CONFIDENCE_CEILING, ratio * _weakest_stated_confidence(facts)
    )
    return {
        "coverage": round(ratio, 4),
        "confidence": round(confidence, 4),
        "partial": ratio < 1.0,
        "warnings": _provenance_warnings(facts),
    }


def _grounded_abstention_quality(data) -> dict:
    reason = data.get("reason") if isinstance(data, dict) else None
    return {
        "coverage": 0.0,
        "confidence": 0.0,
        "partial": True,
        "warnings": [str(reason or "Grounded QA abstained without a reason.")],
    }


def _quality_of_grounded_recall(name, data, arguments, limit_clamped) -> dict | None:
    if not _is_grounded_recall(name, arguments):
        return None
    if isinstance(data, dict) and data.get("status") == "answered":
        return _grounded_answer_quality(data)
    return _grounded_abstention_quality(data)


def _no_results_quality() -> dict:
    """A fresh dict every time: `_degrade_quality` writes into what it is given."""
    return {
        "coverage": 0.1,
        "confidence": 0.3,
        "fallback": True,
        "partial": True,
        "warnings": ["Search returned no results; retrieval coverage is unknown."],
    }


def _bm25_only_quality() -> dict:
    return {
        "coverage": 0.6,
        "confidence": 0.6,
        "fallback": True,
        "partial": True,
        "warnings": ["Only BM25 retrieval evidence is available."],
    }


def _carries_score(result: object) -> bool:
    if not isinstance(result, dict):
        return False
    return any(key in result for key in ("fused_score", "vector_score"))


def _has_fused_scores(results) -> bool:
    return any(_carries_score(result) for result in results)


def _contradiction_candidate_quality(data: list) -> dict:
    if not data:
        return _degrade_quality(
            _no_results_quality(),
            "Contradiction candidates are unverified.",
            coverage=0.6,
            confidence=0.45,
        )
    quality = (
        {"coverage": 0.9, "confidence": 0.8}
        if _has_fused_scores(data)
        else _bm25_only_quality()
    )
    return _degrade_quality(
        quality,
        "Contradiction candidates are unverified.",
        coverage=0.6,
        confidence=0.45,
    )


def _is_verified_validity(validity: object) -> bool:
    if not isinstance(validity, dict):
        return False
    return validity.get("status") == "verified"


def _contradiction_claim_quality(data) -> dict:
    fields = data if isinstance(data, dict) else {}
    evidence = fields.get("evidence", [])
    if _is_verified_validity(fields.get("validity", {})):
        return {
            "coverage": 0.9 if evidence else 0.7,
            "confidence": 0.9,
            "partial": False,
            "warnings": [],
        }
    return {
        "coverage": 0.5 if evidence else 0.2,
        "confidence": 0.35,
        "fallback": True,
        "partial": True,
        "warnings": ["Claim evidence is unsupported; recommendations are quarantined."],
    }


def _quality_of_contradiction(name, data, arguments, limit_clamped) -> dict | None:
    if name != "check_contradiction":
        return None
    if isinstance(data, list):
        return _contradiction_candidate_quality(data)
    return _contradiction_claim_quality(data)


def _results_quality(results) -> dict:
    if not isinstance(results, list) or not results:
        return _no_results_quality()
    if not _has_fused_scores(results):
        return _bm25_only_quality()
    return {"coverage": 0.9, "confidence": 0.8}


def _quality_of_results(name, data, arguments, limit_clamped) -> dict | None:
    if name not in {"recall", "get_decisions"}:
        return None
    results = data.get("results", []) if name == "recall" else data
    quality = _results_quality(results)
    if not limit_clamped:
        return quality
    return _degrade_quality(
        quality,
        "Requested limit was clamped to the safe 1-20 range.",
        coverage=0.8,
        confidence=0.8,
    )


def _impact_warnings(data, fallback: str) -> list:
    return list(data.get("warnings", [])) or [fallback]


def _impact_quality(data) -> dict:
    classification = data.get("classification") if isinstance(data, dict) else None
    if classification == "exact":
        return {"coverage": 0.9, "confidence": 0.9}
    if classification == "conservative":
        return {
            "coverage": 0.6,
            "confidence": 0.55,
            "partial": True,
            "warnings": _impact_warnings(data, "Impact analysis is conservative."),
        }
    return {
        "coverage": 0.2,
        "confidence": 0.25,
        "partial": True,
        "warnings": _impact_warnings(data, "Impact analysis is unresolved."),
    }


def _quality_of_impact(name, data, arguments, limit_clamped) -> dict | None:
    if name != "get_architecture" or not arguments:
        return None
    if arguments.get("mode") != "impact":
        return None
    return _impact_quality(data)


def _quality_of_navigation(name, data, arguments, limit_clamped) -> dict | None:
    if not _is_rendered_navigation_data(name, data):
        return None
    partial = data["status"] != "ok"
    return {
        "coverage": 0.5 if partial else 0.9,
        "confidence": 0.4 if partial else 0.85,
        "fallback": False,
        "partial": partial,
        "warnings": list(data.get("warnings", ())),
    }


def _active_graph_quality(data: dict) -> dict:
    unresolved = data.get("unresolved_count")
    if data.get("graph_complete") is True and unresolved == 0:
        return {
            "coverage": 0.95,
            "confidence": 0.9,
            "fallback": False,
            "partial": False,
            "warnings": [],
        }
    return {
        "coverage": 0.8,
        "confidence": 0.75,
        "fallback": False,
        "partial": True,
        "warnings": [f"Active code graph has {unresolved} unresolved observations."],
    }


def _quality_of_code_graph(name, data, arguments, limit_clamped) -> dict | None:
    if name not in {"find_dead_code", "get_architecture"}:
        return None
    if not isinstance(data, dict) or data.get("fallback") is not False:
        return {
            "coverage": 0.6,
            "confidence": 0.55,
            "fallback": True,
            "partial": True,
            "warnings": ["Live static extraction fallback is incomplete."],
        }
    return _active_graph_quality(data)


def _doctor_warning(prefix: str, data) -> str:
    codes = data.get("codes", []) if isinstance(data, dict) else []
    if not codes:
        return f"{prefix}."
    return f"{prefix}: {', '.join(str(code) for code in codes[:3])}"


def _unknown_doctor_quality(data) -> dict:
    from doctor import degraded_summary

    return {
        "coverage": 0.75,
        "confidence": 0.75,
        "partial": True,
        "warnings": [degraded_summary(data) or "Doctor health is unknown."],
    }


_DOCTOR_QUALITY = {
    "ok": lambda data: {"coverage": 0.9, "confidence": 0.85},
    "error": lambda data: {
        "coverage": 0.2,
        "confidence": 0.3,
        "partial": True,
        "warnings": [_doctor_warning("Doctor operator failed", data)],
    },
    "degraded": lambda data: {
        "coverage": 0.7,
        "confidence": 0.7,
        "partial": True,
        "warnings": [_doctor_warning("Doctor status is degraded", data)],
    },
}


def _quality_of_doctor(name, data, arguments, limit_clamped) -> dict | None:
    if name != "doctor":
        return None
    overall = data.get("overall_status") if isinstance(data, dict) else None
    build = _DOCTOR_QUALITY.get(overall)
    if build is None:
        return _unknown_doctor_quality(data)
    return build(data)


def _is_context_request(name: str, arguments: dict | None) -> bool:
    if name != "get_context":
        return False
    return bool(arguments)


def _missing_slugs(data) -> list:
    if not isinstance(data, dict):
        return []
    return data.get("missing_slugs", [])


def _context_quality(requested, missing) -> dict | None:
    if not requested or not missing:
        return None
    return {
        "coverage": max(0.0, (len(requested) - len(missing)) / len(requested)),
        "confidence": 0.8,
        "partial": True,
        "warnings": ["Some requested pages were unavailable."],
    }


def _quality_of_context(name, data, arguments, limit_clamped) -> dict | None:
    if not _is_context_request(name, arguments):
        return None
    return _context_quality(arguments.get("slugs", []), _missing_slugs(data))


# Order is behaviour: the first rule that recognises the result answers.
_QUALITY_RULES = (
    _quality_of_error,
    _quality_of_grounded_recall,
    _quality_of_contradiction,
    _quality_of_results,
    _quality_of_impact,
    _quality_of_navigation,
    _quality_of_code_graph,
    _quality_of_doctor,
    _quality_of_context,
)


def _resource_quality(data) -> dict:
    if isinstance(data, dict) and "error" in data:
        return _quality_for("resource", data)
    return _compile_health_quality(_resource_status(data))


def _resource_status(data) -> dict:
    status = data.get("status", data) if isinstance(data, dict) else {}
    if not isinstance(status, dict):
        return {}
    return status


def _backlog_quality(backlog) -> dict:
    if isinstance(backlog, (int, float)) and backlog > 0:
        return {
            "coverage": 0.7,
            "confidence": 0.7,
            "partial": True,
            "warnings": [f"Compile backlog contains {backlog} daily file(s)."],
        }
    return {"coverage": 0.9, "confidence": 0.85}


def _compile_health_quality(status: dict) -> dict:
    compile_status = status.get("last_compile_status", "unknown")
    last_compile = status.get("last_compile", "never")
    if compile_status in {None, "unknown"} or last_compile in {None, "never"}:
        return {
            "coverage": 0.4,
            "confidence": 0.5,
            "partial": True,
            "warnings": ["Compile health is unknown."],
        }
    if compile_status not in {"ok", "success"}:
        return {
            "coverage": 0.6,
            "confidence": 0.6,
            "partial": True,
            "warnings": [f"Compile status is {compile_status}."],
        }
    return _backlog_quality(status.get("compile_backlog", 0))


def _build_operation_envelope(
    data,
    quality: dict | None = None,
    *,
    components: dict[str, dict[str, object]] | None = None,
) -> dict:
    envelope = build_envelope(data, components=components, **(quality or {}))
    if components and envelope["freshness"] == "stale":
        _degrade_stale_envelope(envelope)
    envelope["warnings"] = _sanitize_diagnostic(envelope["warnings"])
    envelope["data"] = _sanitize_error_fields(envelope["data"])
    return envelope


# Reached only when the freshness already is "stale", so the ceiling was never
# anything but 0.6; the ternary that said otherwise could not choose its second
# branch.
_STALE_COMPONENT_CEILING = 0.6


def _degrade_stale_envelope(envelope: dict) -> None:
    envelope["coverage"] = min(envelope["coverage"], _STALE_COMPONENT_CEILING)
    envelope["confidence"] = min(envelope["confidence"], _STALE_COMPONENT_CEILING)
    envelope["partial"] = True
    warning = "One or more response components are stale."
    if warning not in envelope["warnings"]:
        envelope["warnings"].append(warning)


def _signal_freshness(signal: str, signals: set) -> str:
    if signal in signals:
        return "fresh"
    return "missing"


def _reranker_freshness(trace: dict) -> str:
    if trace.get("reranker_applied"):
        return "fresh"
    if trace.get("reranker_fallback_reason"):
        return "missing"
    return "unknown"


def _recall_components(data) -> dict:
    if not isinstance(data, dict):
        return {}
    trace = data.get("retrieval_trace")
    if not isinstance(trace, dict):
        return {}
    generation = trace.get("corpus_generation")
    signals = set(trace.get("signals_used", []))
    components = {
        signal: {
            "generation": generation,
            "freshness": _signal_freshness(signal, signals),
        }
        for signal in ("lexical", "dense", "graph")
    }
    components["reranker"] = {
        "generation": generation,
        "freshness": _reranker_freshness(trace),
    }
    return components


def _context_components(data) -> dict:
    if not isinstance(data, dict):
        return {}
    if "packing_trace" not in data:
        return {}
    return {
        "context_compiler": {
            "generation": data.get("corpus_generation"),
            "freshness": "fresh",
        }
    }


def _navigation_freshness(status) -> str:
    if status == "stale":
        return "stale"
    if status in {"timeout", "error", "not_ready"}:
        return "unknown"
    return "fresh"


def _navigation_provider_component(data: dict, freshness: str) -> dict:
    provider = data.get("provider")
    if not isinstance(provider, dict):
        return {}
    if not isinstance(provider.get("name"), str):
        return {}
    return {
        "provider": {"generation": provider.get("version"), "freshness": freshness}
    }


def _first_graph_provenance(provenance: list):
    for item in provenance:
        if isinstance(item, dict) and item.get("source") == "graph":
            return item
    return None


def _navigation_graph_component(data: dict, freshness: str) -> dict:
    provenance = data.get("provenance")
    if not isinstance(provenance, list):
        return {}
    graph = _first_graph_provenance(provenance)
    if graph is None:
        return {}
    return {"graph": {"generation": graph.get("version"), "freshness": freshness}}


def _navigation_components(data: dict) -> dict:
    freshness = _navigation_freshness(data.get("status"))
    return {
        **_navigation_provider_component(data, freshness),
        **_navigation_graph_component(data, freshness),
    }


_GRAPH_COMPONENT_KEYS = ("source_generation", "graph_complete", "fallback")


def _graph_component_freshness(data: dict) -> str:
    if "error" in data:
        return "unknown"
    return "fresh"


def _graph_components(data) -> dict:
    if not isinstance(data, dict):
        return {}
    if not any(key in data for key in _GRAPH_COMPONENT_KEYS):
        return {}
    return {
        "graph": {
            "generation": data.get("source_generation"),
            "freshness": _graph_component_freshness(data),
        }
    }


_COMPONENT_BUILDERS = {
    "recall": _recall_components,
    "get_context": _context_components,
    "get_architecture": _graph_components,
    "find_dead_code": _graph_components,
}


def _components_for(name: str, data) -> dict[str, dict[str, object]]:
    if _is_rendered_navigation_data(name, data):
        return _navigation_components(data)
    builder = _COMPONENT_BUILDERS.get(name)
    if builder is None:
        return {}
    return builder(data)


def _grounded_recall(arguments: dict, deadline: float):
    from memory_state import ROOT
    from query_memory import grounded_qa

    return grounded_qa(
        arguments["query"],
        vault=ROOT,
        profile=arguments.get("profile"),
        deadline=deadline,
    )


def _tool_recall(arguments: dict, deadline: float):
    if arguments.get("grounded", False):
        return _grounded_recall(arguments, deadline), False
    effective_limit, limit_clamped = _clamped_limit(arguments.get("limit", 8))
    results = _call_with_deadline(
        _search_vault,
        arguments["query"],
        limit=effective_limit,
        deadline=deadline,
    )
    data = {
        "results": results,
        "retrieval_trace": _retrieval_trace(arguments["query"], results),
        "_meta": _call_with_deadline(_meta, deadline=deadline),
    }
    return data, limit_clamped


def _tool_read_page(arguments: dict, deadline: float):
    return _call_with_deadline(_read_page, arguments["slug"], deadline=deadline), False


def _tool_wiki_overview(arguments: dict, deadline: float):
    data = _call_with_deadline(_wiki_overview, deadline=deadline)
    data["_meta"] = _call_with_deadline(_meta, deadline=deadline)
    return data, False


def _tool_vault_status(arguments: dict, deadline: float):
    return _call_with_deadline(_vault_status, deadline=deadline), False


def _tool_get_decisions(arguments: dict, deadline: float):
    effective_limit, limit_clamped = _clamped_limit(arguments.get("limit", 10))
    data = _call_with_deadline(
        _get_decisions,
        arguments.get("query"),
        limit=effective_limit,
        deadline=deadline,
    )
    return data, limit_clamped


def _context_token_budget_options(arguments: dict) -> dict:
    if "token_budget" in arguments:
        return {"token_budget": arguments["token_budget"]}
    return {}


def _tool_get_context(arguments: dict, deadline: float):
    data = _call_with_deadline(
        _get_context,
        arguments["slugs"],
        arguments.get("include"),
        **_context_token_budget_options(arguments),
        deadline=deadline,
    )
    return data, False


def _tool_check_contradiction(arguments: dict, deadline: float):
    data = _call_with_deadline(
        _check_contradiction, arguments["claim"], deadline=deadline
    )
    return data, False


def _tool_log_decision(arguments: dict, deadline: float):
    data = _call_with_deadline(
        _log_decision,
        arguments["summary"],
        arguments.get("rationale", ""),
        deadline=deadline,
    )
    return data, False


def _tool_compile(arguments: dict, deadline: float):
    return _call_with_deadline(_trigger_compile, deadline=deadline), False


def _shaped_code_answer(data, arguments: dict):
    """CODE-06: one place where every code answer is measured and reduced.

    Applied here rather than inside `code_graph` or `graph_query` because this
    is the only layer that sees every mode's answer in one shape - and because
    those two modules belong to another agent's task in this session.
    """
    return shape_code_answer(
        data,
        budget_tokens=arguments.get("budget_tokens"),
        include_node_ids=bool(arguments.get("include_node_ids", False)),
    )


def _tool_find_dead_code(arguments: dict, deadline: float):
    try:
        data = _call_with_deadline(
            _find_dead_code,
            arguments.get("directory"),
            live=arguments.get("live", False),
            deadline=deadline,
        )
    except TimeoutError as error:
        return _code_graph_timeout_data(
            arguments.get("directory"), error, completed=()
        ), False
    return _shaped_code_answer(data, arguments), False


def _precise_architecture_call(arguments: dict, deadline: float):
    missing = [
        key
        for key in ("directory", "path", "line", "character")
        if key not in arguments
    ]
    if missing:
        return {"error": f"missing required arguments: {', '.join(missing)}"}
    return _get_precise_architecture(
        arguments["directory"],
        mode=arguments.get("mode"),
        path=arguments["path"],
        line=arguments["line"],
        character=arguments["character"],
        offset=arguments.get("offset", 0),
        limit=arguments.get("limit", 10),
        deadline=deadline,
    )


def _impact_architecture_call(arguments: dict, deadline: float):
    return _analyze_impact(
        directory=arguments.get("directory"),
        comparison=arguments.get("comparison", "dirty"),
        base=arguments.get("base"),
        target=arguments.get("target"),
        branch=arguments.get("branch"),
        deadline=deadline,
    )


def _summary_architecture_call(arguments: dict, deadline: float):
    return _call_with_deadline(
        _get_architecture,
        arguments.get("directory"),
        live=arguments.get("live", False),
        deadline=deadline,
    )


def _architecture_mode_call(arguments: dict, deadline: float):
    return _get_architecture_mode(
        arguments.get("directory"),
        mode=arguments["mode"],
        symbol=arguments.get("symbol"),
        target=arguments.get("target"),
        reverse=arguments.get("reverse", False),
        live=arguments.get("live", False),
        deadline=deadline,
    )


def _provenance_architecture_call(arguments: dict, deadline: float):
    """MEM-16: symbol -> decision pages naming it -> their cited sources."""
    from memory_state import ROOT
    from provenance_join import join_symbol_provenance

    directory = Path(arguments["directory"]).resolve()
    return join_symbol_provenance(
        Path(ROOT), directory, str(arguments["symbol"]), deadline
    )


def _snippet_architecture_call(arguments: dict, deadline: float):
    """CODE-02: bounded source blocks for a symbol, via the active generation."""
    from symbol_snippet import snippet_for_symbol

    directory = Path(arguments["directory"]).resolve()
    return snippet_for_symbol(directory, str(arguments["symbol"]), deadline)


def _coverage_architecture_call(arguments: dict, deadline: float):
    """CODE-05: is this path indexed, fresh, and how many nodes — honestly."""
    from path_coverage import coverage_for_path

    directory = Path(arguments["directory"]).resolve()
    return coverage_for_path(directory, str(arguments["path"]), deadline)


def _query_architecture_call(arguments: dict, deadline: float):
    """CODE-01: bounded multi-hop JSON pipeline over the active generation."""
    from graph_query import run_graph_query

    directory = Path(arguments["directory"]).resolve()
    return run_graph_query(directory, str(arguments["query"]), deadline)


def _tool_get_architecture(arguments: dict, deadline: float):
    try:
        data = _architecture_tool_call(arguments, deadline)
        return _shaped_code_answer(data, arguments), False
    except TimeoutError as error:
        return _architecture_timeout_data(arguments, error), False
    except ValueError as error:
        # Measured 2026-08-28: symbol mode on a common name raised a bare
        # "query row ceiling exceeded" through the dispatcher. A tool answers
        # with a named refusal, never an exception.
        return {
            "status": "error",
            "mode": arguments.get("mode", "summary"),
            "error": str(error)[:200],
        }, False


def _architecture_tool_call(arguments: dict, deadline: float):
    if _is_precise_architecture_request(arguments):
        return _precise_architecture_call(arguments, deadline)
    mode = arguments.get("mode", "summary")
    calls = {
        "impact": _impact_architecture_call,
        "summary": _summary_architecture_call,
        "provenance": _provenance_architecture_call,
        "snippet": _snippet_architecture_call,
        "coverage": _coverage_architecture_call,
        "query": _query_architecture_call,
    }
    call = calls.get(mode, _architecture_mode_call)
    return call(arguments, deadline)


def _architecture_timeout_data(arguments: dict, error: BaseException) -> dict:
    """Precise requests keep their navigation-failure shape; the rest name the budget."""
    failure = _navigation_failure_from_arguments(
        arguments,
        status=_navigation_error_status(error),
        warning=_navigation_error_warning(error),
    )
    if failure is not None:
        return failure
    return _code_graph_timeout_data(arguments.get("directory"), error, completed=())


def _tool_doctor(arguments: dict, deadline: float):
    return _call_with_deadline(_doctor, **arguments, deadline=deadline), False


_TOOL_HANDLERS = {
    "recall": _tool_recall,
    "read_page": _tool_read_page,
    "wiki_overview": _tool_wiki_overview,
    "vault_status": _tool_vault_status,
    "get_decisions": _tool_get_decisions,
    "get_context": _tool_get_context,
    "check_contradiction": _tool_check_contradiction,
    "log_decision": _tool_log_decision,
    "compile": _tool_compile,
    "find_dead_code": _tool_find_dead_code,
    "get_architecture": _tool_get_architecture,
}


def _dispatch_tool(name: str, arguments, deadline: float):
    handler = _TOOL_HANDLERS.get(name, _tool_doctor)
    return handler(arguments, deadline)


def _navigation_error_status(error: BaseException) -> str:
    if isinstance(error, TimeoutError):
        return "timeout"
    return "error"


def _navigation_error_warning(error: BaseException) -> str:
    if isinstance(error, TimeoutError):
        return "navigation_timeout"
    return "navigation_failed"


def _tool_call_failure(name: str, arguments, error: BaseException) -> dict:
    """The failing tool names itself, so the trace can say which one it was."""
    if name != "get_architecture":
        return {"error": _safe_exception_text(error, f"mcp.{name}")}
    failure = _navigation_failure_from_arguments(
        arguments,
        status=_navigation_error_status(error),
        warning=_navigation_error_warning(error),
    )
    if failure is None:
        return {"error": _safe_exception_text(error, f"mcp.{name}")}
    return failure


def _tool_call_data(name: str, arguments, operation_deadline: float):
    """Return the tool payload and whether its limit argument was clamped."""
    _check_deadline(operation_deadline)
    if name not in TOOL_INPUT_SCHEMAS:
        return {"error": f"Unknown tool: {name}"}, False
    _check_deadline(operation_deadline)
    validation_error = _validate_tool_arguments(name, arguments)
    _check_deadline(operation_deadline)
    if validation_error is not None:
        return {"error": validation_error}, False
    try:
        _check_deadline(operation_deadline)
        return _dispatch_tool(name, arguments, operation_deadline)
    except Exception as error:  # noqa: BLE001 - stable tool failure boundary
        return _tool_call_failure(name, arguments, error), False


def _dict_arguments(arguments):
    if isinstance(arguments, dict):
        return arguments
    return None


def _tool_call_envelope(
    name: str, data, arguments, limit_clamped: bool, operation_deadline: float
) -> dict:
    if _is_rendered_navigation_data(name, data):
        data = _sanitize_navigation_data(data)
    _check_deadline(operation_deadline)
    quality = _quality_for(
        name, data, _dict_arguments(arguments), limit_clamped=limit_clamped
    )
    _check_deadline(operation_deadline)
    components = _components_for(name, data)
    _check_deadline(operation_deadline)
    return _build_operation_envelope(data, quality, components=components)


def _record_answer_cost(envelope: dict, started: float, operation_deadline: float) -> None:
    """OPS-02: the answer states what it cost, and never fails for saying so.

    Attached here because this is the one funnel every tool call passes, and
    the last point before the answer is serialised - so the estimate covers
    the finished envelope rather than a payload that later grew. A telemetry
    failure leaves the key absent, which by the module's contract reads as
    "not measured" and never as "free".
    """
    try:
        attach_answer_cost(
            envelope,
            elapsed_seconds=time.monotonic() - started,
            budget_seconds=operation_deadline - started,
        )
    except Exception:  # noqa: BLE001 - an answer outranks its own cost line
        return


def _execute_tool_call(name: str, arguments, operation_deadline: float) -> str:
    """Execute one tool under the absolute deadline created by its async handler."""
    import json

    started = time.monotonic()
    deadline_token = _OPERATION_DEADLINE.set(operation_deadline)
    try:
        data, limit_clamped = _tool_call_data(name, arguments, operation_deadline)
        _check_deadline(operation_deadline)
        envelope = _tool_call_envelope(
            name, data, arguments, limit_clamped, operation_deadline
        )
        _check_deadline(operation_deadline)
        _record_answer_cost(envelope, started, operation_deadline)
        return json.dumps(envelope, indent=2, ensure_ascii=False, allow_nan=False)
    finally:
        _OPERATION_DEADLINE.reset(deadline_token)


async def _handle_tool_call(name: str, arguments) -> str:
    """Handle a tool call without blocking unrelated MCP event-loop work."""
    operation_seconds = _tool_operation_seconds(name, arguments)
    operation_deadline = time.monotonic() + operation_seconds
    try:
        return await _run_bounded(
            _execute_tool_call, name, arguments, operation_deadline, deadline=operation_deadline
        )
    except TimeoutError:
        return _tool_timeout_envelope_text(name, arguments)


def _build_resource_definitions() -> list:
    """Build stable application resources when the installed SDK supports them."""
    if not MCP_RESOURCES_AVAILABLE:
        return []
    return [
        Resource(
            name="llm-wiki-health",
            uri=HEALTH_RESOURCE_URI,
            description="Local vault compile and index health.",
            mimeType="application/json",
        ),
        Resource(
            name="llm-wiki-context",
            uri=CONTEXT_RESOURCE_URI,
            description="Local vault overview and current context status.",
            mimeType="application/json",
        ),
    ]


def _handle_resource_read(uri: str, deadline: float | None = None) -> str:
    """Return one resource as a JSON text envelope."""
    import json

    operation_deadline = _operation_deadline(deadline)
    deadline_token = _OPERATION_DEADLINE.set(operation_deadline)
    try:
        if uri == HEALTH_RESOURCE_URI:
            data = _call_with_deadline(
                _vault_status, deadline=operation_deadline
            )
        elif uri == CONTEXT_RESOURCE_URI:
            data = {
                "overview": _call_with_deadline(
                    _wiki_overview, deadline=operation_deadline
                ),
                "status": _call_with_deadline(
                    _vault_status, deadline=operation_deadline
                ),
            }
        else:
            data = {"error": f"Unknown resource: {uri}"}
    except Exception as error:
        data = {"error": _safe_exception_text(error, f"mcp.resource:{uri}")}
    try:
        envelope = _build_operation_envelope(data, _resource_quality(data))
        return json.dumps(envelope, indent=2, ensure_ascii=False, allow_nan=False)
    finally:
        _OPERATION_DEADLINE.reset(deadline_token)


def _register_resources(server) -> bool:
    """Register resources only when both SDK types and methods are available."""
    if not MCP_RESOURCES_AVAILABLE:
        return False
    list_resources_method = getattr(server, "list_resources", None)
    read_resource_method = getattr(server, "read_resource", None)
    if not callable(list_resources_method) or not callable(read_resource_method):
        return False
    resources = _build_resource_definitions()

    @list_resources_method()
    async def list_resources():
        return resources

    @read_resource_method()
    async def read_resource(uri):
        operation_deadline = time.monotonic() + MCP_OPERATION_SECONDS
        try:
            text = await _run_bounded(
                _handle_resource_read,
                str(uri),
                operation_deadline,
                deadline=operation_deadline,
            )
        except TimeoutError:
            text = _timeout_envelope_text()
        return [
            TextResourceContents(
                uri=uri,
                mimeType="application/json",
                text=text,
            )
        ]

    return True


def _format_tool_result(result: str):
    """Return text-only output or text plus structured content by capability."""
    import json

    text_content = [TextContent(type="text", text=result)]
    structured = json.loads(result)
    if MCP_CALL_TOOL_RESULT_AVAILABLE:
        return CallToolResult(
            content=text_content,
            structuredContent=structured,
            isError=_result_is_error(structured.get("data")),
        )
    if MCP_STRUCTURED_OUTPUT_AVAILABLE:
        return text_content, structured
    return text_content


def _repair_failed(data: dict) -> bool:
    return any(
        isinstance(check, dict)
        and bool(check.get("details", {}).get("repair_errors"))
        for check in data.get("checks", [])
    )


def _plain_result_is_error(data: dict) -> bool:
    if "error" in data or _repair_failed(data):
        return True
    return data.get("status") == "error" or data.get("overall_status") == "error"


def _result_is_error(data) -> bool:
    if not isinstance(data, dict):
        return False
    if _is_rendered_navigation_data("get_architecture", data):
        return data.get("status") in {"error", "timeout"}
    return _plain_result_is_error(data)


def _execute_formatted_tool_call(name: str, arguments, operation_deadline: float):
    result = _execute_tool_call(name, arguments, operation_deadline)
    _check_deadline(operation_deadline)
    return _format_tool_result(result)


def _register_tools(server, tools):
    """Register tool callbacks while disabling SDK-side validation when supported."""
    @server.list_tools()
    async def list_tools():
        return tools

    call_tool_method = server.call_tool
    try:
        parameters = inspect.signature(call_tool_method).parameters.values()
        supports_validate_input = any(
            parameter.name == "validate_input"
            or parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
    except (TypeError, ValueError):
        supports_validate_input = False
    decorator = (
        call_tool_method(validate_input=False)
        if supports_validate_input
        else call_tool_method()
    )
    timeout_result = _format_tool_result(_timeout_envelope_text())

    @decorator
    async def call_tool(name: str, arguments):
        operation_deadline = time.monotonic() + _tool_operation_seconds(
            name,
            arguments,
        )
        try:
            return await _run_bounded(
                _execute_formatted_tool_call,
                name,
                arguments,
                operation_deadline,
                deadline=operation_deadline,
            )
        except TimeoutError:
            if name == "get_architecture" and isinstance(
                arguments,
                dict,
            ) and _is_precise_architecture_request(arguments):
                return _format_tool_result(
                    _tool_timeout_envelope_text(name, arguments)
                )
            return timeout_result

    return call_tool


def _start_encoder_warmup() -> None:
    """Load the semantic encoder before the first question needs it.

    Measured on this vault (2026-08-24/26): a cold encoder costs 7.99 s and
    ~1.1 GiB resident, so the first semantic question of a session always fell
    back to lexical-only. The owner accepted that price on 2026-08-27: every
    resident server pays the memory up front, and the first `recall` answers
    with the dense leg. The load runs on a daemon thread so serving starts
    immediately, and it shares `search_memory._get_embedder`'s module cache
    with the dense leg — a straggler racing it wastes one load, never a vector.
    Set LLMWIKI_NO_ENCODER_WARMUP=1 to keep the old lazy behaviour.
    """
    if os.environ.get("LLMWIKI_NO_ENCODER_WARMUP") == "1":
        return

    def warm() -> None:
        with contextlib.suppress(Exception):
            import search_memory

            search_memory._get_embedder()

    threading.Thread(target=warm, name="encoder-warmup", daemon=True).start()


def run_server() -> int:
    """Start the MCP server (stdio transport). Returns exit code."""
    if not MCP_AVAILABLE:
        print(
            "MCP package not installed. Run: uv sync --extra mcp-server",
            file=sys.stderr,
        )
        return 1

    server = Server("llm-wiki")
    tools = _build_tool_definitions()
    _register_resources(server)
    _register_tools(server, tools)
    _start_encoder_warmup()

    async def main():
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    exit_code = 0
    try:
        asyncio.run(main())
    finally:
        try:
            _close_navigation_session_manager(
                time.monotonic() + MCP_OPERATION_SECONDS
            )
        except BaseException as error:
            print(
                f"navigation session manager close failed: {type(error).__name__}",
                file=sys.stderr,
            )
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(run_server())
