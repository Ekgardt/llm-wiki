# The contradiction check compares what can matter

Date: 2026-09-25. Audit items B-3, B-5 and B-8 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code, and on a copy of the live vault)

- B-8. `ClaimIndex.candidates` selected active claims with the same subject, the
  same fingerprint, or the same relation, `LIMIT 50`. A claim with a different
  subject is `unrelated` by the third deterministic rule, before relation is
  read, so every relation-only row is noise. On the copy of the live vault the
  102 quarantined claims drew 2,911 candidates, about 29 each; for the 8 of them
  still quarantined today, 197 of their 205 candidates were `unrelated`. The rows that could matter — same
  subject — were ordered first, but past 50 the cut was silent.
- B-3. With no deterministic verdict (`relation` or qualifier scope differs),
  compile asked two model evaluators (`_evaluate_with_providers`, evaluators
  `None` outside the fake provider). Outside the benchmark gate
  `apply_policy(deterministic=None)` quarantines whatever they answer
  ("Semantic supersession is intentionally disabled"). The calls cannot change
  the result, cost tokens, and send the private claim text to a provider.
- B-5. `intervals_overlap` compared ISO instants as text. `.` sorts before `Z`,
  so `2026-09-01T00:00:00.500000Z` sorted before `2026-09-01T00:00:00Z`; an
  interval ending at the first and one starting at the second were called
  overlapping.

## Source

- Python `datetime.fromisoformat`, https://docs.python.org/3/library/datetime.html#datetime.datetime.fromisoformat
  (fetched 2026-09-25): since 3.11 it accepts `Z` and fractional seconds; before
  3.11 only what `isoformat()` emits, so `Z` must be spelled `+00:00` on the
  project's 3.10 floor (`requires-python = ">=3.10"`).
- Zep, https://arxiv.org/html/2501.13956 (fetched 2026-09-25): new facts are
  compared "against semantically related existing edges" — related facts, not
  every fact sharing a predicate.

## Decision

- B-8: candidates are the active claims with the same subject or the same
  fingerprint, all of them. The bound is `MAX_ACTIVE_RECORDS`, and past it the
  lookup refuses, as `active_records` already does, instead of returning a prefix.
  The `limit` argument and `MAX_CANDIDATES` go.
- B-3: the compile builds its pipeline with no evaluators, so a pair with no
  deterministic verdict is quarantined without a model call, as it was after
  one. The frozen contradiction benchmark keeps its evaluators: it measures their
  classes (`class_macro_f1`) to decide whether the gate may ever be opened, and
  an explicit `check_contradiction` question still gets a semantic class.
- B-5: instants are parsed to aware datetimes (`Z` read as `+00:00`) before
  they are compared.

## Files

- `scripts/claims.py`
- `scripts/contradiction_pipeline.py`
- `scripts/compile_memory.py`
- `tests/test_claims.py`
- `tests/test_contradiction_benchmark.py`
- `tests/test_the_contradiction_check_compares_what_can_matter.py`
- `CHANGELOG.md`
