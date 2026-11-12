"""The live workspace call graph: definitions, resolved call edges and symbol identities."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from .code_graph_parse import parsed_live_sources


def _index_workspace_definitions(
    path: Path,
    result: dict,
    directory: Path,
    definitions: dict[str, dict],
    by_name: dict[str, list[dict]],
    by_qualified: dict[str, dict],
) -> None:
    for function in result["functions"]:
        definition = {**function, "file": str(path), "language": result["language"]}
        definitions[function["symbol_id"]] = definition
        by_name.setdefault(function["name"], []).append(definition)
        if result["language"] == "python":
            qualified = _python_qualified_name(path, function, directory)
            by_qualified[qualified] = definition


def _unambiguous_candidate(candidates: list[dict], path: Path) -> dict | None:
    same_file = [item for item in candidates if item["file"] == str(path)]
    if len(same_file) == 1:
        return same_file[0]
    if len(candidates) == 1:
        return candidates[0]
    return None


def _resolved_python_target(call, by_qualified, by_name, path):
    """An unconfirmed Python call stays unresolved; it never falls back to a name."""
    if call.get("confidence") != "confirmed":
        return None
    confirmed = by_qualified.get(call.get("qualified_name"))
    if confirmed is not None:
        return confirmed
    return _unambiguous_candidate(by_name.get(call["name"], []), path)


def _resolved_call_target(
    call: dict,
    path: Path,
    language: str,
    by_name: dict[str, list[dict]],
    by_qualified: dict[str, dict],
) -> dict | None:
    """The one definition this call can name, or None when it stays ambiguous."""
    if language == "python":
        return _resolved_python_target(call, by_qualified, by_name, path)
    return _unambiguous_candidate(by_name.get(call["name"], []), path)


def _call_edge(path: Path, result: dict, call: dict, by_name, by_qualified) -> dict | None:
    caller = _containing_function(result["functions"], call)
    if caller is None:
        return None
    target = _resolved_call_target(
        call, path, result["language"], by_name, by_qualified
    )
    if target is None:
        return None
    return {"source": caller["symbol_id"], "target": target["symbol_id"]}


def _workspace_call_edges(
    parsed: list[tuple[Path, dict]],
    by_name: dict[str, list[dict]],
    by_qualified: dict[str, dict],
) -> list[dict]:
    edges = []
    for path, result in parsed:
        found = [
            _call_edge(path, result, call, by_name, by_qualified)
            for call in result["calls"]
        ]
        edges.extend(item for item in found if item is not None)
    return edges


def workspace_call_graph(
    directory: Path,
) -> tuple[list[tuple[Path, dict]], dict[str, dict], list[dict]]:
    """Parse a workspace and resolve calls to canonical path/owner/name IDs.

    The hidden-directory rule of `live_source_files` was the whole of NEW-110:
    measured on the live vault, 7,686 parsable files of which 7,261 — 94% — sat
    under `.claude/`, throwaway checkouts belonging to other agents.
    """
    directory = directory.resolve()
    parsed = parsed_live_sources(directory)
    definitions: dict[str, dict] = {}
    by_name: dict[str, list[dict]] = {}
    by_qualified: dict[str, dict] = {}
    for path, result in parsed:
        _annotate_function_ids(path, result, directory)
        _index_workspace_definitions(
            path, result, directory, definitions, by_name, by_qualified
        )
    return parsed, definitions, _workspace_call_edges(parsed, by_name, by_qualified)


def _function_owner(function: dict, containers: list[dict]) -> str:
    enclosing = [
        item for item in containers
        if item is not function and _symbol_contains(item, function)
    ]
    if not enclosing:
        return "<module>"
    return min(enclosing, key=_symbol_span)["name"]


def _annotated_identity(function: dict, containers: list[dict], relative: str) -> str:
    owner = _function_owner(function, containers)
    function["owner"] = owner
    identity = function.get("signature") or f"{function['name']}@L{function['line']}"
    return f"{relative}::{owner}::{identity}"


def _assign_symbol_id(function: dict, identity: str, duplicated: bool) -> None:
    if duplicated:
        function["symbol_id"] = f"{identity}@L{function['line']}"
        return
    function["symbol_id"] = identity


def _annotate_function_ids(path: Path, result: dict, root: Path) -> None:
    containers = [*result["classes"], *result["functions"]]
    relative = path.resolve().relative_to(root).as_posix()
    annotated = [
        (function, _annotated_identity(function, containers, relative))
        for function in result["functions"]
    ]
    counts = Counter(identity for _function, identity in annotated)
    for function, identity in annotated:
        _assign_symbol_id(function, identity, counts[identity] > 1)


def _python_qualified_name(path: Path, function: dict, root: Path) -> str:
    relative = path.resolve().relative_to(root).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    module = ".".join(parts)
    owner = "" if function["owner"] == "<module>" else f".{function['owner']}"
    return f"{module}{owner}.{function['name']}"


def _symbol_span(symbol: dict) -> tuple[int, int]:
    return (
        symbol.get("end_line", symbol["line"]) - symbol["line"],
        symbol.get("end_column", 0) - symbol.get("column", 0),
    )


def _symbol_contains(container: dict, child: dict) -> bool:
    start = (container["line"], container.get("column", 0))
    end = (container.get("end_line", container["line"]), container.get("end_column", 10**9))
    child_start = (child["line"], child.get("column", 0))
    child_end = (child.get("end_line", child["line"]), child.get("end_column", 10**9))
    return start <= child_start and child_end <= end and (start, end) != (child_start, child_end)


def _containing_function(functions: list[dict], call: dict) -> dict | None:
    matches = [function for function in functions if _symbol_contains(function, call)]
    return min(matches, key=_symbol_span) if matches else None
