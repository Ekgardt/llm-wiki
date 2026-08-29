"""Why `extract_code` batches the whole universe, pinned so it stays true.

`doctor._SourceExtractionAdapter` extracts every code source in the snapshot as
soon as one is rebuilt. That is deliberate, and it was measured rather than
assumed: `docs/research/2026-08-29-what-a-partial-extraction-can-get-wrong.md`.

The decision rests on two structural facts about `code_extractor._Collector`,
and the whole point of this file is that they are facts rather than folklore.
Gradle turns compile avoidance off when annotation processors are on the
classpath "because for annotation processors the implementation details
matter"; Zinc exempts inheritance from name hashing for the same reason. Both
say: an incremental narrowing is only as sound as the claim that the analysis
reads nothing beyond the interface. Here that claim is checkable, so it is
checked.

If one of these tests fails, the split described in the note becomes unsafe,
not merely slower. Bring a new measurement before changing them.
"""

from __future__ import annotations

import ast
import hashlib
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from code_extractor import extract_code  # noqa: E402
from corpus_snapshot import CapturedSource, SourceMetadata, SourceRecord  # noqa: E402

EXTRACTOR_SOURCE = SCRIPTS / "code_extractor.py"

# The cross-source channels a dependent can read. `_resolve_expression`,
# `_candidate_modules` and `_matching_modules` consult these and nothing else,
# and `source_dependencies` is computed from `node_sources`.
SHARED_INDEXES = frozenset(
    {
        "definitions",
        "python_scopes",
        "modules",
        "module_name_index",
        "source_modules",
        "files",
        "tables",
        "syntax_definitions",
        "syntax_functions",
        "node_sources",
        "function_body_scope",
        "function_parent_scope",
        "scope_parent",
        "route_receivers",
        "sqlite_modules",
        "python_entry_names",
    }
)

# The edges pass. Everything else `extract` calls is the definitions pass.
EDGE_METHODS = ("collect_python_edges", "collect_syntax_edges")

MUTATORS = frozenset(
    {"setdefault", "append", "add", "update", "extend", "pop", "clear"}
)


def _is_self(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id == "self"


def _self_attribute(node: ast.AST) -> str | None:
    """`self.<name>` as a name, or None for anything else."""
    if not isinstance(node, ast.Attribute):
        return None
    return node.attr if _is_self(node.value) else None


def _stored_attribute(node: ast.AST) -> str | None:
    """`self.<name> = ...`"""
    if not isinstance(node, ast.Attribute) or not isinstance(node.ctx, ast.Store):
        return None
    return _self_attribute(node)


def _stored_subscript(node: ast.AST) -> str | None:
    """`self.<name>[key] = ...`"""
    if not isinstance(node, ast.Subscript) or not isinstance(node.ctx, ast.Store):
        return None
    return _self_attribute(node.value)


def _mutating_call(node: ast.AST) -> str | None:
    """`self.<name>.setdefault(...)` and its family."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return None
    return _self_attribute(node.func.value) if node.func.attr in MUTATORS else None


def _written_name(node: ast.AST) -> str | None:
    for probe in (_stored_attribute, _stored_subscript, _mutating_call):
        found = probe(node)
        if found is not None:
            return found
    return None


def _written_self_attributes(tree: ast.AST) -> set[str]:
    written = set()
    for node in ast.walk(tree):
        name = _written_name(node)
        if name is not None:
            written.add(name)
    return written


def _called_method_names(tree: ast.AST) -> set[str]:
    called = set()
    for node in ast.walk(tree):
        name = _mutating_or_plain_call(node)
        if name is not None:
            called.add(name)
    return called


def _mutating_or_plain_call(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return None
    return node.func.attr if _is_self(node.func.value) else None


def _method(name: str) -> ast.FunctionDef:
    tree = ast.parse(EXTRACTOR_SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"code_extractor no longer defines {name!r}")


def test_the_edges_pass_writes_no_shared_resolution_index() -> None:
    """The edges pass may read the universe. It may never extend it.

    This is what makes the pass per-source: a source analysed alone sees the
    same indexes it would have seen in a full run, because no other source's
    edges pass could have changed them.
    """
    for method_name in EDGE_METHODS:
        written = _written_self_attributes(_method(method_name))
        offenders = sorted(written & SHARED_INDEXES)
        assert not offenders, (
            f"{method_name} now writes {offenders} -- a shared resolution index. "
            "Partial extraction would stop matching a full build; see "
            "docs/research/2026-08-29-what-a-partial-extraction-can-get-wrong.md"
        )


def test_the_edges_pass_records_no_occurrence() -> None:
    """`node_sources` is complete before the edges pass and frozen during it.

    `add_occurrence` is the only writer of `node_sources`, and
    `source_dependencies` -- the edge the dependency closure walks -- is
    computed from it. An occurrence recorded during the edges pass would make a
    source's dependencies depend on which other sources were analysed.
    """
    for method_name in EDGE_METHODS:
        called = _called_method_names(_method(method_name))
        assert "add_occurrence" not in called, (
            f"{method_name} now records an occurrence, so node_sources is no "
            "longer settled before the edges pass runs"
        )


def _source(path: str, content: bytes) -> CapturedSource:
    return CapturedSource(
        SourceRecord(
            logical_id=f"source:{path}",
            relative_path=path,
            sha256=hashlib.sha256(content).hexdigest(),
            size=len(content),
            media_type="text/x-python",
            language="python",
            git_oid=None,
        ),
        SourceMetadata(type="code", language="python"),
        content,
    )


def _nodes_with_an_occurrence(result) -> set[str]:
    return {str(item["node_id"]) for item in result.occurrences}


def _module_node_of(result, path: str):
    for node in result.nodes:
        if node["kind"] == "module" and node["metadata"].get("path") == path:
            return node
    raise AssertionError(f"no module node was built for {path!r}")


def test_an_empty_source_leaves_a_node_no_occurrence_can_own() -> None:
    """The one construction a partial batch cannot attribute on its own.

    `add_occurrence` and `add_assertion` both return early on an empty span, so
    a zero-byte source contributes a module node carrying neither. A full run
    hands that node to whichever source imports it; a partial run that does not
    analyse the importer has no way to know anyone does, and
    `_partition_nodes` sends it to `min(source_ids)` instead. Measured
    divergence, not a hypothesis -- see the note.
    """
    result = extract_code(
        (_source("pkg/__init__.py", b""), _source("pkg/live.py", b"import pkg\n")),
        repository_id="universe-test",
    )
    empty_module = _module_node_of(result, "pkg/__init__.py")
    owned = _nodes_with_an_occurrence(result)
    assert str(empty_module["node_id"]) not in owned
