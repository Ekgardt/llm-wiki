"""Tool definitions and the validation of tool arguments against their schemas."""
from __future__ import annotations

from pathlib import Path

from llm_wiki import mcp_server_limits
from llm_wiki.mcp_server_limits import (
    PRECISE_ARCHITECTURE_MODES,
    Tool,
)
from llm_wiki.mcp_server_schemas import (
    ANSWER_BUDGET_SCHEMA_FIELDS,
    TOOL_INPUT_SCHEMAS,
)
from llm_wiki.mcp_server_workers import check_deadline
from mcp_contract import envelope_schema


def validated_code_directory(
    directory: str, *, deadline: float | None = None
) -> tuple[Path | None, str | None]:
    """Validate an explicitly supplied, bounded local project directory."""
    if not isinstance(directory, str) or not directory.strip():
        return None, "directory is required"
    check_deadline(deadline)
    candidate = Path(directory).expanduser()
    check_deadline(deadline)
    if not candidate.is_absolute():
        return None, "directory must be an absolute local path"
    resolved = candidate.resolve()
    check_deadline(deadline)
    error = _code_directory_error(resolved)
    check_deadline(deadline)
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


def build_tool_definitions() -> list:
    """Build MCP tool definitions."""
    if not mcp_server_limits.MCP_AVAILABLE:
        return []

    return [
        _make_tool(
            name="recall",
            description="Search the knowledge vault. Returns ranked results with titles, paths, and the matching text. Use this to find relevant knowledge pages.",
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
            description="Read current operational vault health with explicit coverage, or perform a named operator action. Full historical diagnosis is available through doctor.py --scope full.",
            inputSchema=TOOL_INPUT_SCHEMAS["doctor"],
        ),
    ]


def _make_tool(**kwargs):
    """Create a tool with structured output metadata when the SDK supports it."""
    if mcp_server_limits.MCP_STRUCTURED_OUTPUT_AVAILABLE:
        kwargs["outputSchema"] = envelope_schema()
    return Tool(**kwargs)


def meta(*, deadline: float | None = None) -> dict:
    """Build the legacy payload metadata retained inside envelope data."""
    from datetime import datetime

    from lookup_mode import count_wiki_pages
    from memory_state import load_state

    check_deadline(deadline)
    state = load_state()
    check_deadline(deadline)
    return {
        "page_count": count_wiki_pages(),
        "last_compile": state.get("last_compile_at", "never"),
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }


def validate_tool_arguments(name: str, arguments) -> str | None:
    """Validate the small declared schema subset before helper dispatch."""
    if not isinstance(arguments, dict):
        return "arguments must be an object"
    schema = TOOL_INPUT_SCHEMAS[name]
    if "oneOf" in schema:
        return _validate_one_of(schema, arguments)
    return _validate_object_then_specific(name, schema, arguments)


def _validate_object_then_specific(name: str, schema: dict, arguments: dict):
    """The declared object schema first, then the tool's own argument rules."""
    error = validate_object_schema(
        schema,
        arguments,
        reject_unknown=name == "get_architecture",
    )
    if error is not None:
        return error
    return _validate_tool_specific_arguments(name, arguments)


def _validate_one_of(schema: dict, arguments: dict) -> str | None:
    errors = [
        validate_object_schema(branch, arguments) for branch in schema["oneOf"]
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
        return validate_architecture_arguments(arguments)
    return None


_ARCHITECTURE_POSITION_KEYS = ("path", "line", "character")

ARCHITECTURE_CONTRACTS = {
    "summary": ({"directory"}, {"directory", "mode", "live", "limit"}),
    "symbol": (
        {"directory", "mode", "symbol"},
        {"directory", "mode", "symbol", "live"},
    ),
    # Issue #24, B4: `depth` walks the CALLS closure; omitted is one hop.
    "callers": (
        {"directory", "mode", "symbol"},
        {"directory", "mode", "symbol", "live", "depth"},
    ),
    "callees": (
        {"directory", "mode", "symbol"},
        {"directory", "mode", "symbol", "live", "depth"},
    ),
    # Issue #24, B2: ranked name search over the whole generation; `symbol`
    # is the pattern and `path` an optional repository-relative prefix.
    "search": (
        {"directory", "mode", "symbol"},
        {"directory", "mode", "symbol", "path", "limit"},
    ),
    "dependencies": (
        {"directory", "mode", "symbol"},
        {"directory", "mode", "symbol", "reverse", "live", "depth"},
    ),
    "path": (
        {"directory", "mode", "symbol", "target"},
        {"directory", "mode", "symbol", "target", "live"},
    ),
    "community": (
        {"directory", "mode"},
        # `symbol` is optional and narrows the answer to the communities that
        # symbol belongs to. Measured 2026-08-28: the whole-graph listing
        # cannot be both complete and bounded here — naming all 4,078
        # communities costs 899,071 tokens against a 25,000 ceiling — so
        # "which module does X belong to" needs an anchor, exactly as
        # who-calls does. Anchored, the same question costs 291 tokens.
        {"directory", "mode", "symbol", "live"},
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
    # Issue #24, B3: argument bindings hop by hop, never data-flow analysis
    # (docs/research/2026-09-11-argument-bindings-and-route-calls.md).
    "data_flow": (
        {"directory", "mode", "symbol"},
        {"directory", "mode", "symbol", "depth"},
    ),
    # Issue #24, B3: calls plus the HTTP boundary, same bounds as `data_flow`.
    "cross_service": (
        {"directory", "mode", "symbol"},
        {"directory", "mode", "symbol", "depth"},
    ),
    "impact": (
        {"directory", "mode"},
        {"directory", "mode", "comparison", "base", "target", "branch"},
    ),
    # CODE-03. `index` is the one mode that writes, and what it writes is a
    # generation under `cache/evidence-graph/` -- disposable derived cache in
    # this vault. It never writes into the repository it is pointed at, and it
    # never activates: the single active pointer belongs to the vault.
    "index": ({"directory", "mode"}, {"directory", "mode", "roots"}),
    "repositories": ({"directory", "mode"}, {"directory", "mode"}),
    "changes": ({"directory", "mode"}, {"directory", "mode"}),
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
    return ARCHITECTURE_CONTRACTS[mode]


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


def validate_architecture_arguments(arguments: dict) -> str | None:
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


# Modes whose `path` names one file that is then read from disk. `search` is
# absent on purpose: its `path` is a prefix compared inside the graph, never
# opened. Audit 3, A2:
# `docs/research/2026-09-17-coverage-reads-only-inside-the-repository.md`.
FILE_READING_ARCHITECTURE_MODES = PRECISE_ARCHITECTURE_MODES | {"coverage"}


def _positioned_architecture_path_error(arguments: dict, mode: str, positioned: bool):
    """Every call that opens the file its path names carries a path to validate."""
    if mode in FILE_READING_ARCHITECTURE_MODES or positioned:
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


def validate_object_schema(
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
