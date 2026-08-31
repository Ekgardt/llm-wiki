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

# What a caller who named no budget gets. The ceiling itself, because it is the
# only value at which the cut falls entirely outside what the answer claims.
#
# Measured 2026-08-29, `find_dead_code` on this repository: 873 candidates,
# 50 673 estimated tokens once the opaque ids are gone - 2x the client ceiling,
# so the host was cutting it with no signal, which is exactly what this module
# exists to prevent. At 25 000 the ladder drops `owner` before any row and all
# every row the tool actually asserts survives; at 12 000, 216 of them did not.
# The count behind that measurement has since changed and the reason has not:
# `zero_confirmed_incoming_calls` was 461 when this was written and is 26
# today, because a name loaded as a value turned out not to be dead at all
# (see `docs/research/2026-08-29-a-name-loaded-is-a-name-used.md`). A default
# below the ceiling deletes the part of the answer the tool asserts, so thrift
# is left to the caller's explicit `budget_tokens`.
#
# Research: `docs/research/2026-08-29-a-default-budget-for-a-dead-code-answer.md`.
DEFAULT_BUDGET_TOKENS = MAX_BUDGET_TOKENS

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
    """Shape one code answer: drop the opaque ids, compact, then fit the budget."""
    if not isinstance(data, dict):
        return data
    answer, omitted = _without_opaque_identifiers(data, include_node_ids)
    answer = _with_row_constants(answer, 0)
    if budget_tokens is None:
        return _fitted_to_default(answer, omitted)
    return _fitted(answer, omitted, budget_tokens)


def _fitted_to_default(answer: dict, omitted: list[str]) -> dict:
    """No budget named still means an answer the client can carry whole.

    An answer already under the default is returned as it was, with no budget
    block: a caller who asked for no accounting gets none, and the contract
    that a small answer comes back unchanged survives. Only an answer that
    would have been cut by the host is cut here instead, where the cut can say
    so.
    """
    if estimate_tokens(answer) <= DEFAULT_BUDGET_TOKENS - REPORT_TOKEN_ALLOWANCE:
        return _annotated(answer, _default_report(omitted))
    return _fitted(answer, omitted, DEFAULT_BUDGET_TOKENS)


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
    pruned = _pruned(data, _is_opaque_identifier, omitted, 0)
    return _without_recoverable_identities(pruned, omitted, 0), sorted(omitted)


# The second mint. `code_extractor._identifier` makes `code:<kind>:<32 hex>`,
# which the value rule above already catches; the stored generation makes
# `repository:<64 hex>\x1f<language>\x1f<name>\x1f<path>` for a module, and that
# one slipped through because it ends in readable text.
#
# Measured 2026-08-29, `mode=dependencies` on this repository: 326 rows,
# 23 849 tokens, of which `identity_key` was 14 564 - 61.1% - and the constant
# `repository:<64 hex>` prefix alone was 6 112, repeated on all 326 rows. The
# same rows carry `metadata.name` and `metadata.path`, so the key restates in
# an internal encoding what the row already says in the open.
#
# Dropped only when that is demonstrably true of the row in hand: every
# readable segment of the key must already appear as a value in the same row's
# `metadata`. A key holding anything the row does not otherwise say is left
# alone, so the rule can never be the reason a fact left the answer.
_REPOSITORY_IDENTITY = re.compile(r"\Arepository:[0-9a-f]{64}\x1f")


def _readable_identity_segments(key: str) -> list[str]:
    """Everything after the repository digest and the language tag."""
    return key.split("\x1f")[2:]


def _is_repository_identity(key) -> bool:
    return isinstance(key, str) and bool(_REPOSITORY_IDENTITY.match(key))


def _stated_values(metadata) -> set[str]:
    if not isinstance(metadata, dict):
        return set()
    return {str(value) for value in metadata.values()}


def _identity_is_recoverable(row: dict) -> bool:
    key = row.get("identity_key")
    if not _is_repository_identity(key):
        return False
    stated = _stated_values(row.get("metadata"))
    return all(segment in stated for segment in _readable_identity_segments(key))


def _row_without_identity(row: dict, omitted: set, depth: int) -> dict:
    descended = {
        key: _without_recoverable_identities(value, omitted, depth + 1)
        for key, value in row.items()
    }
    if not _identity_is_recoverable(descended):
        return descended
    omitted.add("identity_key")
    return {key: value for key, value in descended.items() if key != "identity_key"}


