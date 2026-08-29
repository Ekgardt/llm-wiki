# A weight that decides order must also decide admission

Date: 2026-08-29. Question: both vault stands fell below their own gates on
HEAD — `hit@5` 0.3 against a 0.6 gate, `applied@5` 0.2857 against a recorded
0.857 — with no retrieval commit to blame.

## What was measured

Machine quiet throughout: load average 0.70–1.38 at every measurement, 4 vCPU.
Reproduced on HEAD before any change, through the stands' own default entry
point (the MCP wrapper, budget and all):

    uv run python benchmark/run_vault_retrieval.py --repeat 3
    uv run python benchmark/run_vault_application.py

| measure | HEAD | after | gate |
|---|---|---|---|
| `hit@1` | 0.3 | 0.4 | — |
| `hit@5` | 0.3 | 0.8 | 0.6 |
| `applied@5` | 0.2857 | 0.8571 | gain over grep |

`hit@5` was 0.7 before the stale gold was corrected (see below). Spread across
three runs was nil in both directions — `min == max` on every metric — so this
is not the run-to-run wander recorded on 2026-08-26.

The `grep` baseline fell with the product: `applied` 0.429 → 0.1429,
`hit@5` → 0.0. That was the first real clue. `grep` never touches retrieval, the
generation, or the corpus rule, so a fall in both pointed at the *files*, not
the code — and the gold pages were all still on disk.

## The cause

Not a code regression. The corpus changed under a rule that could not absorb it.

- `docs/` now carries **1,409 chunks** against `knowledge/notes`' **620**.
  `docs/research` alone is 660 — more than every compiled page in the vault.
- **49 of the 87 research notes were written on 2026-08-28 and 2026-08-29**,
  after the last good measurement on 2026-08-26.
- The stand's questions are Russian; the decision pages are English; the
  commentary is Russian. `multilingual-e5-small` rewards the same-language
  match, so the commentary took every high cosine.

Measured directly against the active generation's vectors, the gold page's best
chunk ranked **117, 123, 163, 175, 192, 244, 310** by raw cosine out of 3,498.
The dense leg over-fetches `limit * 3` = 120 rows. So for eight of ten questions
the right page was **never returned by the backend at all** — confirmed by
holding the pool at `limit=5` semantics and inspecting the full 120-candidate
fused pool: `goldrank = None` in 9 of 10 cases.

The vault had already decided this ranking question. `TYPE_WEIGHTS` gives
`decision` 1.25 and `doc` 0.8, and the comment on `doc` records the identical
failure from 2026-08-24: "with it at neutral, the audit register was the first
result on all ten stand questions and `hit@5` fell from 0.7 to 0.4."

But that weight is applied in `_weigh_by_trust`, **after** fusion — so it
ordered candidates that were already in the pool and had no say in which
candidates entered it. While the commentary was small that distinction did not
matter. Once the commentary outnumbered the compiled pages 2.3:1 *in the
language of the questions*, admission became the binding constraint and the
ordering rule became unreachable.

The asymmetry was already visible in the code and nobody had read it that way:
`_generation_result` multiplies the **lexical** rank by `trust_weight`, so the
lexical leg has always decided admission by trust. `_vector_scored_rows` then
overwrote that score with raw cosine. One leg obeyed the rule, the other did not.

## The fix

One multiplication in `scripts/search_memory.py::_vector_scored_rows`: the dense
leg scores by `cosine * trust_weight(authority, type)`, the same weight the
lexical leg already applies at the same stage. The gold pages move from ranks
117–310 to **1–14**.

Absent provenance weighs 1.0 by `trust_weight`'s own documented contract, so a
row carrying none is admitted on cosine alone rather than refused.

Blast radius, from the graph rather than from reading: `_vector_scored_rows` has
six inbound callers and every one of them is on the dense generation read path
(`_generation_vector_rows` → `_generation_vectors_search` →
`_generation_vector_hits` / `_dense_hits_or_none` → `_generation_search_results`
/ `_generation_dense_hits`). Nothing outside dense retrieval consumes it.

## Alternatives measured, not guessed

- **Deepen the over-fetch** (`limit * 30` instead of `limit * 3`). The author
  predicted from the arithmetic of rank-only RRF that this could not work, and
  **the measurement refuted the prediction**: it recovers `hit@5` 0.6 and
  `applied@5` 0.8571 too. It was rejected anyway, on a different ground: it
  reaches rank 150, the observed deficits already run to 310, and the commentary
  grows daily. It buys the number without touching the reason, and the next
  fifty research notes take it back.
- **Intent-conditional weight at admission** (`source_type_weight(...,
  curated_first=True)`, the capped form fusion uses). Measured identical on both
  stands to the plain `trust_weight`, and it would have needed the query intent
  plumbed through seven signatures across two modules. Rejected as strictly more
  machinery for no measured gain.

## One stale gold, corrected with evidence

`daily-entry-boundary` named `knowledge/notes/daily-entry-boundary-decision.md`,
which carries `status: superseded` and `superseded_by:
[[daily-entry-quote-anchor-decision]]` since 2026-08-24. The corpus deliberately
excludes superseded pages (`NEW-67`), so that case **could never pass**, before
or after this fix. The gold now names the active successor, whose own summary
answers the question asked. This is worth +0.1 on `hit@5` and is reported
separately from the fix for that reason: 0.7 fixed-only, 0.8 with the gold
corrected. Both clear the 0.6 gate.

## What this does not claim

Not measured: whether code-shaped questions changed. No stand covers them; their
invariance rests on `tests/test_intent_conditional_trust.py` and the suites
below, which pass. The dense leg's `limit * 3` over-fetch is left as it was —
this change makes the weight act inside it rather than widening it.

Also unaddressed, and visible throughout the diagnosis:
`benchmark/vault-retrieval-v1.json` — this stand's own answer key — appeared in
the top five of **9 of 10** cases before the fix. It is indexed corpus like
anything else under a code root. The `doc`/`code` weighting now pushes it down,
but a benchmark whose answer sheet is inside the index it measures is a
contamination question this note does not settle.

The generation was not rebuilt for any of this. The diagnosis and the fix both
run against the generation already active — `generation-18d0425dd6fa9f88-7c7e7a5f`,
`vector_state: complete`, 910 sources, 3,498 chunks — so no rebuild cost was
paid and the active pointer was never moved.

## Verification

- Managed gate exit 0 on `scripts/search_memory.py` and the new test file.
- `uv run ruff check scripts/ tests/ benchmark/` clean.
- `tests/test_dense_admission_trust_weight.py`: 3 tests, 2 fail on HEAD without
  the fix, all pass with it. The third is a guard — a large cosine gap must
  still win — and passes both ways by design.
- Every suite importing `search_memory` (23 files) plus
  `tests/test_intent_conditional_trust.py`: **1333 passed, 7 skipped**.
