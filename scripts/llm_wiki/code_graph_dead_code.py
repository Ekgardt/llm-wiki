"""Dead-code candidates and the reasons each one is or is not reported."""
from __future__ import annotations

import ast
import re
import sqlite3
from pathlib import (
    Path,
    PurePath,
)

try:
    from .. import value_references
except ImportError:
    import value_references
from . import code_graph_generation, code_graph_workspace
from .code_graph_generation import (
    NO_GENERATION,
    stored_location,
    stored_or_reason,
)
from .code_graph_reports import (
    live_report,
    reported_answer,
    store_report,
)

try:
    from ..python_parse import (
        PARSE_FAILURES,
        parse_python,
    )
except ImportError:
    from python_parse import (
        PARSE_FAILURES,
        parse_python,
    )

# Names `code_graph` treats as conventionally reached, pushed into the store's
# anti-join as caller data. Measured: it takes this repository's dead-code
# candidate set from 8,546 of 10,000 rows (17% headroom) to 3,621 (2.8x).
DEAD_CODE_NAME_PREFIXES = ("test_",)


def _reachable_or_conventional(function, name, path, incoming, exports) -> bool:
    return (
        function["symbol_id"] in incoming
        or name in {"main", "__init__"}
        or name.startswith("test_")
        or path.name.startswith("test_")
        or name in exports
    )


def _live_dead_candidate(
    function: dict, path: Path, incoming, exports, lines
) -> dict | None:
    name = function["name"]
    if _reachable_or_conventional(function, name, path, incoming, exports):
        return None
    if _is_framework_route(lines, function["line"]):
        return None
    return {
        "name": name,
        "symbol_id": function["symbol_id"],
        "owner": function["owner"],
        "file": str(path),
        "line": function["line"],
        "status": "candidate",
        "reason": _checked_reason("zero_confirmed_incoming_calls", str(path)),
        "graph_complete": False,
    }


def _live_dead_candidates_in_file(path: Path, result: dict, incoming) -> list[dict]:
    source = path.read_text(encoding="utf-8", errors="ignore")
    exports = _declared_exports(path, source, result["language"])
    lines = source.splitlines()
    found = [
        _live_dead_candidate(function, path, incoming, exports, lines)
        for function in result["functions"]
    ]
    return [item for item in found if item is not None]


def _named_dead_candidates(candidates: list[dict], symbol: str | None) -> list[dict]:
    """Only the candidates the named symbol could be, as the stored path selects."""
    if not symbol:
        return candidates
    wanted = str(symbol).rsplit(".", 1)[-1]
    return [item for item in candidates if item.get("name") == wanted]


def _stored_dead_code_result(directory: Path, with_report: bool, symbol=None):
    if with_report:
        return store_find_dead_code(directory, with_report=True, symbol=symbol)
    return store_find_dead_code(directory, symbol=symbol)


def find_dead_code(
    directory: Path,
    *,
    live: bool = False,
    with_report: bool = False,
    symbol: str | None = None,
) -> list[dict] | dict:
    """Return conservative dead-code candidates from the incomplete static graph.

    `symbol` narrows the question to one name. On the stored path it narrows the
    reference scan too: asking about one function used to parse every Python
    source in the repository (11 s, measured 2026-09-12). The verdict for that
    name is unchanged — a file that does not mention the name cannot reference it
    — and the report says how many sources were skipped. The live path must parse
    the workspace to see its calls; it answers only for the name (audit C-44,
    docs/research/2026-09-25-navigation-dead-code-and-stale-words.md).
    """
    stored, reason = None, NO_GENERATION
    if not live:
        stored, reason = stored_or_reason(
            lambda: _stored_dead_code_result(directory, with_report, symbol)
        )
    if stored is not None:
        return stored
    parsed, definitions, edges = code_graph_workspace.workspace_call_graph(directory)
    incoming = {edge["target"] for edge in edges}
    candidates: list[dict] = []
    for path, result in parsed:
        candidates.extend(_live_dead_candidates_in_file(path, result, incoming))
    candidates = ordered_dead_candidates(_named_dead_candidates(candidates, symbol))
    report = {
        **live_report(directory, parsed, reason),
        **dead_code_counts(candidates),
    }
    return reported_answer("candidates", candidates, report, with_report)