def _recoverable_identity_container(value, omitted: set, depth: int):
    if isinstance(value, dict):
        return _row_without_identity(value, omitted, depth)
    if isinstance(value, list):
        return [
            _without_recoverable_identities(item, omitted, depth + 1) for item in value
        ]
    return value


def _without_recoverable_identities(value, omitted: set, depth: int):
    if depth > _MAX_DEPTH:
        return value
    return _recoverable_identity_container(value, omitted, depth)


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


# A value identical on every row of a table is a fact about the table, not
# about a row. Stating it once is the columnar move TOON and every CSV-shaped
# encoding make, and it is lossless: the value stays in the answer, under
# `<key>_row_constants`, exactly once.
#
# Measured 2026-08-29 on this repository, before this rule:
#   `find_dead_code`  532 rows - `status` was the constant "candidate" at a
#                     cost of 3 059 tokens, and `graph_complete` the constant
#                     `false` at 3 325, the second of which the answer already
#                     states at top level. 6 384 of 24 779 candidate tokens
#                     (25.8%) said nothing a reader could not read once.
#   `mode=summary`    97 entry points - `kind` and `name` were both the
#                     constant "main": 776 of 2 617 tokens (29.7%).
#
# Applied only where it pays, decided by measuring both shapes rather than by a
# threshold on row count: a short table whose constants block costs more than
# it saves is left exactly as it was, so small answers do not move at all.
#
# Research: `docs/research/2026-08-29-what-the-caller-asked.md`.


def _is_constant_table(value) -> bool:
    """A list of at least two dicts - the only shape a constant column can have.

    Deliberately not named `_is_row_list`: that name is already taken further
    down this module by the trimming path's own predicate, which takes a list
    that is known to be a list and answers a different question. Defining it
    twice made the later definition win silently and broke every code answer.
    """
    return (
        isinstance(value, list)
        and len(value) > 1
        and all(isinstance(item, dict) for item in value)
    )


def _is_scalar(value) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _same_value(candidate, value) -> bool:
    """Type-strict, so a column mixing `False` and `0` is not called constant."""
    return type(candidate) is type(value) and candidate == value


def _constant_across(rows: list, key: str, value) -> bool:
    return all(key in row and _same_value(row[key], value) for row in rows)


def _row_constant_keys(rows: list) -> list[str]:
    """Keys every row carries with one identical scalar value."""
    return sorted(
        key
        for key, value in rows[0].items()
        if _is_scalar(value) and _constant_across(rows, key, value)
    )


def _hoisted_rows(rows: list, keys) -> list[dict]:
    dropped = set(keys)
    return [
        {key: value for key, value in row.items() if key not in dropped}
        for row in rows
    ]


def _constants_if_cheaper(key: str, rows: list, keys: list[str]) -> dict:
    constants = {name: rows[0][name] for name in keys}
    compacted = {
        key: _hoisted_rows(rows, keys),
        f"{key}_row_constants": constants,
    }
    if estimate_tokens(compacted) >= estimate_tokens({key: rows}):
        return {}
    return constants


def _payable_row_constants(key: str, value) -> dict:
    if not _is_constant_table(value):
        return {}
    keys = _row_constant_keys(value)
    if not keys:
        return {}
    return _constants_if_cheaper(key, value, keys)


def _compacted_entry(key, value, depth: int) -> dict:
    """One key's contribution: its value, plus row constants when they pay."""
    descended = _with_row_constants(value, depth + 1)
    constants = _payable_row_constants(key, descended)
    if not constants:
        return {key: descended}
    return {
        key: _hoisted_rows(descended, sorted(constants)),
        f"{key}_row_constants": constants,
    }


def _row_constant_dict(mapping: dict, depth: int) -> dict:
    compacted: dict = {}
    for key, value in mapping.items():
        compacted.update(_compacted_entry(key, value, depth))
    return compacted


def _row_constant_container(value, depth: int):
    if isinstance(value, dict):
        return _row_constant_dict(value, depth)
    if isinstance(value, list):
        return [_with_row_constants(item, depth + 1) for item in value]
    return value


def _with_row_constants(value, depth: int):
    if depth > _MAX_DEPTH:
        return value
    return _row_constant_container(value, depth)


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
