# One piece found twice is one candidate

Dated 2026-09-17. Found by the third audit (retrieval, L5); the research before the fix.

Files: `scripts/query_memory.py`, `tests/test_one_piece_found_twice_is_one_candidate.py`

## What was found

- `query_memory._merged` joins the first candidates with what further searches find, and
  promises: "A piece that the question and a sub-query both found outranks one that a single
  search found" — one vote per search, first order among equals.
- Two candidates are "the same piece" by `_candidate_key`: the identifier when the candidate
  has one, otherwise `(path, byte_start)`. Retrieval rows carry an identifier; the rows of the
  keys leg and the cited spans of a second pass carry only a path and a position. The same
  piece arriving from retrieval and from the keys leg therefore had two different keys, never
  collapsed, and never earned the second vote. The duplicate was removed later, when the
  candidates were resolved to chunks, so nothing was read twice — only the agreement was lost.

## Practice on this date

- Rank fusion counts, per document, the rankings that returned it: `RRFscore(d ∈ D) =
  Σ_{r ∈ R} 1 / (k + r(d))` (Cormack, Clarke, Büttcher, "Reciprocal Rank Fusion outperforms
  Condorcet and individual Rank Learning Methods", SIGIR 2009). That requires one identity per
  document across every ranking that is fused.
- The identity every one of these candidates has is its place in the source: the path and the
  byte where the piece begins. An identifier exists only on some of them.

## The decision

- `_candidate_key` names a piece by `(path, byte_start)` whenever the candidate carries both,
  and by its identifier only when it does not. Every leg's rows then meet under one key.
- Nothing else changes: the vote count, the cap and the order among equals are as they were.
- The effect on the stand is a reordering of the reader's pool when two legs agree. It makes
  the merge do what its measured description of 2026-09-08 says; it is not a new policy, and
  it has not been measured on the stand (no run was permitted for this work).