# The order a cut obeys. `answer_budget` drops rows from the tail, so this is
# the rule that decides what a reader loses when the answer does not fit.
#
# Measured 2026-08-29 on this repository at the 25 000-token default: sorted by
# name, the cut threw away 97 `zero_confirmed_incoming_calls` rows and kept 162
# `unresolved_receiver` ones; sorted by reason first, it dropped 354 rows and
# not one defensible candidate. `unresolved_receiver` states doubt - a call site
# names the symbol and the receiver did not resolve - and doubt is what a cut
# should reach first. This orders, it never filters: a large enough budget still
# returns every row, and `candidates_by_reason` states the totals either way.
#
# The order is by how strong a claim of death the verdict makes, so the cut
# always reaches the weakest first. `referenced_without_call` sits between the
# other two: nothing calls the name, which `unresolved_receiver` cannot say, but
# something names it, which `zero_confirmed_incoming_calls` denies. Appending it
# after `unresolved_receiver` instead cost a graded case - the CODE-07 stand's
# `T07` lost `_search_backends` to the budget cut on 2026-08-29 while keeping
# rows that state weaker evidence.
DEAD_CODE_REASON_ORDER = (
    "zero_confirmed_incoming_calls",
    "referenced_without_call",
    "unresolved_receiver",
    "references_not_indexed",
)
# The value-reference index reads Python only. For a TypeScript, JavaScript, Go
# or Rust symbol "nothing names it" was never checked, so claiming it was put 8
# live rows of 11 under the strongest verdict (audit 2026-09-26 A-7,
# docs/research/2026-09-26-a-dead-code-claim-names-what-it-checked.md).
_REFERENCE_INDEXED_SUFFIXES = frozenset({".py", ".pyi"})


def _references_indexed(path: str) -> bool:
    return PurePath(path).suffix in _REFERENCE_INDEXED_SUFFIXES


def _checked_reason(reason: str, path: str) -> str:
    """A claim that nothing names the symbol stands only where names were read."""
    if reason == "unresolved_receiver" or _references_indexed(path):
        return reason
    return "references_not_indexed"


def _dead_code_reason_rank(reason: str) -> int:
    """An unknown reason sorts last, so a new one is never cut before a known."""
    if reason not in DEAD_CODE_REASON_ORDER:
        return len(DEAD_CODE_REASON_ORDER)
    return DEAD_CODE_REASON_ORDER.index(reason)


def _dead_code_order_key(candidate: dict) -> tuple:
    """Defensible candidates first, then the vault's usual name/file/line."""
    rank = _dead_code_reason_rank(str(candidate.get("reason", "")))
    return (rank, candidate["name"], candidate["file"], candidate["line"])


def ordered_dead_candidates(candidates: list[dict]) -> list[dict]:
    return sorted(candidates, key=_dead_code_order_key)


def dead_code_counts(candidates: list[dict]) -> dict[str, object]:
    """Totals that survive a cut, because the report block is never trimmed.

    A budget may drop rows; it must not leave the reader with a wrong picture of
    how many candidates there are or how defensible they are.
    """
    counts: dict[str, int] = {}
    for candidate in candidates:
        reason = str(candidate.get("reason", "unknown"))
        counts[reason] = counts.get(reason, 0) + 1
    return {
        "candidate_count": len(candidates),
        "candidates_by_reason": dict(sorted(counts.items())),
    }


def _conventionally_reachable(name: str, path: str) -> bool:
    """Names this project treats as reached even with no confirmed caller.

    The `test_` name prefix is also pushed into the store's anti-join; the
    basename rule stays here, where `PurePath(path).name` means exactly what it
    says and the nearest SQL spelling would mean something wider.
    """
    return (
        name in {"main", "__init__"}
        or name.startswith("test_")
        or PurePath(path).name.startswith("test_")
    )


def protocol_invoked(name: str) -> bool:
    """A dunder the language calls itself, never by name at a call site.

    Measured 2026-08-28: 69 of 868 candidates were these — `__post_init__` 47,
    `__reduce__` 8, `__enter__` and `__exit__` 5 each, `__call__` and `__str__`
    2 each. Name-based analysis cannot see their callers by construction, so
    "no confirmed caller" says nothing about them at all. `__init__` is already
    excluded by `_conventionally_reachable`.
    """
    return len(name) > 4 and name.startswith("__") and name.endswith("__")


