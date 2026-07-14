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
  compile(scope)             — trigger compile (non-blocking)
  find_dead_code(directory)  — conservative zero-confirmed-caller candidates
  get_architecture(directory) — entry points, routes, hotspots, communities
  doctor(repair)            — local health and optional safe repairs
"""
from __future__ import annotations

import sys
from pathlib import Path, PureWindowsPath

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bounded_io import read_stable_bytes  # noqa: E402

MAX_MCP_PAGE_BYTES = 4 * 1024 * 1024
MAX_MCP_EVIDENCE_BYTES = 64 * 1024
MAX_MCP_TOTAL_EVIDENCE_BYTES = 256 * 1024

MCP_AVAILABLE = False
MCP_RESOURCES_AVAILABLE = False
MCP_STRUCTURED_OUTPUT_AVAILABLE = False
MCP_CALL_TOOL_RESULT_AVAILABLE = False
Resource = None
TextResourceContents = None
CallToolResult = None
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

HEALTH_RESOURCE_URI = "llm-wiki://health"
CONTEXT_RESOURCE_URI = "llm-wiki://context"

TOOL_INPUT_SCHEMAS = {
    "recall": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "limit": {
                "type": "integer",
                "default": 8,
                "description": "Requested result count; execution clamps it to 1-20",
            },
        },
        "required": ["query"],
    },
    "read_page": {
        "type": "object",
        "properties": {
            "slug": {
                "type": "string",
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
            "query": {"type": "string", "description": "Optional filter query"},
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
                "items": {"type": "string"},
                "description": "List of page slugs",
            },
            "include": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional strings; 'frontmatter' adds content_preview for backward compatibility",
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
        },
        "required": ["directory"],
    },
    "doctor": {
        "type": "object",
        "properties": {
            "repair": {
                "type": "boolean",
                "default": False,
                "description": "Apply only safe, idempotent local repairs",
            },
        },
        "required": [],
    },
}


def _search_vault(query: str, limit: int = 8) -> list[dict]:
    """Run hybrid search on the vault."""
    from search_memory import search
    return search(query, limit=limit)


def _read_page(slug: str) -> dict:
    """Read a full page by slug."""
    from memory_state import ROOT, STATE_ROOT

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
    if not page_path.exists():
        return {"error": f"Page not found: {slug}"}
    try:
        content = read_stable_bytes(
            page_path, MAX_MCP_PAGE_BYTES, label="MCP page"
        ).decode("utf-8", errors="strict")
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return {"error": f"Page read failed for {slug}: {exc}"}
    from evidence_resolver import (
        EvidenceResolutionError,
        EvidenceResolver,
        extract_evidence_references,
    )

    evidence = []
    evidence_bytes = 0
    resolver = EvidenceResolver(ROOT, state_root=STATE_ROOT)
    try:
        for reference in extract_evidence_references(content):
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
        return {"error": f"Evidence resolution failed for {slug}: {exc}"}
    return {
        "slug": slug,
        "path": str(page_path.relative_to(ROOT)),
        "content": content,
        "evidence": evidence,
    }


def _wiki_overview() -> dict:
    """Get vault statistics and retrieval tier recommendation."""
    from lookup_mode import count_wiki_pages, tier_for
    from memory_state import ROOT

    count = count_wiki_pages()
    tier = tier_for(count)
    return {
        "page_count": count,
        "retrieval_tier": tier,
        "vault_root": str(ROOT),
    }


def _vault_status() -> dict:
    """Return compile status and current daily-file backlog."""
    from memory_state import ROOT, file_hash, load_state

    state = load_state()
    compiled = state.get("compiled_daily_hashes", {}) or {}
    try:
        daily_files = list((ROOT / "knowledge" / "daily").glob("*.md"))
    except OSError:
        daily_files = []
    backlog = 0
    for daily_path in daily_files:
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


def _get_decisions(query: str | None = None, limit: int = 10) -> list[dict]:
    """Get active decisions from the vault."""
    from search_memory import search
    results = search(query or "decision", limit=limit)
    # Filter to decision-type pages
    return [r for r in results if r.get("type") == "decision" or "decision" in r.get("path", "").lower()]


def _get_context(slugs: list[str], include: list[str] | None = None) -> dict:
    """Batch page context; ``frontmatter`` retains the legacy content preview."""
    include = include or []
    result = {}
    for slug in slugs:
        page = _read_page(slug)
        if "error" not in page:
            entry = {"slug": slug, "title": slug}
            if "content" in page and "frontmatter" in (include or []):
                entry["content_preview"] = page["content"][:500]
            result[slug] = entry
    return result


def _check_contradiction(claim: str) -> list[dict]:
    """Find pages that might contradict a claim."""
    from search_memory import search
    return search(claim, limit=5)


def _log_decision(summary: str, rationale: str = "") -> dict:
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
        path = append_daily(now.strftime("%Y-%m-%d"), slug, block)
        return {"status": "logged", "path": str(path)}
    except Exception as e:
        return {"error": str(e)}


def _trigger_compile() -> dict:
    """Trigger a compile (non-blocking)."""
    from maybe_compile import spawn_compile_if_idle
    spawned, reason = spawn_compile_if_idle()
    return {"spawned": spawned, "reason": reason}


def _find_dead_code(directory: str) -> dict:
    """Find conservative dead-code candidates in a project directory."""
    from code_graph import find_dead_code

    resolved, error = _validated_code_directory(directory)
    if error:
        return {"error": error}
    return {"directory": str(resolved), "candidates": find_dead_code(resolved)}


def _get_architecture(directory: str) -> dict:
    """Summarize the statically visible architecture of a project directory."""
    from code_graph import get_architecture

    resolved, error = _validated_code_directory(directory)
    if error:
        return {"error": error}
    return {"directory": str(resolved), "architecture": get_architecture(resolved)}


def _doctor(repair: bool = False) -> dict:
    """Run local health checks without repair unless explicitly requested."""
    from doctor import run_doctor

    return run_doctor(repair=repair)


def _validated_code_directory(directory: str) -> tuple[Path | None, str | None]:
    """Validate an explicitly supplied, bounded local project directory."""
    if not isinstance(directory, str) or not directory.strip():
        return None, "directory is required"
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
            description="Trigger a background compile of daily logs into knowledge pages. Non-blocking.",
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


def _meta() -> dict:
    """Build the legacy payload metadata retained inside envelope data."""
    from datetime import datetime

    from lookup_mode import count_wiki_pages
    from memory_state import load_state

    state = load_state()
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
    for key in schema["required"]:
        if key not in arguments:
            return f"required argument is missing: {key}"
    for key, value in arguments.items():
        field = schema["properties"].get(key)
        if field is None:
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
        if "minimum" in field and value < field["minimum"]:
            return f"argument '{key}' must be at least {field['minimum']}"
        if "maximum" in field and value > field["maximum"]:
            return f"argument '{key}' must be at most {field['maximum']}"
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
    if name in {"recall", "get_decisions", "check_contradiction"}:
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
        if name == "check_contradiction":
            quality = _degrade_quality(
                quality,
                "Contradiction candidates are unverified.",
                coverage=0.6,
                confidence=0.45,
            )
        if limit_clamped:
            quality = _degrade_quality(
                quality,
                "Requested limit was clamped to the safe 1-20 range.",
                coverage=0.8,
                confidence=0.8,
            )
        return quality
    if name in {"find_dead_code", "get_architecture"}:
        return {
            "coverage": 0.6,
            "confidence": 0.55,
            "partial": True,
            "warnings": ["Static code graph is incomplete."],
        }
    if name == "doctor":
        overall = data.get("overall_status") if isinstance(data, dict) else None
        if overall == "ok":
            return {"coverage": 0.9, "confidence": 0.85}
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
        if requested and isinstance(data, dict) and len(data) < len(requested):
            ratio = len(data) / len(requested)
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
    freshness_sensitive: bool = False,
) -> dict:
    envelope = build_envelope(data, **(quality or {}))
    if freshness_sensitive and envelope["freshness"] != "fresh":
        limit = 0.6 if envelope["freshness"] == "stale" else 0.4
        envelope["coverage"] = min(envelope["coverage"], limit)
        envelope["confidence"] = min(envelope["confidence"], limit)
        envelope["partial"] = True
        warning = f"Search index freshness is {envelope['freshness']}."
        if warning not in envelope["warnings"]:
            envelope["warnings"].append(warning)
    return envelope


async def _handle_tool_call(name: str, arguments) -> str:
    """Handle a tool call and return a compatible JSON text envelope."""
    import json

    limit_clamped = False
    if name not in TOOL_INPUT_SCHEMAS:
        data = {"error": f"Unknown tool: {name}"}
    elif validation_error := _validate_tool_arguments(name, arguments):
        data = {"error": validation_error}
    else:
        try:
            if name == "recall":
                effective_limit, limit_clamped = _clamped_limit(
                    arguments.get("limit", 8)
                )
                results = _search_vault(
                    arguments["query"], limit=effective_limit
                )
                data = {"results": results, "_meta": _meta()}
            elif name == "read_page":
                data = _read_page(arguments["slug"])
            elif name == "wiki_overview":
                data = _wiki_overview()
                data["_meta"] = _meta()
            elif name == "vault_status":
                data = _vault_status()
            elif name == "get_decisions":
                effective_limit, limit_clamped = _clamped_limit(
                    arguments.get("limit", 10)
                )
                data = _get_decisions(
                    arguments.get("query"),
                    limit=effective_limit,
                )
            elif name == "get_context":
                data = _get_context(arguments["slugs"], arguments.get("include"))
            elif name == "check_contradiction":
                data = _check_contradiction(arguments["claim"])
            elif name == "log_decision":
                data = _log_decision(
                    arguments["summary"], arguments.get("rationale", "")
                )
            elif name == "compile":
                data = _trigger_compile()
            elif name == "find_dead_code":
                data = _find_dead_code(arguments["directory"])
            elif name == "get_architecture":
                data = _get_architecture(arguments["directory"])
            else:
                data = _doctor(arguments.get("repair", False))
        except Exception as e:
            data = {"error": str(e)}

    quality = _quality_for(
        name,
        data,
        arguments if isinstance(arguments, dict) else None,
        limit_clamped=limit_clamped,
    )
    envelope = _build_operation_envelope(
        data,
        quality,
        freshness_sensitive=name
        in {"recall", "get_decisions", "check_contradiction"},
    )
    return json.dumps(envelope, indent=2, ensure_ascii=False, allow_nan=False)


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


def _handle_resource_read(uri: str) -> str:
    """Return one resource as a JSON text envelope."""
    import json

    try:
        if uri == HEALTH_RESOURCE_URI:
            data = _vault_status()
        elif uri == CONTEXT_RESOURCE_URI:
            data = {"overview": _wiki_overview(), "status": _vault_status()}
        else:
            data = {"error": f"Unknown resource: {uri}"}
    except Exception as e:
        data = {"error": str(e)}
    envelope = _build_operation_envelope(
        data, _resource_quality(data), freshness_sensitive=True
    )
    return json.dumps(envelope, indent=2, ensure_ascii=False, allow_nan=False)


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
        return [
            TextResourceContents(
                uri=uri,
                mimeType="application/json",
                text=_handle_resource_read(str(uri)),
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
        is_error = isinstance(data, dict) and ("error" in data or repair_failed)
        return CallToolResult(
            content=text_content,
            structuredContent=structured,
            isError=is_error,
        )
    if MCP_STRUCTURED_OUTPUT_AVAILABLE:
        return text_content, structured
    return text_content


def _register_tools(server, tools):
    """Register tool callbacks while disabling SDK-side validation when supported."""
    import inspect

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

    @decorator
    async def call_tool(name: str, arguments):
        result = await _handle_tool_call(name, arguments)
        return _format_tool_result(result)

    return call_tool


def run_server() -> int:
    """Start the MCP server (stdio transport). Returns exit code."""
    if not MCP_AVAILABLE:
        print(
            "MCP package not installed. Run: uv sync --extra mcp-server",
            file=sys.stderr,
        )
        return 1

    import asyncio

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
