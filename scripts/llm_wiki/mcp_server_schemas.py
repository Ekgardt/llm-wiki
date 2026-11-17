"""The input schema of every tool."""
from __future__ import annotations

from answer_budget import (
    MAX_BUDGET_TOKENS as ANSWER_BUDGET_MAX_TOKENS,
)
from answer_budget import (
    MIN_BUDGET_TOKENS as ANSWER_BUDGET_MIN_TOKENS,
)
from llm_wiki.mcp_server_limits import MAX_MCP_SLUG_LENGTH
from retrieval import PROFILES as QA_PROFILES


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
# The ceiling `code_graph.DEPENDENCY_MAX_DEPTH` enforces, stated here so the
# tool schema does not import `code_graph` at module load — that import pulls
# the whole graph stack in before the server can answer anything.
# `test_architecture_depth_argument.py` pins the two to the same number.
ARCHITECTURE_MAX_DEPTH = 8

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
                "uniqueItems": True,
                "items": {"type": "string", "maxLength": MAX_MCP_SLUG_LENGTH},
                "description": "List of page slugs",
            },
            "include": {
                "type": "array",
                "uniqueItems": True,
                "items": {"type": "string"},
                "description": "Accepted for compatibility and ignored; page content is already in `text`",
            },
            "token_budget": {
                "type": "integer",
                "minimum": 1,
                "default": 8192,
                "description": "Estimated token allowance for the context package; MCP envelope and telemetry add overhead",
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
            "symbol": {
                "type": "string",
                "maxLength": 256,
                "description": (
                    "Answer about one function by name: a verdict plus only its "
                    "rows, instead of the whole-repository candidate listing"
                ),
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
                    "data_flow",
                    "cross_service",
                    "provenance",
                    "snippet",
                    "coverage",
                    "index",
                    "repositories",
                    "changes",
                    "search",
                ],
                "description": "Bounded architecture query mode",
            },
            "roots": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 255},
                "minItems": 1,
                "maxItems": 128,
                "description": (
                    "CODE-03 index mode: top-level directories of the "
                    "repository to cover. Omit to use every Git-tracked "
                    "top-level directory, which refuses by name if any of them "
                    "is one the corpus collector will not accept"
                ),
            },
            "symbol": {
                "type": "string",
                "minLength": 1,
                "maxLength": 1024,
                "description": (
                    "Symbol for symbol, callers, callees, dependencies, path, "
                    "provenance, and snippet modes (snippet accepts owner.name); "
                    "name pattern for search mode (* and ? are globs, otherwise "
                    "substring)"
                ),
            },
            "query": {
                "type": "string",
                "minLength": 2,
                "maxLength": 4096,
                "description": "Bounded JSON hop pipeline for mode=query",
            },
            "reverse": {"type": "boolean", "default": False},
            "depth": {
                "type": "integer",
                "minimum": 1,
                "maximum": ARCHITECTURE_MAX_DEPTH,
                "description": (
                    "dependencies mode: how many hops to walk; omitted means "
                    "the whole reachable set. callers and callees modes: walk "
                    "the CALLS closure this deep; omitted means one hop."
                ),
            },
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
                "description": (
                    "Repository-relative path for coverage mode and for precise "
                    "or positioned calls; repository-relative path prefix for "
                    "search mode"
                ),
            },
            "line": {"type": "integer", "minimum": 1, "description": "1-based line of the position"},
            "character": {
                "type": "integer",
                "minimum": 0,
                "description": (
                    "0-based UTF-8 byte offset within the line (not UTF-16 "
                    "code units); past the line end means the line end"
                ),
            },
            "offset": {"type": "integer", "minimum": 0, "default": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10},
            **ANSWER_BUDGET_SCHEMA_FIELDS,
        },
        "required": ["directory"],
    },
    "doctor": DOCTOR_INPUT_SCHEMA,
}
