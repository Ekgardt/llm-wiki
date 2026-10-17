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
import os
import re
from functools import partial

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

# `code:<kind>:<32 hex>` - the form `code_extractor._identifier` mints.
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

# The budget block has to fit inside the budget too, or the answer would
# overrun the number it just claimed to honour. The block is held back from
# the body's budget rather than measured after the fact, because measuring it
# after the fact is circular - its own size changes when the size it reports
# changes. Worst observed block is ~150 characters; the allowance is generous
# on purpose and `tests/test_answer_budget.py` holds it to the promise. It keeps
# the 192 bytes it held under the earlier 4-bytes estimate (48 tokens then): at
# the measured 2 bytes per token 48 would no longer cover a 150-byte block.
REPORT_TOKEN_ALLOWANCE = 96

_TOO_SMALL_NOTE = (
    "the reduced answer still exceeds the budget; nothing was returned rather "
    "than a silently shortened answer"
)


# A token of this vault's Markdown costs 2.3-4.0 UTF-8 bytes on the provider it uses
# (median 2.6 English, 3.1 Russian), measured 2026-09-27 from provider-reported usage;
# tiktoken's "about 4 bytes" is an English average and undercounted these answers
# 1.3-1.7x. A budget is a ceiling, so the estimate takes the side that never
# undercounted a measured sample. Bytes, not characters: a Cyrillic character is two
# bytes. Rerun the measurement when the provider's tokenizer changes. See
# docs/research/2026-09-27-an-estimate-is-measured-and-an-open-day-waits.md.
BYTES_PER_TOKEN = 2


