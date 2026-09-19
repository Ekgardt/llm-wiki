# Two pages with one file name are two candidates

Dated 2026-09-17. Found by the third audit (retrieval, M7); the research before the fix.

Files: `scripts/search_memory.py`, `scripts/retrieval.py`,
`tests/test_two_pages_with_one_file_name_are_two_candidates.py`

## What was found

- The legacy search path (no active generation, or a generation whose seal changed during
  the run) names a candidate by the stem of its file: `candidate_id = Path(path).stem`, in
  four row builders of `search_memory` and in the fallback of `retrieval._legacy_candidate_id`.
- `fuse_rrf` adds up ranks per `candidate_id`. Two different pages with the same file name
  therefore become one candidate: it keeps the first page's path, earns both pages' votes,
  and the second page disappears from the result.
- Every `knowledge/projects/<slug>/state.md` has the stem `state`. Reproduced by the audit:
  the state pages of two projects fused into one candidate `state` with a doubled score, and
  the second project vanished.
- A stem is an identity only where the namespace is flat. The vault's contract makes
  `knowledge/notes/` flat ("Pages live flat as `<slug>.md` under `knowledge/notes/`"), and
  the access counts and impressions of the legacy path are keyed by that slug. Nowhere else
  does the contract promise unique file names.

## Practice on this date

- Reciprocal rank fusion is defined per document: `RRFscore(d ∈ D) = Σ_{r ∈ R} 1 / (k + r(d))`
  (Cormack, Clarke, Büttcher, "Reciprocal Rank Fusion outperforms Condorcet and individual
  Rank Learning Methods", SIGIR 2009). The sum runs over the rankings of one document `d`;
  an identifier shared by two documents adds the ranks of different documents, which the
  formula does not describe.

## The decision

- One function, `search_memory.legacy_candidate_id(path)`, names a legacy candidate: the slug
  for a page directly under `knowledge/notes/`, the relative path without its suffix for any
  other file. All four row builders and the retrieval fallback use it.
- Flat notes keep the identifier they had, so the telemetry and access counts recorded under
  slugs keep matching. Only files outside the flat namespace change, and those are exactly
  the ones whose stems were never identities.
- The generation path is untouched: it already names a candidate by its chunk.
