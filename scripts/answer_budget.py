"""Token-budgeted shaping for code-intelligence answers.

CODE-06, roadmap 2026-08-18 section 12. The CODE-07 paired run measured this
product spending 3.4x codebase-memory-mcp's tokens for the same fact, with
20.6% of its answer bytes spent on opaque `code:node:<32 hex>` identifiers that
no caller reads. This module is the reduction path.

Three properties are load-bearing.

1. **Opacity is decided by the value, not the key.** The `query` mode calls the
   hash `node_id`; the `callers` mode calls the same hash `symbol_id`. One rule
   keyed on the `code:<kind>:<32 hex>` form that `code_extractor._identifier`
   mints catches both, and deliberately spares the *readable* `symbol_id`
   (`scripts.module::function`) that the live dead-code fallback emits.

2. **The ladder drops the least informative field first** - the hash, then a
   field derivable from one that stays (`owner`, recoverable from `path`), then
   rows from the tail. `PROTECTED_FIELDS` never goes: the `file:line` citation
   is what the answer exists to deliver, and the graph-honesty fields are what
   the envelope's quality scoring reads.

3. **It fails closed.** MCP defines no field for "this was truncated" - checked
   against the 2025-03-26 specification, which carries no size limit and no
   truncation signal - so the answer body says it or nothing does. Any answer
   that lost rows carries `truncated: true` and the count; a budget too small
   for the frame is a named refusal, never a quietly shortened answer.

`refused_expansions[].refused_node_ids` keeps its hashes on purpose: it is a
list value rather than a scalar field, so the rule spares it, and it is the
evidence for a refusal that actually happened.

Research: `docs/research/2026-08-28-token-budgeted-answers.md`.
"""

from __future__ import annotations

import json
import re

# The client ceiling Anthropic documents for tool responses; a budget above it
# would be a number with nothing behind it.
MAX_BUDGET_TOKENS = 25_000
# Below the cost of any real answer frame, so the refusal path is reachable.
MIN_BUDGET_TOKENS = 32

# `code:<kind>:<32 hex>` - the form scripts/code_extractor.py:226 mints.
_OPAQUE_IDENTIFIER = re.compile(r"\Acode:[a-z]+:[0-9a-f]{32}\Z")

# Derivable from a field that stays; dropped only under budget pressure.
DERIVABLE_FIELDS = frozenset({"owner"})

# The citation the answer exists to deliver, plus the fields the operation
# envelope's quality and component scoring reads. Never dropped at any step.
PROTECTED_FIELDS = frozenset(
    {
        "path",
        "file",
        "line",
        "name",
        "function",
        "qualified_name",
        "symbol",
        "status",
        "error",
        "mode",
        "directory",
        "source_generation",
        "graph_complete",
        "unresolved_count",
        "fallback",
        "frontier_truncated",
        "refused_expansions",
        # Bare hashes, and kept on purpose: they appear only when an expansion
        # was actually refused, and they are the evidence for that refusal.
        # Named here rather than left to fall through the value rule, because
        # a fail-closed guarantee should not rest on an accident of shape.
        "refused_node_ids",
    }
)

_MAX_DEPTH = 6

# The budget block has to fit inside the budget too, or the answer would
# overrun the number it just claimed to honour. The block is held back from
# the body's budget rather than measured after the fact, because measuring it
# after the fact is circular - its own size changes when the size it reports
# changes. Worst observed block is ~150 characters; the allowance is generous
# on purpose and `tests/test_answer_budget.py` holds it to the promise.
REPORT_TOKEN_ALLOWANCE = 48

_TOO_SMALL_NOTE = (
    "the reduced answer still exceeds the budget; nothing was returned rather "
    "than a silently shortened answer"
)


def estimate_tokens(data) -> int:
    """`len // 4`, the same approximation `benchmark/run_code_parity.py` uses.

    Not a tokenizer. A real count needs a network round trip and an API key,
    which do not belong on a local, offline answer path.
    """
    return len(_serialized(data)) // 4


def _serialized(data) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def shape_code_answer(
    data,
    *,
    budget_tokens: int | None = None,
    include_node_ids: bool = False,
):
    """Shape one code answer: drop the opaque ids, then fit the budget."""
    if not isinstance(data, dict):
        return data
    answer, omitted = _without_opaque_identifiers(data, include_node_ids)
    if budget_tokens is None:
        return _annotated(answer, _default_report(omitted))
    return _fitted(answer, omitted, budget_tokens)


def _default_report(omitted: list[str]) -> dict:
    if not omitted:
        return {}
    return {"omitted_fields": omitted}


def _annotated(answer: dict, report: dict) -> dict:
    if not report:
        return answer
    return {**answer, "answer_budget": report}


def _without_opaque_identifiers(
    data: dict, include_node_ids: bool
) -> tuple[dict, list[str]]:
    if include_node_ids:
        return data, []
    omitted: set[str] = set()
    return _pruned(data, _is_opaque_identifier, omitted, 0), sorted(omitted)


def _is_opaque_identifier(key: str, value) -> bool:
    if key in PROTECTED_FIELDS:
        return False
    return _is_opaque_text(value) or _is_opaque_collection(value)


def _is_opaque_text(value) -> bool:
    return isinstance(value, str) and bool(_OPAQUE_IDENTIFIER.match(value))


def _is_opaque_collection(value) -> bool:
    """A structure holding nothing but hashes carries nothing but hashes.

    Measured 2026-08-28: `mode=summary` on this repository answers with 4 078
    communities, each a bare list of `code:node:` strings - 199 770 of the
    answer's 208 786 tokens, naming no symbol, no file and no line. A rule that
    only looked at scalar fields would have left the largest opaque payload in
    the product untouched.
    """
    if not isinstance(value, list) or not value:
        return False
    return all(_is_opaque_text(item) or _is_opaque_collection(item) for item in value)


