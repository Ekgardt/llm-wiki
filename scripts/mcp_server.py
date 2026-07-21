"""LLM-Wiki MCP Server — 12 task-shaped tools, stdio transport, 100% local.

Gives AI agents (Claude Code, OpenCode, Codex, Cursor, Antigravity) structured
access to the knowledge vault via Model Context Protocol. No server, no cloud,
no network — stdio subprocess on the same machine.

Install: uv sync --extra mcp-server
Run:    uv run python scripts/mcp_server.py

Agent config (e.g. for Claude Code ~/.claude/.mcp.json):
{
  "mcpServers": {
    "llm-wiki": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/llm-wiki", "python", "scripts/mcp_server.py"]
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
import contextvars
import datetime as dt
import inspect
import itertools
import re
import sys
import threading
import time
from contextvars import ContextVar
from dataclasses import asdict
from pathlib import Path, PureWindowsPath

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bounded_io import read_stable_bytes  # noqa: E402

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


async def _run_bounded(function, *args, deadline: float):
    """Run synchronous work off-loop without an unbounded submission queue."""
    _check_deadline(deadline)
    submitted = concurrent.futures.Future()
    abandoned = threading.Event()
    cancellation_token = _OPERATION_CANCELLED.set(abandoned.is_set)
    try:
        context = contextvars.copy_context()
    finally:
        _OPERATION_CANCELLED.reset(cancellation_token)
    with _MCP_WORKERS_LOCK:
        _MCP_WORKERS.difference_update(
            future for future in _MCP_WORKERS if future.done()
        )
        if len(_MCP_WORKERS) >= MCP_WORKER_SLOTS:
            raise TimeoutError("MCP worker capacity exhausted")
        _MCP_WORKERS.add(submitted)

    def discard(completed):
        try:
            error = completed.exception()
        except concurrent.futures.CancelledError:
            error = None
        if error is not None and abandoned.is_set():
            print(
                f"mcp worker failed after caller timeout: {type(error).__name__}",
                file=sys.stderr,
            )
        with _MCP_WORKERS_LOCK:
            _MCP_WORKERS.discard(completed)

    submitted.add_done_callback(discard)

    def run():
        if not submitted.set_running_or_notify_cancel():
            return
        try:
            result = context.run(function, *args)
        except BaseException as error:
            submitted.set_exception(error)
        else:
            submitted.set_result(result)

    thread = threading.Thread(
        target=run,
        name=f"llm-wiki-mcp-{next(_MCP_WORKER_IDS)}",
        daemon=True,
    )
    try:
        thread.start()
    except BaseException:
        submitted.cancel()
        raise
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


def _doctor_branch(
    action: str,
    *,
    target: bool = False,
    limit: bool = False,
    mutation: bool = False,
) -> dict:
    properties = {"action": {"type": "string", "const": action}}
    required = ["action"]
    if target:
        properties["target_id"] = {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
        }
        required.append("target_id")
    if limit:
        properties["limit"] = {"type": "integer", "minimum": 1, "maximum": 100}
    if mutation:
        properties["repair"] = {"type": "boolean", "const": True}
        required.append("repair")
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
                ],
                "description": "Bounded architecture query mode",
            },
            "symbol": {"type": "string", "minLength": 1, "maxLength": 1024},
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
    from search_memory import search

    operation_deadline = (
        _SEARCH_OPERATION_DEADLINE.get() if deadline is None else deadline
    )
    if operation_deadline is None:
        operation_deadline = time.monotonic() + MCP_OPERATION_SECONDS

    def run_search(*, semantic: bool, graph: bool = True, rerank: bool = True) -> list[dict]:
        return search(
            query,
            limit=limit,
            semantic=semantic,
            graph=graph,
            rerank=rerank,
            source_tool="mcp.recall",
            deadline_monotonic=operation_deadline,
            max_candidates=limit,
        )

    try:
        return run_search(semantic=True)
    except TimeoutError:
        lexical = run_search(semantic=False, graph=False, rerank=False)
        return [
            {
                **row,
                "requested_mode": "HYBRID",
                "fallback_reason": "retrieval_deadline_exceeded",
                "partial": True,
            }
            for row in lexical
        ]


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

    if not isinstance(slug, str) or len(slug) > MAX_MCP_SLUG_LENGTH:
        return {"error": "Invalid page slug"}
    windows_path = PureWindowsPath(slug)
    if (
        slug in {".", ".."}
        or "/" in slug
        or "\\" in slug
        or windows_path.drive
        or Path(slug).is_absolute()
    ):
        return {"error": f"Invalid page slug: {slug}"}
    notes_dir = ROOT / "knowledge" / "notes"
    page_path = notes_dir / f"{slug}.md"
    _check_deadline(deadline)
    if not page_path.exists():
        return {"error": f"Page not found: {slug}"}
    try:
        content = read_stable_bytes(
            page_path, MAX_MCP_PAGE_BYTES, label="MCP page"
        ).decode("utf-8", errors="strict")
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return {"error": f"Page read failed: {_safe_page_read_error(exc)}"}
    from evidence_resolver import (
        EvidenceResolutionError,
        EvidenceResolver,
        extract_evidence_references,
    )

    evidence = []
    evidence_bytes = 0
    resolver = EvidenceResolver(ROOT, state_root=STATE_ROOT)
    try:
        references = extract_evidence_references(content) if resolve_evidence else []
        for reference in references:
            _check_deadline(deadline)
            resolved = resolver.resolve(reference)
            if len(resolved.bytes) > MAX_MCP_EVIDENCE_BYTES:
                raise EvidenceResolutionError(
                    f"evidence slice exceeds {MAX_MCP_EVIDENCE_BYTES} bytes"
                )
            evidence_bytes += len(resolved.bytes)
            if evidence_bytes > MAX_MCP_TOTAL_EVIDENCE_BYTES:
                raise EvidenceResolutionError(
                    f"total evidence exceeds {MAX_MCP_TOTAL_EVIDENCE_BYTES} bytes"
                )
            evidence.append(
                {
                    "reference": str(reference),
                    "sha256": resolved.sha256,
                    "text": resolved.bytes.decode("utf-8", errors="strict"),
                }
            )
    except (EvidenceResolutionError, OSError, UnicodeDecodeError, ValueError) as exc:
        return {"error": f"Evidence resolution failed: {_safe_evidence_error(exc)}"}
    result = {
        "slug": slug,
        "path": str(page_path.relative_to(ROOT)),
        "content": content,
        "evidence": evidence,
    }
    if emit_telemetry:
        try:
            from retrieval_telemetry import (
                best_effort_make_event,
                best_effort_record_events,
            )

            events = []
            for kind, candidate_id in [
                ("page_read", slug),
                *(("evidence_read", item["sha256"]) for item in evidence),
            ]:
                event = best_effort_make_event(
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
            if events:
                best_effort_record_events(events)
        except Exception:
            pass
    return result


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
    try:
        daily_files = list((ROOT / "knowledge" / "daily").glob("*.md"))
    except OSError:
        daily_files = []
    backlog = 0
    for daily_path in daily_files:
        _check_deadline(deadline)
        try:
            current_hash = file_hash(daily_path)
        except OSError:
            backlog += 1
            continue
        if compiled.get(daily_path.name) != current_hash:
            backlog += 1
    return {
        "last_compile": state.get("last_compile_at", "never"),
        "last_compile_status": state.get("last_compile_status", "unknown"),
        "compile_backlog": backlog,
    }


def _get_decisions(
    query: str | None = None, limit: int = 10, *, deadline: float | None = None
) -> list[dict]:
    """Get active decisions from the vault."""
    from search_memory import search
    if query is not None and (
        not isinstance(query, str) or len(query) > MAX_MCP_QUERY_LENGTH
    ):
        raise ValueError("query exceeds the MCP retrieval bound")
    effective_query = query or "decision"
    candidates = search(
        effective_query,
        limit=limit,
        source_tool="mcp.get_decisions",
        emit_telemetry=False,
        deadline_monotonic=deadline,
    )
    # Filter to decision-type pages
    results = [
        result
        for result in candidates
        if result.get("type") == "decision"
        or "decision" in result.get("path", "").lower()
    ]
    if results:
        try:
            from retrieval_telemetry import (
                best_effort_make_event,
                best_effort_record_events,
            )

            events = []
            for rank, result in enumerate(results, start=1):
                candidate_id = result.get("slug") or Path(
                    result.get("path", "")
                ).stem
                event = best_effort_make_event(
                    event_kind="impression",
                    query=effective_query,
                    retrieval_mode="decision-filter",
                    candidate_id=candidate_id,
                    rank=rank,
                    generation="legacy",
                    source_tool="mcp.get_decisions",
                )
                if event is not None:
                    events.append(event)
            if events:
                best_effort_record_events(events)
        except Exception:
            pass
    return results


def _get_context(
    slugs: list[str],
    include: list[str] | None = None,
    *,
    token_budget: int = 8192,
    deadline: float | None = None,
) -> dict:
    """Return one compiler package under one shared token budget."""
    if not isinstance(slugs, list) or not 1 <= len(slugs) <= MAX_MCP_CONTEXT_SLUGS:
        raise ValueError("slugs exceed the MCP context bound")
    if any(
        not isinstance(slug, str) or len(slug) > MAX_MCP_SLUG_LENGTH
        for slug in slugs
    ):
        raise ValueError("slug exceeds the MCP context bound")
    include = include or []
    if not isinstance(include, list) or len(include) > MAX_MCP_CONTEXT_INCLUDE:
        raise ValueError("include exceeds the MCP context bound")
    if any(
        not isinstance(item, str) or len(item) > MAX_MCP_INCLUDE_LENGTH
        for item in include
    ):
        raise ValueError("include item exceeds the MCP context bound")
    slugs = list(dict.fromkeys(slugs))
    include = list(dict.fromkeys(include))
    if (
        isinstance(token_budget, bool)
        or not isinstance(token_budget, int)
        or not 256 <= token_budget <= MAX_MCP_CONTEXT_TOKENS
    ):
        raise ValueError("token_budget exceeds the MCP context bound")

    from context_budget import BudgetExceededError, ContextBudget
    from context_compiler import compile_context
    from corpus_snapshot import CorpusSnapshot, collect_corpus
    from memory_state import ROOT

    operation_deadline = _operation_deadline(deadline)
    snapshot = collect_corpus(ROOT, deadline=operation_deadline)
    requested = set(slugs)
    sources = tuple(
        source
        for source in snapshot.sources
        if Path(source.record.relative_path).stem in requested
        or source.record.logical_id in requested
        or source.record.relative_path in requested
    )
    selected_paths = {source.record.relative_path for source in sources}
    found_requested = {
        requested_slug
        for requested_slug in requested
        for source in sources
        if requested_slug
        in {
            Path(source.record.relative_path).stem,
            source.record.logical_id,
            source.record.relative_path,
        }
    }
    chunks = tuple(
        chunk for chunk in snapshot.chunks if chunk.parent_page in selected_paths
    )
    narrow = CorpusSnapshot(
        sources,
        chunks,
        snapshot.corpus_sha256,
        snapshot.policy,
        snapshot.collector_version,
        snapshot.extractor_version,
    )
    budget = ContextBudget(None, token_budget, 0, 0)
    shortlist = tuple(source.record.logical_id for source in sources)
    try:
        compiled = compile_context(
            narrow,
            shortlist=shortlist,
            evidence_chunk_ids=(chunk.id for chunk in chunks),
            budget=budget,
            deadline=operation_deadline,
        )
    except BudgetExceededError:
        compiled = compile_context(
            narrow,
            shortlist=shortlist,
            evidence_chunk_ids=(),
            budget=budget,
            deadline=operation_deadline,
        )
    items = [asdict(item) for item in compiled.items]
    pages = [item for item in items if item["source"].endswith(".md")]
    result = {
        "text": compiled.text,
        "packed_tokens": compiled.packed_tokens,
        "token_budget": token_budget,
        "corpus_generation": snapshot.corpus_sha256,
        "repo_map": sorted(selected_paths),
        "pages": pages,
        "symbols": [item for item in items if not item["source"].endswith(".md")],
        "decisions": [item for item in items if item["type"] == "decision"],
        "incidents": [
            item for item in items if item["type"] in {"debugging", "incident"}
        ],
        "active_task": [item for item in items if item["type"] == "project-state"],
        "evidence": [item for item in items if item["representation"] == "l2"],
        "retrieval_trace": asdict(compiled.trace.retrieval),
        "materialization_trace": [
            asdict(item) for item in compiled.trace.materializations
        ],
        "packing_trace": asdict(compiled.trace.packing),
        "missing_slugs": sorted(requested - found_requested),
        "include": include,
    }
    if sources:
        try:
            from retrieval_telemetry import (
                best_effort_make_event,
                best_effort_record_events,
            )

            events = [
                event
                for slug in sorted({Path(path).stem for path in selected_paths})
                if (
                    event := best_effort_make_event(
                        event_kind="context_injected",
                        query=None,
                        retrieval_mode="direct",
                        candidate_id=slug,
                        rank=None,
                        generation="legacy",
                        source_tool="mcp.get_context",
                    )
                ) is not None
            ]
            if events:
                best_effort_record_events(events)
        except Exception:
            pass
    return result


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
        return {"error": _safe_exception_text(e)}


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
    return {"status": "completed", "returncode": returncode}


def _find_dead_code(
    directory: str, *, live: bool = False, deadline: float | None = None
) -> dict:
    """Find conservative dead-code candidates in a project directory."""
    from code_graph import find_dead_code

    resolved, error = _validated_code_directory(directory, deadline=deadline)
    if error:
        return {"error": error}
    result = find_dead_code(resolved, live=live, with_report=True)
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

    resolved, error = _validated_code_directory(directory, deadline=deadline)
    if error:
        return {"error": error}
    architecture = get_architecture(resolved, live=live, with_report=True)
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
    from code_graph import (
        detect_communities,
        find_callees,
        find_callers,
        find_dependencies,
        find_paths,
    )

    resolved, error = _validated_code_directory(directory, deadline=deadline)
    if error:
        return {"error": error}
    if mode in {"symbol", "callers", "callees", "dependencies"} and not symbol:
        return {"error": f"symbol is required for {mode} mode"}
    if mode == "path" and (not symbol or not target):
        return {"error": "symbol and target are required for path mode"}

    _check_deadline(deadline)
    if mode == "callers":
        architecture = find_callers(symbol, resolved, live=live, with_report=True)
    elif mode == "callees":
        architecture = find_callees(symbol, resolved, live=live, with_report=True)
    elif mode == "dependencies":
        architecture = find_dependencies(
            symbol, resolved, reverse=reverse, live=live, with_report=True
        )
    elif mode == "path":
        architecture = find_paths(
            symbol, target, resolved, live=live, with_report=True
        )
    elif mode == "community":
        architecture = detect_communities(resolved, live=live, with_report=True)
    else:
        callers = find_callers(symbol, resolved, live=live, with_report=True)
        _check_deadline(deadline)
        callees = find_callees(symbol, resolved, live=live, with_report=True)
        _check_deadline(deadline)
        dependencies = find_dependencies(
            symbol, resolved, live=live, with_report=True
        )
        architecture = {
            "symbol": symbol,
            "callers": callers.get("callers", []),
            "callees": callees.get("callees", []),
            "dependencies": dependencies.get("dependencies", []),
            **{
                key: callers.get(key)
                for key in (
                    "source_generation",
                    "graph_complete",
                    "unresolved_count",
                    "fallback",
                )
            },
        }
    report = {
        key: architecture.get(key)
        for key in (
            "source_generation",
            "graph_complete",
            "unresolved_count",
            "fallback",
        )
        if isinstance(architecture, dict) and key in architecture
    }
    return {
        "directory": str(resolved),
        "mode": mode,
        "architecture": architecture,
        **report,
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


def _operator_result(
    action: str,
    *,
    ids: list[str] | None = None,
    states: list[str] | None = None,
    codes: list[str] | None = None,
    counts: dict[str, int] | None = None,
    overall_status: str | None = None,
) -> dict:
    state_values = set(states or [])
    overall_status = overall_status or (
        "error"
        if "error" in state_values
        else "degraded"
        if "degraded" in state_values
        else "ok"
    )
    return {
        "action": action,
        "overall_status": overall_status,
        "ids": (ids or [])[:100],
        "counts": counts or {"items": len(ids or [])},
        "states": sorted(set(states or [])),
        "codes": sorted(set(codes or [])),
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

    mutations = {
        "queue-cancel",
        "queue-redrive",
        "transaction-recover",
        "transaction-undo",
    }
    if action in mutations and repair is not True:
        return _operator_result(
            action, states=["error"], codes=["repair_required"], overall_status="error"
        )
    operation_deadline = _operation_deadline(deadline)
    cancelled = _operation_cancelled()
    try:
        if action == "status":
            from doctor import run_doctor

            report = run_doctor(
                root=ROOT,
                state_root=STATE_ROOT,
                deadline=operation_deadline,
            )
            codes = [item["code"] for item in report["run_deletion"]["blockers"]]
            for check in report["checks"]:
                codes.extend(str(code) for code in check.get("details", {}).get("codes", []))
            return _operator_result(
                action,
                ids=[str(check["id"]) for check in report["checks"]][:limit],
                states=[str(report["overall_status"])],
                codes=codes,
                counts={key: int(value) for key, value in report["counts"].items()},
                overall_status=str(report["overall_status"]),
            )
        if action in {"queue-inspect", "queue-dead-list"}:
            from reliable_memory import open_readonly_operational_db

            path = STATE_ROOT / "run" / "queue.sqlite3"
            if not path.is_file():
                if action == "queue-dead-list":
                    return _operator_result(action, codes=["queue_missing"])
                return _operator_result(
                    action,
                    states=["error"],
                    codes=["queue_missing"],
                    overall_status="error",
                )
            connection = open_readonly_operational_db(
                path,
                STATE_ROOT,
                max_bytes=256 * 1024 * 1024,
            )
            try:
                if action == "queue-inspect":
                    row = connection.execute(
                        "SELECT id,state,error_code FROM tasks WHERE id=?", (target_id,)
                    ).fetchone()
                    if row is None:
                        return _operator_result(
                            action,
                            states=["error"],
                            codes=["unknown_task"],
                            overall_status="error",
                        )
                    return _operator_result(
                        action,
                        ids=[str(row["id"])],
                        states=[str(row["state"])],
                        codes=[str(row["error_code"])] if row["error_code"] else [],
                    )
                rows = connection.execute(
                    "SELECT id,state,error_code FROM tasks WHERE state='dead' "
                    "ORDER BY updated_at,id LIMIT ?",
                    (limit,),
                ).fetchall()
            finally:
                connection.close()
            return _operator_result(
                action,
                ids=[str(row["id"]) for row in rows],
                states=[str(row["state"]) for row in rows],
                codes=[str(row["error_code"]) for row in rows if row["error_code"]],
            )
        if action in {"queue-cancel", "queue-redrive"}:
            from memory_queue import MemoryQueue, QueueOperationError

            queue = MemoryQueue(STATE_ROOT)
            if action == "queue-cancel":
                changed = queue.cancel(
                    str(target_id),
                    deadline=operation_deadline,
                    cancelled=cancelled,
                )
                return _operator_result(
                    action,
                    ids=[str(target_id)] if changed else [],
                    states=["cancelled"] if changed else ["error"],
                    codes=[] if changed else ["unknown_or_terminal_task"],
                    overall_status="ok" if changed else "error",
                )
            try:
                replacement = queue.redrive(
                    str(target_id),
                    deadline=operation_deadline,
                    cancelled=cancelled,
                )
            except KeyError:
                return _operator_result(
                    action,
                    states=["error"],
                    codes=["unknown_task"],
                    overall_status="error",
                )
            except QueueOperationError as error:
                code = str(error) if str(error) == "redrive_requires_dead" else "redrive_invalid"
                return _operator_result(
                    action,
                    states=["error"],
                    codes=[code],
                    overall_status="error",
                )
            return _operator_result(action, ids=[replacement], states=["ready"])
        if action in {"transaction-recover", "transaction-undo"}:
            from markdown_transaction import MarkdownCoordinator

            coordinator = MarkdownCoordinator(ROOT, STATE_ROOT)
            if action == "transaction-recover":
                records = coordinator.recover(
                    max_transactions=limit,
                    deadline=operation_deadline,
                    cancelled=cancelled,
                )
            else:
                prepared = coordinator.undo(
                    str(target_id),
                    deadline=operation_deadline,
                    cancelled=cancelled,
                )
                records = [
                    coordinator.apply(
                        prepared.id,
                        deadline=operation_deadline,
                        cancelled=cancelled,
                    )
                ]
            return _operator_result(
                action,
                ids=[record.id for record in records],
                states=[record.state for record in records],
                codes=[record.error_code for record in records if record.error_code],
            )
        if action in {"archive-status", "claim-status"}:
            from doctor import _archive_check, _claim_check

            check = (
                _archive_check(ROOT, STATE_ROOT, operation_deadline)
                if action == "archive-status"
                else _claim_check(ROOT, STATE_ROOT, operation_deadline)
            )
            details = check["details"]
            counts = {
                key: value
                for key, value in details.items()
                if isinstance(value, int) and not isinstance(value, bool)
            }
            return _operator_result(
                action,
                states=[str(check["status"])],
                codes=[str(code) for code in details.get("codes", [])],
                counts=counts,
            )
    except Exception as error:  # noqa: BLE001 - stable operator failure boundary
        code = {
            "KeyError": "unknown_target",
            "TimeoutError": "owner_busy",
            "MigrationBusy": "owner_busy",
        }.get(type(error).__name__, "operation_failed")
        return _operator_result(
            action, states=["error"], codes=[code], overall_status="error"
        )
    return _operator_result(
        action, states=["error"], codes=["unknown_action"], overall_status="error"
    )


def _validated_code_directory(
    directory: str, *, deadline: float | None = None
) -> tuple[Path | None, str | None]:
    """Validate an explicitly supplied, bounded local project directory."""
    if not isinstance(directory, str) or not directory.strip():
        return None, "directory is required"
    _check_deadline(deadline)
    candidate = Path(directory).expanduser()
    if not candidate.is_absolute():
        return None, "directory must be an absolute local path"
    resolved = candidate.resolve()
    if not resolved.exists():
        return None, f"directory does not exist: {resolved}"
    if not resolved.is_dir():
        return None, f"directory is not a directory: {resolved}"
    if resolved == Path(resolved.anchor):
        return None, "directory must not be a filesystem root"
    return resolved, None


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
        errors = [
            _validate_object_schema(branch, arguments)
            for branch in schema["oneOf"]
        ]
        if sum(error is None for error in errors) == 1:
            return None
        return "arguments do not match exactly one allowed action"
    error = _validate_object_schema(schema, arguments, reject_unknown=False)
    if error is not None:
        return error
    if name == "recall" and "profile" in arguments and arguments.get("grounded") is not True:
        return "argument 'profile' requires grounded=true"
    if name == "get_architecture":
        mode = arguments.get("mode", "summary")
        if mode in {"symbol", "callers", "callees", "dependencies"} and not arguments.get(
            "symbol"
        ):
            return f"required argument is missing for {mode}: symbol"
        if mode == "path" and (
            not arguments.get("symbol") or not arguments.get("target")
        ):
            return "required arguments are missing for path: symbol and target"
    return None


def _validate_object_schema(
    schema: dict, arguments: dict, *, reject_unknown: bool = True
) -> str | None:
    for key in schema["required"]:
        if key not in arguments:
            return f"required argument is missing: {key}"
    for key, value in arguments.items():
        field = schema["properties"].get(key)
        if field is None:
            if reject_unknown or schema.get("additionalProperties") is False:
                return f"unknown argument: {key}"
            continue
        expected = field["type"]
        if expected == "string" and not isinstance(value, str):
            return f"argument '{key}' must be a string"
        if expected == "integer" and (
            isinstance(value, bool) or not isinstance(value, int)
        ):
            return f"argument '{key}' must be an integer"
        if expected == "boolean" and not isinstance(value, bool):
            return f"argument '{key}' must be a boolean"
        if expected == "array":
            if not isinstance(value, list):
                return f"argument '{key}' must be an array"
            item_type = field.get("items", {}).get("type")
            if item_type == "string" and any(not isinstance(item, str) for item in value):
                return f"argument '{key}' items must be strings"
            if "minItems" in field and len(value) < field["minItems"]:
                return f"argument '{key}' has too few items"
            if "maxItems" in field and len(value) > field["maxItems"]:
                return f"argument '{key}' has too many items"
            if field.get("uniqueItems") and len(set(value)) != len(value):
                return f"argument '{key}' items must be unique"
            item_max_length = field.get("items", {}).get("maxLength")
            if item_max_length is not None and any(
                len(item) > item_max_length for item in value
            ):
                return f"argument '{key}' item is too long"
        if "minimum" in field and value < field["minimum"]:
            return f"argument '{key}' must be at least {field['minimum']}"
        if "maximum" in field and value > field["maximum"]:
            return f"argument '{key}' must be at most {field['maximum']}"
        if "minLength" in field and len(value) < field["minLength"]:
            return f"argument '{key}' is too short"
        if "maxLength" in field and len(value) > field["maxLength"]:
            return f"argument '{key}' is too long"
        if "const" in field and value != field["const"]:
            return f"argument '{key}' has an invalid value"
        if "enum" in field and value not in field["enum"]:
            return f"argument '{key}' has an invalid value"
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


def _safe_exception_text(error: BaseException) -> str:
    """Return a bounded stable code without exposing untrusted exception details."""
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


def _sanitize_diagnostic(value):
    if isinstance(value, str):
        text = redact_secrets(value)
        text = _ABSOLUTE_PATH.sub("[REDACTED_PATH]", text)
        return " ".join(text.split())[:MAX_MCP_ERROR_CHARS]
    if isinstance(value, list):
        return [_sanitize_diagnostic(item) for item in value]
    if isinstance(value, dict):
        return {key: _sanitize_diagnostic(item) for key, item in value.items()}
    return value


def _sanitize_error_fields(value):
    if isinstance(value, list):
        return [_sanitize_error_fields(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _sanitize_diagnostic(item)
            if key == "error"
            else _sanitize_error_fields(item)
            for key, item in value.items()
        }
    return value


def _quality_for(
    name: str,
    data,
    arguments: dict | None = None,
    *,
    limit_clamped: bool = False,
) -> dict:
    """Infer conservative operation quality only from returned evidence."""
    if isinstance(data, dict) and "error" in data:
        return {
            "coverage": 0.0,
            "confidence": 0.2,
            "partial": True,
            "warnings": [str(data["error"])],
        }
    if name == "recall" and arguments and arguments.get("grounded") is True:
        if isinstance(data, dict) and data.get("status") == "answered":
            claims = data.get("claims", [])
            citations = data.get("citations", [])
            citation_ids = {
                citation.get("citation_id")
                for citation in citations
                if isinstance(citation, dict) and citation.get("citation_id")
            }
            verified_claims = sum(
                1
                for claim in claims
                if isinstance(claim, dict)
                and claim.get("citation_ids")
                and set(claim["citation_ids"]).issubset(citation_ids)
            )
            verification_ratio = verified_claims / len(claims) if claims else 0.0
            return {
                "coverage": 0.0,
                "confidence": min(0.8, verification_ratio),
                "partial": verification_ratio < 1.0,
                "warnings": ["Grounded answer coverage is unknown."],
            }
        reason = data.get("reason") if isinstance(data, dict) else None
        return {
            "coverage": 0.0,
            "confidence": 0.0,
            "partial": True,
            "warnings": [str(reason or "Grounded QA abstained without a reason.")],
        }
    if name == "check_contradiction":
        if isinstance(data, list):
            if not data:
                return {
                    "coverage": 0.1,
                    "confidence": 0.3,
                    "fallback": True,
                    "partial": True,
                    "warnings": [
                        "Search returned no results; retrieval coverage is unknown."
                    ],
                }
            fused = any(
                isinstance(result, dict)
                and any(key in result for key in ("fused_score", "vector_score"))
                for result in data
            )
            quality = (
                {"coverage": 0.9, "confidence": 0.8}
                if fused
                else {
                    "coverage": 0.6,
                    "confidence": 0.6,
                    "fallback": True,
                    "partial": True,
                    "warnings": ["Only BM25 retrieval evidence is available."],
                }
            )
            return _degrade_quality(
                quality,
                "Contradiction candidates are unverified.",
                coverage=0.6,
                confidence=0.45,
            )
        validity = data.get("validity", {}) if isinstance(data, dict) else {}
        evidence = data.get("evidence", []) if isinstance(data, dict) else []
        if isinstance(validity, dict) and validity.get("status") == "verified":
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
            "warnings": [
                "Claim evidence is unsupported; recommendations are quarantined."
            ],
        }
    if name in {"recall", "get_decisions"}:
        results = data.get("results", []) if name == "recall" else data
        if not isinstance(results, list) or not results:
            quality = {
                "coverage": 0.1,
                "confidence": 0.3,
                "fallback": True,
                "partial": True,
                "warnings": ["Search returned no results; retrieval coverage is unknown."],
            }
        else:
            fused = any(
                isinstance(result, dict)
                and any(key in result for key in ("fused_score", "vector_score"))
                for result in results
            )
            if not fused:
                quality = {
                    "coverage": 0.6,
                    "confidence": 0.6,
                    "fallback": True,
                    "partial": True,
                    "warnings": ["Only BM25 retrieval evidence is available."],
                }
            else:
                quality = {"coverage": 0.9, "confidence": 0.8}
        if limit_clamped:
            quality = _degrade_quality(
                quality,
                "Requested limit was clamped to the safe 1-20 range.",
                coverage=0.8,
                confidence=0.8,
            )
        return quality
    if name == "get_architecture" and arguments and arguments.get("mode") == "impact":
        classification = data.get("classification") if isinstance(data, dict) else None
        if classification == "exact":
            return {"coverage": 0.9, "confidence": 0.9}
        if classification == "conservative":
            return {
                "coverage": 0.6,
                "confidence": 0.55,
                "partial": True,
                "warnings": list(data.get("warnings", [])) or ["Impact analysis is conservative."],
            }
        return {
            "coverage": 0.2,
            "confidence": 0.25,
            "partial": True,
            "warnings": list(data.get("warnings", [])) or ["Impact analysis is unresolved."],
        }
    if name in {"find_dead_code", "get_architecture"}:
        if isinstance(data, dict) and data.get("fallback") is False:
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
                "warnings": [
                    f"Active code graph has {unresolved} unresolved observations."
                ],
            }
        return {
            "coverage": 0.6,
            "confidence": 0.55,
            "fallback": True,
            "partial": True,
            "warnings": ["Live static extraction fallback is incomplete."],
        }
    if name == "doctor":
        overall = data.get("overall_status") if isinstance(data, dict) else None
        if overall == "ok":
            return {"coverage": 0.9, "confidence": 0.85}
        if overall == "error":
            codes = data.get("codes", []) if isinstance(data, dict) else []
            return {
                "coverage": 0.2,
                "confidence": 0.3,
                "partial": True,
                "warnings": [
                    "Doctor operator failed"
                    + (f": {', '.join(str(code) for code in codes[:3])}" if codes else ".")
                ],
            }
        if overall == "degraded":
            codes = data.get("codes", []) if isinstance(data, dict) else []
            return {
                "coverage": 0.7,
                "confidence": 0.7,
                "partial": True,
                "warnings": [
                    "Doctor status is degraded"
                    + (f": {', '.join(str(code) for code in codes[:3])}" if codes else ".")
                ],
            }
        from doctor import degraded_summary

        warning = degraded_summary(data) or "Doctor health is unknown."
        return {
            "coverage": 0.75,
            "confidence": 0.75,
            "partial": True,
            "warnings": [warning],
        }
    if name == "get_context" and arguments:
        requested = arguments.get("slugs", [])
        missing = data.get("missing_slugs", []) if isinstance(data, dict) else []
        if requested and missing:
            ratio = max(0.0, (len(requested) - len(missing)) / len(requested))
            return {
                "coverage": ratio,
                "confidence": 0.8,
                "partial": True,
                "warnings": ["Some requested pages were unavailable."],
            }
    return {}


def _resource_quality(data) -> dict:
    if isinstance(data, dict) and "error" in data:
        return _quality_for("resource", data)
    status = data.get("status", data) if isinstance(data, dict) else {}
    if not isinstance(status, dict):
        status = {}
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
    backlog = status.get("compile_backlog", 0)
    if isinstance(backlog, (int, float)) and backlog > 0:
        return {
            "coverage": 0.7,
            "confidence": 0.7,
            "partial": True,
            "warnings": [f"Compile backlog contains {backlog} daily file(s)."],
        }
    return {"coverage": 0.9, "confidence": 0.85}


def _build_operation_envelope(
    data,
    quality: dict | None = None,
    *,
    components: dict[str, dict[str, object]] | None = None,
) -> dict:
    envelope = build_envelope(data, components=components, **(quality or {}))
    if components and envelope["freshness"] == "stale":
        limit = 0.6 if envelope["freshness"] == "stale" else 0.4
        envelope["coverage"] = min(envelope["coverage"], limit)
        envelope["confidence"] = min(envelope["confidence"], limit)
        envelope["partial"] = True
        warning = "One or more response components are stale."
        if warning not in envelope["warnings"]:
            envelope["warnings"].append(warning)
    envelope["warnings"] = _sanitize_diagnostic(envelope["warnings"])
    envelope["data"] = _sanitize_error_fields(envelope["data"])
    return envelope


def _components_for(name: str, data) -> dict[str, dict[str, object]]:
    if name == "recall" and isinstance(data, dict):
        trace = data.get("retrieval_trace")
        if isinstance(trace, dict):
            generation = trace.get("corpus_generation")
            signals = set(trace.get("signals_used", []))
            components = {
                signal: {
                    "generation": generation,
                    "freshness": "fresh" if signal in signals else "missing",
                }
                for signal in ("lexical", "dense", "graph")
            }
            components["reranker"] = {
                "generation": generation,
                "freshness": "fresh"
                if trace.get("reranker_applied")
                else "missing"
                if trace.get("reranker_fallback_reason")
                else "unknown",
            }
            return components
    if name == "get_context" and isinstance(data, dict) and "packing_trace" in data:
        return {
            "context_compiler": {
                "generation": data.get("corpus_generation"),
                "freshness": "fresh",
            }
        }
    if (
        name in {"get_architecture", "find_dead_code"}
        and isinstance(data, dict)
        and any(
            key in data
            for key in ("source_generation", "graph_complete", "fallback")
        )
    ):
        return {
            "graph": {
                "generation": data.get("source_generation"),
                "freshness": "fresh" if "error" not in data else "unknown",
            }
        }
    return {}


def _execute_tool_call(name: str, arguments, operation_deadline: float) -> str:
    """Execute one tool under the absolute deadline created by its async handler."""
    import json

    deadline_token = _OPERATION_DEADLINE.set(operation_deadline)
    limit_clamped = False
    try:
        if name not in TOOL_INPUT_SCHEMAS:
            data = {"error": f"Unknown tool: {name}"}
        elif validation_error := _validate_tool_arguments(name, arguments):
            data = {"error": validation_error}
        else:
            try:
                _check_deadline(operation_deadline)
                if name == "recall":
                    if arguments.get("grounded", False):
                        from memory_state import ROOT
                        from query_memory import grounded_qa

                        data = grounded_qa(
                            arguments["query"],
                            vault=ROOT,
                            profile=arguments.get("profile"),
                            deadline=operation_deadline,
                        )
                    else:
                        effective_limit, limit_clamped = _clamped_limit(
                            arguments.get("limit", 8)
                        )
                        results = _call_with_deadline(
                            _search_vault,
                            arguments["query"],
                            limit=effective_limit,
                            deadline=operation_deadline,
                        )
                        data = {
                            "results": results,
                            "retrieval_trace": _retrieval_trace(arguments["query"], results),
                            "_meta": _call_with_deadline(
                                _meta, deadline=operation_deadline
                            ),
                        }
                elif name == "read_page":
                    data = _call_with_deadline(
                        _read_page, arguments["slug"], deadline=operation_deadline
                    )
                elif name == "wiki_overview":
                    data = _call_with_deadline(
                        _wiki_overview, deadline=operation_deadline
                    )
                    data["_meta"] = _call_with_deadline(
                        _meta, deadline=operation_deadline
                    )
                elif name == "vault_status":
                    data = _call_with_deadline(
                        _vault_status, deadline=operation_deadline
                    )
                elif name == "get_decisions":
                    effective_limit, limit_clamped = _clamped_limit(
                        arguments.get("limit", 10)
                    )
                    data = _call_with_deadline(
                        _get_decisions,
                        arguments.get("query"),
                        limit=effective_limit,
                        deadline=operation_deadline,
                    )
                elif name == "get_context":
                    context_options = (
                        {"token_budget": arguments["token_budget"]}
                        if "token_budget" in arguments
                        else {}
                    )
                    data = _call_with_deadline(
                        _get_context,
                        arguments["slugs"],
                        arguments.get("include"),
                        **context_options,
                        deadline=operation_deadline,
                    )
                elif name == "check_contradiction":
                    data = _call_with_deadline(
                        _check_contradiction,
                        arguments["claim"],
                        deadline=operation_deadline,
                    )
                elif name == "log_decision":
                    data = _call_with_deadline(
                        _log_decision,
                        arguments["summary"],
                        arguments.get("rationale", ""),
                        deadline=operation_deadline,
                    )
                elif name == "compile":
                    data = _call_with_deadline(
                        _trigger_compile, deadline=operation_deadline
                    )
                elif name == "find_dead_code":
                    data = _call_with_deadline(
                        _find_dead_code,
                        arguments["directory"],
                        live=arguments.get("live", False),
                        deadline=operation_deadline,
                    )
                elif name == "get_architecture":
                    if arguments.get("mode", "summary") == "impact":
                        data = _analyze_impact(
                            directory=arguments["directory"],
                            comparison=arguments.get("comparison", "dirty"),
                            base=arguments.get("base"),
                            target=arguments.get("target"),
                            branch=arguments.get("branch"),
                            deadline=operation_deadline,
                        )
                    elif arguments.get("mode", "summary") == "summary":
                        data = _call_with_deadline(
                            _get_architecture,
                            arguments["directory"],
                            live=arguments.get("live", False),
                            deadline=operation_deadline,
                        )
                    else:
                        data = _get_architecture_mode(
                            arguments["directory"],
                            mode=arguments["mode"],
                            symbol=arguments.get("symbol"),
                            target=arguments.get("target"),
                            reverse=arguments.get("reverse", False),
                            live=arguments.get("live", False),
                            deadline=operation_deadline,
                        )
                else:
                    data = _call_with_deadline(
                        _doctor, **arguments, deadline=operation_deadline
                    )
            except Exception as error:
                data = {"error": _safe_exception_text(error)}

        quality = _quality_for(
            name,
            data,
            arguments if isinstance(arguments, dict) else None,
            limit_clamped=limit_clamped,
        )
        envelope = _build_operation_envelope(
            data,
            quality,
            components=_components_for(name, data),
        )
        return json.dumps(envelope, indent=2, ensure_ascii=False, allow_nan=False)
    finally:
        _OPERATION_DEADLINE.reset(deadline_token)


async def _handle_tool_call(name: str, arguments) -> str:
    """Handle a tool call without blocking unrelated MCP event-loop work."""
    operation_deadline = time.monotonic() + MCP_OPERATION_SECONDS
    try:
        return await _run_bounded(
            _execute_tool_call, name, arguments, operation_deadline, deadline=operation_deadline
        )
    except TimeoutError:
        return _timeout_envelope_text()


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
        data = {"error": _safe_exception_text(error)}
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
        data = structured.get("data")
        repair_failed = (
            isinstance(data, dict)
            and any(
                isinstance(check, dict)
                and bool(check.get("details", {}).get("repair_errors"))
                for check in data.get("checks", [])
            )
        )
        is_error = isinstance(data, dict) and (
            "error" in data
            or data.get("overall_status") == "error"
            or repair_failed
        )
        return CallToolResult(
            content=text_content,
            structuredContent=structured,
            isError=is_error,
        )
    if MCP_STRUCTURED_OUTPUT_AVAILABLE:
        return text_content, structured
    return text_content


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
        operation_deadline = time.monotonic() + MCP_OPERATION_SECONDS
        try:
            return await _run_bounded(
                _execute_formatted_tool_call,
                name,
                arguments,
                operation_deadline,
                deadline=operation_deadline,
            )
        except TimeoutError:
            return timeout_result

    return call_tool


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

    async def main():
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    asyncio.run(main())
    return 0


if __name__ == "__main__":
    raise SystemExit(run_server())