def _unnamed_reason(name: str, index) -> str:
    """The verdict for a name no call site writes.

    NEW-135 stopped here and called every such name dead. It is not: a symbol
    named without being called — a thread target, a registry entry,
    `set_defaults(handler=…)`, a name spelled in a `getattr` table — is never
    written at a call site, so it looked uncalled while being called every
    run. Measured 2026-08-29 against the active generation: 402 of the 461
    names this verdict called dead are named somewhere in the same corpus, so
    87% of the strongest verdict was false. It refuted itself inside one run —
    `architecture_dependencies` sat in `_ARCHITECTURE_MODE_QUERIES` while
    being listed dead.
    """
    if index.names_a_value(name):
        return "referenced_without_call"
    return "zero_confirmed_incoming_calls"


def _dead_code_reason(name: str, called_names: frozenset[str] | None, index) -> str:
    """`zero_confirmed_incoming_calls` only when nothing names it anywhere.

    Measured 2026-08-28 on this repository: of 868 candidates, 384 were named
    by some call text — a dynamic call may reach them — so calling them all
    dead made 52% of the answer indefensible once the protocol methods are
    counted too. `None` on either reader means that set could not be read, and
    then nothing can be claimed dead: doubt is reported instead.
    """
    if called_names is None or index is None:
        return "unresolved_receiver"
    if name in called_names:
        return "unresolved_receiver"
    return _unnamed_reason(name, index)


def _dispatched_definition(index, path: str, name: str, line: int) -> bool:
    """True when the definition itself is handed to whatever will call it.

    The same argument `protocol_invoked` already makes for dunders: a
    decorated definition goes to its decorator at import time, and a method of
    a class that declares a base is reached through that base — `visit_Call`
    by `ast.NodeVisitor.visit`, `readinto` by `io.RawIOBase`. Name analysis
    cannot see either caller by construction, so "no confirmed caller" says
    nothing about them and they are not candidates at all.
    """
    if index is None:
        return False
    return index.is_dispatched(path, name, line)


def _exclusion_rule(name: str, path: str) -> str | None:
    """Why this symbol is not a candidate at all, or None when it is one."""
    if _conventionally_reachable(name, path):
        return "conventionally_reachable"
    if protocol_invoked(name):
        return "protocol_invoked"
    return None


def dead_candidate_row(
    node: dict, location: tuple[str, int], called_names, index
) -> dict:
    name = str(node["metadata"].get("name", ""))
    return {
        "name": name,
        "symbol_id": node["node_id"],
        "owner": node["metadata"].get("owner", ""),
        "file": location[0],
        "line": location[1],
        "status": "candidate",
        "reason": _checked_reason(_dead_code_reason(name, called_names, index), location[0]),
        "graph_complete": False,
    }


def _dead_code_verdict(
    graph, node: dict, directory: Path, called_names, index
) -> tuple[dict | None, str | None]:
    """One candidate, or the named rule that dropped it. Never both."""
    name = str(node["metadata"].get("name", ""))
    path = str(node["metadata"].get("path", ""))
    rule = _exclusion_rule(name, path)
    if rule is not None:
        return (None, rule)
    location = stored_location(graph, node["node_id"], directory)
    if _dispatched_definition(index, path, name, location[1]):
        return (None, "framework_dispatched")
    return (dead_candidate_row(node, location, called_names, index), None)


def _called_names(graph) -> frozenset[str] | None:
    """Every name any call site mentions, or None when the read refused."""
    try:
        return graph.call_target_names()
    except (ValueError, sqlite3.Error):
        return None


def _reference_index(graph, names=None):
    """What the corpus names as a value, or None when the read refused.

    A store that cannot serve sources has none, and for such a store "nothing
    names it" is true, so `EMPTY_INDEX` is the honest answer there rather than
    a refusal. A real read that fails returns None and every verdict falls
    back to doubt, exactly like a refused `call_target_names`.
    """
    reader = getattr(graph, "python_sources", None)
    if reader is None:
        return value_references.EMPTY_INDEX
    try:
        return value_references.build_reference_index(reader(), names=names)
    except (ValueError, sqlite3.Error):
        return None


def stored_dead_candidates(graph, nodes, directory) -> list[dict]:
    candidates, _counts = _classified_dead_nodes(graph, nodes, directory)
    return candidates


def _classified_dead_nodes(
    graph, nodes, directory, names=None
) -> tuple[list[dict], dict]:
    """Split the nodes into candidates and the reasons the rest were dropped.

    The dropped count is reported, never silent: this pass removes more than
    NEW-135 did, and a reader who is not told how many were removed cannot
    tell a smaller answer from a better one.
    """
    called_names = _called_names(graph)
    index = _reference_index(graph, names)
    verdicts = [
        _dead_code_verdict(graph, node, directory, called_names, index)
        for node in nodes
    ]
    return _split_verdicts(verdicts, index)