def _is_derivable(key: str, value) -> bool:
    del value
    return key in DERIVABLE_FIELDS


def _pruned(value, drop, omitted: set, depth: int):
    if depth > _MAX_DEPTH:
        return value
    return _pruned_container(value, drop, omitted, depth)


def _pruned_container(value, drop, omitted: set, depth: int):
    if isinstance(value, dict):
        return _pruned_dict(value, drop, omitted, depth)
    if isinstance(value, list):
        return [_pruned(item, drop, omitted, depth + 1) for item in value]
    return value


def _pruned_dict(mapping: dict, drop, omitted: set, depth: int) -> dict:
    kept: dict = {}
    for key, value in mapping.items():
        _keep_entry(kept, key, value, drop, omitted, depth)
    return kept


def _keep_entry(kept: dict, key, value, drop, omitted: set, depth: int) -> None:
    if drop(key, value):
        omitted.add(key)
        return
    kept[key] = _pruned(value, drop, omitted, depth + 1)


def _bounded_budget(budget_tokens) -> int:
    if not isinstance(budget_tokens, int) or isinstance(budget_tokens, bool):
        raise ValueError("budget_tokens must be an integer")
    if not MIN_BUDGET_TOKENS <= budget_tokens <= MAX_BUDGET_TOKENS:
        raise ValueError(
            f"budget_tokens must be between {MIN_BUDGET_TOKENS} "
            f"and {MAX_BUDGET_TOKENS}"
        )
    return budget_tokens


def _fitted(data: dict, omitted: list[str], budget_tokens) -> dict:
    budget = _bounded_budget(budget_tokens)
    body_budget = budget - REPORT_TOKEN_ALLOWANCE
    state = {"answer": data, "omitted": list(omitted), "rows_omitted": 0}
    _apply_reductions(state, body_budget)
    body = estimate_tokens(state["answer"])
    if body > body_budget:
        return _too_small(budget, body + REPORT_TOKEN_ALLOWANCE, state)
    return _annotated(state["answer"], _report(state, budget, body))


def _apply_reductions(state: dict, budget: int) -> None:
    """Least informative first: derivable fields, then rows from the tail."""
    for step in (_drop_derivable_fields, _trim_rows):
        if estimate_tokens(state["answer"]) <= budget:
            return
        step(state, budget)


def _drop_derivable_fields(state: dict, budget: int) -> None:
    del budget
    omitted: set[str] = set()
    state["answer"] = _pruned(state["answer"], _is_derivable, omitted, 0)
    state["omitted"].extend(sorted(omitted))


def _trim_rows(state: dict, budget: int) -> None:
    for rows in _row_lists_by_size(state["answer"]):
        _trim_until_fits(state, rows, budget)


def _trim_until_fits(state: dict, rows: list, budget: int) -> None:
    while rows and estimate_tokens(state["answer"]) > budget:
        _drop_tail_rows(state, rows, budget)


def _drop_tail_rows(state: dict, rows: list, budget: int) -> None:
    overflow = estimate_tokens(state["answer"]) - budget
    count = min(len(rows), max(1, overflow // _row_tokens(rows)))
    del rows[len(rows) - count :]
    state["rows_omitted"] += count


def _row_tokens(rows: list) -> int:
    """Mean token cost of one row, floored at 1 so the divisor is safe."""
    return max(1, estimate_tokens(rows) // len(rows))


def _row_lists_by_size(answer: dict) -> list[list]:
    found: list[list] = []
    _collect_row_lists(answer, found, 0)
    return sorted(found, key=len, reverse=True)


def _collect_row_lists(value, found: list, depth: int) -> None:
    if depth > _MAX_DEPTH:
        return
    _collect_from_container(value, found, depth)


def _collect_from_container(value, found: list, depth: int) -> None:
    if isinstance(value, dict):
        _collect_from_items(value.values(), found, depth)
        return
    if isinstance(value, list):
        _collect_from_list(value, found, depth)


def _collect_from_list(rows: list, found: list, depth: int) -> None:
    if _is_row_list(rows):
        found.append(rows)
    _collect_from_items(rows, found, depth)


def _collect_from_items(items, found: list, depth: int) -> None:
    for item in items:
        _collect_row_lists(item, found, depth + 1)


def _is_row_list(rows: list) -> bool:
    return bool(rows) and all(isinstance(row, dict) for row in rows)


def _report(state: dict, budget: int, body: int) -> dict:
    """`body_tokens` is the answer without this block; see the allowance."""
    report = {"budget_tokens": budget, "body_tokens": body}
    _record_omitted_fields(report, state)
    _record_omitted_rows(report, state)
    return report


def _record_omitted_fields(report: dict, state: dict) -> None:
    if not state["omitted"]:
        return
    report["omitted_fields"] = sorted(set(state["omitted"]))


def _record_omitted_rows(report: dict, state: dict) -> None:
    if not state["rows_omitted"]:
        return
    report["rows_omitted"] = state["rows_omitted"]
    report["truncated"] = True


def _too_small(budget: int, tokens: int, state: dict) -> dict:
    """A budget the frame cannot fit is a named refusal, not a short answer."""
    return {
        "status": "error",
        "error": "answer_budget_too_small",
        "answer_budget": {
            "budget_tokens": budget,
            "minimum_tokens": tokens,
            "rows_omitted": state["rows_omitted"],
            "truncated": False,
            "note": _TOO_SMALL_NOTE,
        },
    }
