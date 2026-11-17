"""LLM-Wiki MCP Server — 12 task-shaped tools, stdio transport, 100% local.

Gives AI agents (Claude Code, OpenCode, Codex) structured
access to the knowledge vault via Model Context Protocol. No server, no cloud,
no network — stdio subprocess on the same machine.

Install: uv sync --locked (MCP is a base dependency)
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
  get_context(slugs)          — batch page context under a token budget
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

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Run as a program, this file is the supervisor that restarts the server when its
# code changes; the server itself runs in a child with LLM_WIKI_MCP_WORKER=1. See
# `docs/research/2026-09-24-the-memory-server-reloads-its-own-code.md`.
if __name__ == "__main__" and os.environ.get("LLM_WIKI_MCP_WORKER") != "1":
    import mcp_supervisor

    raise SystemExit(mcp_supervisor.main(sys.argv[1:]))

from llm_wiki.mcp_server_architecture import (  # noqa: E402
    ARCHITECTURE_REPORT_KEYS,
    COMMUNITY_MEMBERS_HINT,
    architecture_answer,
    architecture_callers,
    architecture_community,
    architecture_definition,
    architecture_dependencies,
    find_dead_code_answer,
    get_architecture_mode,
    refresh_action,
    repository_freshness,
    request_repository_refresh,
    request_worktree_follow,
    with_generation_freshness,
    worktree_follow_fields,
)
from llm_wiki.mcp_server_doctor import (  # noqa: E402
    doctor,
)
from llm_wiki.mcp_server_freshness import (  # noqa: E402
    NAVIGATION_FRESHNESS,
    answer_freshness,
    answer_generation,
    components_for,
    file_sha256,
    graph_components,
    index_timestamp,
    navigation_freshness,
    recall_components,
    recorded_memory_digests,
    requested_signals,
)
from llm_wiki.mcp_server_limits import (  # noqa: E402
    CONTEXT_RESOURCE_URI,
    HEALTH_RESOURCE_URI,
    LEXICAL_FALLBACK_RESERVE_SECONDS,
    MAX_MCP_EVIDENCE_BYTES,
    MAX_NAVIGATION_GRAPH_FACTS,
    MAX_NAVIGATION_SOURCE_BYTES,
    MAX_NAVIGATION_SOURCE_CACHE_BYTES,
    MCP_AVAILABLE,
    MCP_LSP_STARTUP_SECONDS,
    MCP_OPERATION_SECONDS,
    MCP_REPOSITORY_INDEX_SECONDS,
    MCP_RESOURCES_AVAILABLE,
    MCP_STRUCTURED_OUTPUT_AVAILABLE,
    MCP_WORKER_SLOTS,
    PRECISE_ARCHITECTURE_MODES,
    RETRIEVAL_TRACE_SCHEMA,
)
from llm_wiki.mcp_server_navigation import (  # noqa: E402
    NavigationSourceCache,
    close_navigation_session_manager,
    collected_call_locations,
    get_precise_architecture,
    graph_call_locations,
    graph_declaration_locations,
    graph_node_ids_at_anchor,
    navigation_edge_verifier,
    navigation_location_from_span,
    navigation_named_by_session,
    navigation_profile,
    navigation_session_manager,
    navigation_source_bytes,
    navigation_structural_candidates,
    navigation_symbol_resolver,
    normalized_navigation_failure,
    open_navigation_graph,
    same_filesystem_path,
)
from llm_wiki.mcp_server_quality import (  # noqa: E402
    EXACT_ANSWER_TOOLS,
    build_operation_envelope,
    quality_for,
    quality_of_code_graph,
    quality_of_results,
)
from llm_wiki.mcp_server_runtime import (  # noqa: E402
    SHUTDOWN_INFERENCE_SECONDS,
    build_resource_definitions,
    build_server,
    format_tool_result,
    handle_resource_read,
    register_resources,
    register_tools,
    run_server,
    settle_inference,
)
from llm_wiki.mcp_server_sanitize import (  # noqa: E402
    sanitize_navigation_text,
)
from llm_wiki.mcp_server_schemas import (  # noqa: E402
    ARCHITECTURE_MAX_DEPTH,
    TOOL_INPUT_SCHEMAS,
)
from llm_wiki.mcp_server_tools import (  # noqa: E402
    DIRECTORY_CHECKED_MODES,
    TOOL_HANDLERS,
    answer_text,
    architecture_tool_call,
    cross_service_architecture_call,
    data_flow_architecture_call,
    dispatch_tool,
    execute_tool_call,
    handle_tool_call,
    shaped_code_answer,
    tool_call_data,
    tool_call_envelope,
    tool_get_architecture,
    tool_get_decisions,
    tool_timeout_envelope_text,
)
from llm_wiki.mcp_server_validation import (  # noqa: E402
    ARCHITECTURE_CONTRACTS,
    build_tool_definitions,
    validate_architecture_arguments,
    validate_object_schema,
    validate_tool_arguments,
)
from llm_wiki.mcp_server_vault import (  # noqa: E402
    agent_row,
    assess_contradiction_text,
    check_contradiction,
    compiled_context,
    context_result,
    get_context,
    get_decisions,
    log_decision,
    read_page,
    reported_trace,
    retrieval_trace,
    search_vault,
    selected_chunks,
    trigger_compile,
    unreported_trace,
    vault_status,
    wiki_overview,
)
from llm_wiki.mcp_server_warmup import (  # noqa: E402
    WARMUP_LIMIT_SECONDS,
    WARMUP_PASSES,
    start_encoder_warmup,
    warmup_retrieval_path,
    warmup_stage,
    warmup_state,
)
from llm_wiki.mcp_server_workers import (  # noqa: E402
    CODE_GRAPH_SLOTS,
    CODE_GRAPH_WORK_SLOTS,
    bounded_operation_deadline,
    check_deadline,
    is_precise_architecture_request,
    retrieval_operation_seconds,
    run_bounded,
    timeout_envelope_text,
    tool_operation_seconds,
)

__all__ = [
    "ARCHITECTURE_CONTRACTS",
    "ARCHITECTURE_MAX_DEPTH",
    "ARCHITECTURE_REPORT_KEYS",
    "CODE_GRAPH_SLOTS",
    "CODE_GRAPH_WORK_SLOTS",
    "COMMUNITY_MEMBERS_HINT",
    "CONTEXT_RESOURCE_URI",
    "DIRECTORY_CHECKED_MODES",
    "EXACT_ANSWER_TOOLS",
    "HEALTH_RESOURCE_URI",
    "LEXICAL_FALLBACK_RESERVE_SECONDS",
    "MAX_MCP_EVIDENCE_BYTES",
    "MAX_NAVIGATION_GRAPH_FACTS",
    "MAX_NAVIGATION_SOURCE_BYTES",
    "MAX_NAVIGATION_SOURCE_CACHE_BYTES",
    "MCP_AVAILABLE",
    "MCP_LSP_STARTUP_SECONDS",
    "MCP_OPERATION_SECONDS",
    "MCP_REPOSITORY_INDEX_SECONDS",
    "MCP_RESOURCES_AVAILABLE",
    "MCP_STRUCTURED_OUTPUT_AVAILABLE",
    "MCP_WORKER_SLOTS",
    "NAVIGATION_FRESHNESS",
    "NavigationSourceCache",
    "PRECISE_ARCHITECTURE_MODES",
    "RETRIEVAL_TRACE_SCHEMA",
    "SHUTDOWN_INFERENCE_SECONDS",
    "TOOL_HANDLERS",
    "TOOL_INPUT_SCHEMAS",
    "WARMUP_LIMIT_SECONDS",
    "WARMUP_PASSES",
    "agent_row",
    "answer_freshness",
    "answer_generation",
    "answer_text",
    "architecture_answer",
    "architecture_callers",
    "architecture_community",
    "architecture_definition",
    "architecture_dependencies",
    "architecture_tool_call",
    "assess_contradiction_text",
    "bounded_operation_deadline",
    "build_operation_envelope",
    "build_resource_definitions",
    "build_server",
    "build_tool_definitions",
    "check_contradiction",
    "check_deadline",
    "close_navigation_session_manager",
    "collected_call_locations",
    "compiled_context",
    "components_for",
    "context_result",
    "cross_service_architecture_call",
    "data_flow_architecture_call",
    "dispatch_tool",
    "doctor",
    "execute_tool_call",
    "file_sha256",
    "find_dead_code_answer",
    "format_tool_result",
    "get_architecture_mode",
    "get_context",
    "get_decisions",
    "get_precise_architecture",
    "graph_call_locations",
    "graph_components",
    "graph_declaration_locations",
    "graph_node_ids_at_anchor",
    "handle_resource_read",
    "handle_tool_call",
    "index_timestamp",
    "is_precise_architecture_request",
    "log_decision",
    "navigation_edge_verifier",
    "navigation_freshness",
    "navigation_location_from_span",
    "navigation_named_by_session",
    "navigation_profile",
    "navigation_session_manager",
    "navigation_source_bytes",
    "navigation_structural_candidates",
    "navigation_symbol_resolver",
    "normalized_navigation_failure",
    "open_navigation_graph",
    "quality_for",
    "quality_of_code_graph",
    "quality_of_results",
    "read_page",
    "recall_components",
    "recorded_memory_digests",
    "refresh_action",
    "register_resources",
    "register_tools",
    "reported_trace",
    "repository_freshness",
    "request_repository_refresh",
    "request_worktree_follow",
    "requested_signals",
    "retrieval_operation_seconds",
    "retrieval_trace",
    "run_bounded",
    "same_filesystem_path",
    "sanitize_navigation_text",
    "search_vault",
    "selected_chunks",
    "settle_inference",
    "shaped_code_answer",
    "start_encoder_warmup",
    "timeout_envelope_text",
    "tool_call_data",
    "tool_call_envelope",
    "tool_get_architecture",
    "tool_get_decisions",
    "tool_operation_seconds",
    "tool_timeout_envelope_text",
    "trigger_compile",
    "unreported_trace",
    "validate_architecture_arguments",
    "validate_object_schema",
    "validate_tool_arguments",
    "vault_status",
    "warmup_retrieval_path",
    "warmup_stage",
    "warmup_state",
    "wiki_overview",
    "with_generation_freshness",
    "worktree_follow_fields",
]

if __name__ == "__main__":
    raise SystemExit(run_server())