def estimate_text_tokens(text: str) -> int:
    """The one estimate of what a text costs a model: UTF-8 bytes / BYTES_PER_TOKEN, rounded up.

    Not a tokenizer. A real count needs a network round trip and an API key,
    which do not belong on a local, offline answer path.
    """
    return -(-len(text.encode("utf-8")) // BYTES_PER_TOKEN)


def estimate_tokens(data) -> int:
    """The estimate of an answer: `estimate_text_tokens` of its serialized form."""
    return estimate_text_tokens(_serialized(data))


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
    answer = _without_repeated_modules(answer)
    answer = _with_row_constants(answer)
    return _as_columnar(_shaped_to_budget(answer, omitted, budget_tokens))


def _shaped_to_budget(answer: dict, omitted: list[str], budget_tokens: int | None):
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
    pruned = _pruned(data, _is_opaque_identifier, omitted)
    return _without_recoverable_identities(pruned, omitted), sorted(omitted)


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



_TREE_EXIT = object()


def _unchanged(value):
    return value


def _children(value):
    if isinstance(value, dict):
        return list(value.items())
    return list(enumerate(value))


def _claim_tree_container(value, active: set) -> None:
    identity = id(value)
    if identity in active:
        raise ValueError("Circular reference in code answer")
    active.add(identity)


def _walk_tree_step(value, closing: bool, pending: list, active: set):
    if closing:
        active.remove(id(value))
        return _TREE_EXIT
    if not isinstance(value, (dict, list)):
        return value
    _claim_tree_container(value, active)
    pending.append((value, True))
    pending.extend((item, False) for _key, item in reversed(_children(value)))
    return value


def _tree_nodes(value):
    pending = [(value, False)]
    active: set = set()
    while pending:
        node = _walk_tree_step(*pending.pop(), pending, active)
        if node is not _TREE_EXIT:
            yield node


def _opaque_collection_member(value) -> bool:
    return _is_opaque_text(value) or (isinstance(value, list) and bool(value))


def _empty_container(value):
    if isinstance(value, dict):
        return {key: None for key in value}
    return [None] * len(value)


class _TreeRewrite:
    """Preserve child order and acyclic sharing without a depth cutoff.

    Only ancestors are marked live: visiting the same acyclic child on a
    different branch is valid. Each result is independent of the input tree.
    """

    def __init__(self, prepare, finish, *, descend_lists=True):
        self.prepare = prepare
        self.finish = finish
        self.descend_lists = descend_lists
        self.pending = []
        self.active = set()

    def run(self, value):
        result = {}
        self.pending.append(("enter", (value, result, "root")))
        while self.pending:
            action, arguments = self.pending.pop()
            {"enter": self._enter, "leave": self._leave}[action](*arguments)
        return result["root"]

    def _is_container(self, value):
        return isinstance(value, dict) or (self.descend_lists and isinstance(value, list))

    def _enter(self, original, parent, key):
        if not self._is_container(original):
            parent[key] = original
            return
        _claim_tree_container(original, self.active)
        prepared = self.prepare(original)
        target = _empty_container(prepared)
        self.pending.append(("leave", (original, target, parent, key)))
        self._schedule_children(prepared, target)

    def _schedule_children(self, prepared, target):
        for key, value in reversed(_children(prepared)):
            self.pending.append(("enter", (value, target, key)))

    def _leave(self, original, target, parent, key):
        parent[key] = self.finish(target)
        self.active.remove(id(original))


def _module_free_row(value):
    if isinstance(value, dict):
        return _row_without_its_module(value)
    return value


def _pruned_mapping(value, *, drop, omitted: set):
    if not isinstance(value, dict):
        return value
    kept = {}
    for key, item in value.items():
        _keep_pruned_entry(kept, key, item, drop, omitted)
    return kept


def _keep_pruned_entry(kept: dict, key, value, drop, omitted: set) -> None:
    if drop(key, value):
        omitted.add(key)
        return
    kept[key] = value


def _collect_row_list(value, found: list) -> None:
    if isinstance(value, list) and _is_row_list(value):
        found.append(value)


def _row_without_identity(omitted: set, value):
    if not isinstance(value, dict):
        return value
    if not _identity_is_recoverable(value):
        return value
    omitted.add("identity_key")
    return {key: item for key, item in value.items() if key != "identity_key"}




def _without_recoverable_identities(value, omitted: set):
    return _TreeRewrite(_unchanged, partial(_row_without_identity, omitted)).run(value)


def _is_opaque_identifier(key: str, value) -> bool:
    if key in PROTECTED_FIELDS:
        return False
    return _is_opaque_text(value) or _is_opaque_collection(value)


def _is_opaque_text(value) -> bool:
    return isinstance(value, str) and bool(_OPAQUE_IDENTIFIER.match(value))


def _is_opaque_collection(value) -> bool:
    """Every leaf is opaque; cycles are invalid, and aliases are allowed."""
    if not isinstance(value, list) or not value:
        return False
    return all(_opaque_collection_member(item) for item in _tree_nodes(value))


def _is_derivable(key: str, value) -> bool:
    del value
    return key in DERIVABLE_FIELDS


def _pruned(value, drop, omitted: set):
    prepare = partial(_pruned_mapping, drop=drop, omitted=omitted)
    return _TreeRewrite(prepare, _unchanged).run(value)








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


# The second columnar move, added 2026-09-12: once the constant columns are
# hoisted out, what remains still spells every key name again on every row.
# A header stated once plus one array per row says the same thing, and the
# parity run measured the cost of not doing it — 949 tokens against 56 on one
# caller list. Chosen the same way as the constants above: both shapes are
# estimated and the cheaper one wins, so a two-row table does not move.
# Research: `docs/research/2026-09-12-three-changes-to-pass-them.md`.


# The third columnar move, 2026-09-12: a string column can hold one prefix on
# every row. The architecture summary lists 110 entry points, and each row spelt
# the vault's own absolute path again — 2 156 of that answer's tokens, a quarter
# of them one prefix said 110 times, in an answer whose top level already names
# the directory. Stated once under `<key>_row_prefixes`, the value is still in
# the answer and the rows carry what differs. Chosen by measuring both shapes,
# like every compaction above it.


def _common_prefix(values: list[str]) -> str:
    """The longest prefix every value shares, cut at the last path separator."""
    shared = os.path.commonprefix(values)
    return shared[: shared.rfind("/") + 1]


def _prefixable_column(rows: list, key: str) -> str:
    values = [row[key] for row in rows]
    if not all(isinstance(value, str) for value in values):
        return ""
    prefix = _common_prefix(values)
    return prefix


def _row_prefixes(rows: list) -> dict[str, str]:
    """Every column whose values share a path prefix, and that prefix."""
    found = {}
    for key in sorted(rows[0]):
        prefix = _prefixable_column(rows, key)
        if prefix:
            found[key] = prefix
    return found


def _without_prefixes(rows: list, prefixes: dict[str, str]) -> list[dict]:
    return [
        {
            key: value[len(prefixes[key]):] if key in prefixes else value
            for key, value in row.items()
        }
        for row in rows
    ]


def _prefixes_if_cheaper(key: str, rows: list) -> dict:
    if not _is_uniform_table(rows):
        return {}
    prefixes = _row_prefixes(rows)
    if not prefixes:
        return {}
    return _cheaper_of(key, rows, prefixes)


def _cheaper_of(key: str, rows: list, prefixes: dict) -> dict:
    """The prefixed shape, but only when it really costs fewer tokens."""
    compacted = {key: _without_prefixes(rows, prefixes), f"{key}_row_prefixes": prefixes}
    if estimate_tokens(compacted) >= estimate_tokens({key: rows}):
        return {}
    return compacted


def _is_uniform_table(rows: list) -> bool:
    """Every row carries exactly the same keys, so a header cannot lose one."""
    if not _is_constant_table(rows):
        return False
    first = set(rows[0])
    return all(set(row) == first for row in rows)


def _columnar(key: str, rows: list, cols: list[str]) -> dict:
    return {
        f"{key}_cols": cols,
        key: [[row[name] for name in cols] for row in rows],
    }


def _columnar_if_cheaper(key: str, rows: list) -> dict:
    """The header-plus-arrays form of a table, when it costs fewer tokens."""
    if not _is_uniform_table(rows):
        return {}
    cols = sorted(rows[0])
    columnar = _columnar(key, rows, cols)
    if estimate_tokens(columnar) >= estimate_tokens({key: rows}):
        return {}
    return columnar


def _compacted_entry(key, value) -> dict:
    """Children are already shaped; preserve every profitable table constant."""
    constants = _payable_row_constants(key, value)
    if not constants:
        return {key: value}
    return {key: _hoisted_rows(value, sorted(constants)), f"{key}_row_constants": constants}


def _shortened_rows(key, value) -> dict:
    prefixes = _prefixes_if_cheaper(key, value)
    if prefixes:
        return prefixes
    return {key: value}


def _columnar_entry(key, value) -> dict:
    shortened = _shortened_rows(key, value)
    columnar = _columnar_if_cheaper(key, shortened[key])
    if columnar:
        return {**shortened, **columnar}
    return shortened


def _columnar_dict(value):
    if not isinstance(value, dict):
        return value
    compacted: dict = {}
    for key, item in value.items():
        compacted.update(_columnar_entry(key, item))
    return compacted


_PATH_KEYS = ("relative_path", "path", "file")
_NAME_KEYS = ("qualified_name", "name")


def _dotted_path(path: str) -> str:
    """A file path as a dotted trail: scripts/retrieval.py → scripts.retrieval."""
    stripped = str(path).replace("\\", "/").removesuffix(".py")
    return stripped.strip("/").replace("/", ".")


def _name_without_its_path(path: str, name: str) -> str:
    """The part of a qualified name the path does not already spell.

    The longest match wins, so `scripts.retrieval._fused_candidates` beside a
    path ending `scripts/retrieval.py` becomes `_fused_candidates`, while
    `pkg.mod.Class.method` keeps `Class.method` — the class is not in the path.
    An absolute path is fine: what matters is that the trail *ends* with the
    module.
    """
    dotted = _dotted_path(path)
    parts = name.split(".")
    for index in range(len(parts) - 1, 0, -1):
        if dotted.endswith(".".join(parts[:index])):
            return ".".join(parts[index:])
    return name


def _first_present(mapping: dict, keys) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value:
            return key
    return None


def _shortened_name(mapping: dict, path_key: str, name_key: str) -> dict:
    name = mapping[name_key]
    shorter = _name_without_its_path(mapping[path_key], name)
    if shorter == name:
        return mapping
    return {**mapping, name_key: shorter}


def _row_without_its_module(mapping: dict) -> dict:
    """A row that carries its file path spells the module twice; drop one.

    `["scripts/retrieval.py", 3113, "scripts.retrieval._fused_candidates"]` says
    `scripts.retrieval` in two shapes, and the path is the one a reader opens.
    What is left of the name — a bare symbol, or `Class.method` — is what the
    path cannot say. Measured 2026-09-13: 608 tokens to 558 on one `callers`
    answer. Research:
    `docs/research/2026-09-13-a-shorter-answer-and-a-fresher-line.md`.
    """
    path_key = _first_present(mapping, _PATH_KEYS)
    name_key = _first_present(mapping, _NAME_KEYS)
    if path_key is None or name_key is None:
        return mapping
    return _shortened_name(mapping, path_key, name_key)


def _without_repeated_modules(value):
    return _TreeRewrite(_module_free_row, _unchanged).run(value)






def _as_columnar(value):
    """Apply last: the budget still reads original row objects before this step.

    Lists intentionally remain opaque to this traversal, as in the original
    columnar contract. A table's dictionaries are not independently reshaped.
    """
    return _TreeRewrite(_unchanged, _columnar_dict, descend_lists=False).run(value)


def _row_constant_dict(value):
    if not isinstance(value, dict):
        return value
    compacted: dict = {}
    for key, item in value.items():
        compacted.update(_compacted_entry(key, item))
    return compacted




def _with_row_constants(value):
    return _TreeRewrite(_unchanged, _row_constant_dict).run(value)


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
    state["answer"] = _pruned(state["answer"], _is_derivable, omitted)
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
    _collect_row_lists(answer, found)
    return sorted(found, key=len, reverse=True)


def _collect_row_lists(value, found: list) -> None:
    for item in _tree_nodes(value):
        _collect_row_list(item, found)








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
