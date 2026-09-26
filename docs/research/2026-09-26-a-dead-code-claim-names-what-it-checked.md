# A dead-code claim names what it checked

Date: 2026-09-26. Audit 2026-09-26 A-7.

## Facts

- The navigation auditor ran `find_dead_code` on a TypeScript/JavaScript/Go/Rust
  checkout: 8 of 11 rows under `zero_confirmed_incoming_calls` were live code.
- `code_graph._reference_index` reads `graph.python_sources` only
  (`value_references.build_reference_index`), so for any other language the
  claim "nothing names it anywhere" was never checked; `_dead_code_reason` and the
  live path `_live_dead_candidate` still gave the strongest verdict.
- Vulture (https://github.com/jendrikseipp/vulture, fetched 2026-09-26) states its
  confidence per finding kind — "a value of 100% signals that it is certain that
  the code won't be executed", 60 % for functions — i.e. a dead-code tool says how
  strong each claim is. This product does it by reason and ordering
  (`DEAD_CODE_REASON_ORDER`, weakest cut first).

## Decision

- A row outside `.py`/`.pyi` that would have claimed `zero_confirmed_incoming_calls`
  or `referenced_without_call` says `references_not_indexed` instead; a row that
  already states doubt (`unresolved_receiver`) is kept as it is. The new reason is
  the weakest and is ordered last, so a budget cut reaches it first.
- No reference index for other languages is added here; that is a feature, not a
  correction of a false claim.

## Files

- `scripts/code_graph.py`
- `tests/test_a_dead_code_claim_names_what_it_checked.py`
- `CHANGELOG.md`