def _split_verdicts(verdicts: list[tuple], index) -> tuple[list[dict], dict]:
    candidates = [row for row, _rule in verdicts if row is not None]
    rules = [rule for _row, rule in verdicts if rule is not None]
    return candidates, _dropped_counts(rules, index)


def _rule_counts(rules: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for rule in rules:
        counts[rule] = counts.get(rule, 0) + 1
    return dict(sorted(counts.items()))


def _dropped_counts(rules: list[str], index) -> dict[str, object]:
    report = {"excluded_count": len(rules), "excluded_by_rule": _rule_counts(rules)}
    if index is None:
        return {**report, "reference_index": "unreadable"}
    return {**report, **index.as_report()}


# The most dead-code candidates one answer reads; past it the answer says it was cut.
MAX_DEAD_CODE_NODES = 10_000


def _stored_dead_nodes(graph, name: str | None = None) -> tuple[list[dict], bool]:
    """Function and method nodes no resolved CALLS reaches and no EXPOSES names.

    The anti-join runs in SQL, so this reads its own answer — measured 3,621
    rows in 0.17 s — instead of the 19,153 nodes and 35,313 edges the old shape
    materialised before refusing at the row ceiling. A named symbol narrows in
    SQL, and a graph past the bound returns its first rows and says so instead of
    refusing (audit 2026-09-26 B-7).
    """
    return graph.nodes_without_edges(
        kinds=("function", "method"),
        incoming_edge_types=("CALLS",),
        outgoing_edge_types=("EXPOSES",),
        exclude_name_prefixes=DEAD_CODE_NAME_PREFIXES,
        name=name,
        max_rows=MAX_DEAD_CODE_NODES,
    )


def _marked_complete(candidates: list[dict], report: dict) -> list[dict]:
    for candidate in candidates:
        candidate["graph_complete"] = report["graph_complete"]
    return ordered_dead_candidates(candidates)


def _scope_names(symbol: str | None) -> frozenset[str] | None:
    """The names a narrowed reference scan must read, or None for all of them."""
    if not symbol:
        return None
    return frozenset(part for part in str(symbol).split(".") if part)


def _dead_nodes_for(graph, symbol: str | None) -> tuple[list[dict], bool]:
    """Every candidate node, or only those the named symbol could be, and whether cut."""
    if not symbol:
        return _stored_dead_nodes(graph)
    return _stored_dead_nodes(graph, str(symbol).rsplit(".", 1)[-1])


def store_find_dead_code(
    directory: Path, *, with_report: bool = False, symbol: str | None = None
) -> list[dict] | dict | None:
    graph = code_graph_generation.active_evidence_graph(directory)
    if graph is None:
        return None
    try:
        nodes, truncated = _dead_nodes_for(graph, symbol)
        candidates, dropped = _classified_dead_nodes(
            graph, nodes, directory, _scope_names(symbol)
        )
        report = store_report(graph)
        ordered = _marked_complete(candidates, report)
        counts = {**dead_code_counts(ordered), **dropped, "candidates_truncated": truncated}
        return reported_answer("candidates", ordered, {**report, **counts}, with_report)
    finally:
        graph.close()


def _all_assignment(node) -> bool:
    return isinstance(node, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id == "__all__"
        for target in node.targets
    )


def _all_names(node) -> set[str]:
    try:
        value = ast.literal_eval(node.value)
    except (ValueError, TypeError):
        return set()
    return {item for item in value if isinstance(item, str)}


def _python_exports(source: str) -> set[str]:
    try:
        tree = parse_python(source)
    except PARSE_FAILURES:
        return set()
    for node in tree.body:
        if _all_assignment(node):
            return _all_names(node)
    return set()


def _script_exports(source: str) -> set[str]:
    return set(
        re.findall(
            r"\bexport\s+(?:default\s+)?(?:async\s+)?(?:function|class)\s+(\w+)",
            source,
        )
    )


def _declared_exports(path: Path, source: str, language: str) -> set[str]:
    if language == "python":
        return _python_exports(source)
    if language in {"javascript", "typescript"}:
        return _script_exports(source)
    return set()


def _is_framework_route(lines: list[str], definition_line: int) -> bool:
    prefix = "\n".join(lines[max(0, definition_line - 2):definition_line])
    return bool(re.search(r"(?:@\w*\.route\s*\(|@(?:GetMapping|RequestMapping)\b)", prefix))
