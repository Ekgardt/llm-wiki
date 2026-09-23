# The window covers the question first

Dated 2026-09-22. Mechanism 1 of the plan in
`docs/research/2026-09-22-a-class-level-plan-to-pass-the-field.md`, made precise by
`docs/research/2026-09-22-what-the-sciences-really-lend-a-memory.md`: before the reader's
window is filled by the lane score, it first holds one chunk from every source that covers
a distinct part of the question. This replaces the conditional per-source quota merged on
2026-09-19 (`docs/research/2026-09-19-one-session-does-not-take-the-window.md`).

Files: `scripts/retrieval.py`, `scripts/fact_keys.py`, `scripts/search_memory.py`,
`tests/test_one_source_does_not_take_the_window.py`,
`tests/test_the_window_covers_the_question_first.py`,
`docs/research/2026-09-22-the-window-covers-the-question-first.md`.

## What was found in the code

- The order a caller sees is `_visible_order`: page diversity, the lane score over
  episodes, the per-source quota, the exact-filename promotion; then `_capped` cuts the
  window (`QA_MAX_CANDIDATES = 12` in `query_memory`). Both call sites
  (`_executed_plan`, `_assembled_partial`) hold `progress.analysis.query` and
  `progress.limit`, so the question text and the window size can reach the order without a
  new parameter on `retrieve`.
- The question that reaches retrieval is `query_memory.searchable_question`: the
  question plus every ISO day its relative expressions resolve to, written there by
  `temporal_anchor.query_with_dates`. A stretch ("last week") arrives as its seven days; a
  point ("four weeks ago") as a day and its three neighbours each side. So the dates and
  ranges `temporal_anchor` found are already in the query text, as tokens.
- A candidate's text is `display_meta[candidate_id]["content"]`, which is what
  `lane_score.candidate_text` reads. Its file and heading are the source key the quota and
  the packer already use: `(relative_path, heading_path)`; a daily file's path carries the
  day, its heading carries `[HH:MM:SS] <event> | <session>`.
- The fact keys of a turn live in `cache/fact-keys/keys.sqlite3`, keyed by the turn's
  `span_sha256`, and are indexed beside the turn in the v2 search table. A generation hit
  (`search_memory._generation_result`) carries neither `keys` nor `span_sha256` nor byte
  offsets, and a chunk id is a canonical hash. So a hit gives no handle on its keys today.
  The smallest change that gives one: select `span_sha256` — a column of both artifact
  versions, already in `_FTS_CHUNK_SELECT` — and put it on the hit row.
- The pointers a compiled page carries to its source episodes are in its own text: the
  `## Evidence` lines are `` `daily:<day> sha256:<digest> block:<HH:MM:SS> bytes:<a>-<b>` ``
  (`compile_memory._evidence_lines`), and the `## Claims` ledger is a JSON block whose
  every claim has `"evidence": {"reference": "daily:..."}` and an `observed_at`
  (`compile_memory._derived_claim`, `_with_claim_ledger`). Both name the daily file and the
  block whose heading opens with that time. The compile receipt binds the same page to the
  same daily file by logical path and digest; it repeats the pointer the page already
  states, so it is not read at query time. Wikilinks between pages are read by
  `co_activation.links_in`. `evidence_resolver.extract_evidence_references` is strict on
  purpose — a ledger line or a chunk cut in the middle of a reference raises — so the
  query-time scan uses the resolver's own `_REF_RE` to find references and skips what is
  not one.
- `aggregation_pass.asks_to_aggregate` says whether the question counts or sums;
  `counted_kind` names the stem of what it counts. Its model-written sub-queries
  (`fan_out_queries`) need a provider and belong to the answer pass, not to candidate
  selection.
- The recorded runs: `lane_matrix` rows of `full-2026-09-18` carry no `source` (the field
  was added on 2026-09-19) and no text; the LoCoMo run of 2026-09-19 carries no `source`
  either, and no evidence label at all — `coverage.sessions_labelled` is 0 on all 300 rows
  and `evidence` is false on every row, because the stand maps `locomo_evidence`
  (`D7:19`) to no `answer_session_ids`. So neither rule can be replayed candidate by
  candidate on either stand, and nothing about sources can be bounded on LoCoMo.

## The mechanism

Aspects of a question are read from the question text and the store only, never from a
benchmark label:

1. **Terms** — `_query_terms` over the query (case folded, `[\w-]+`), minus the lexical
   leg's own `_QUERY_STOPWORDS`, minus ISO days.
2. **Dates and ranges** — the ISO days in the query, grouped: a run of consecutive days is
   one aspect (a stretch or a widened point), a lone day is one aspect. A chunk covers a
   date aspect when its path, heading or text names a day inside the range.
3. **Fact keys** — a chunk's coverable text is its content plus its fact keys, read from
   the store by the hit's `span_sha256`, so a turn that states a fact in other words than
   the question still covers the term. Absent store, absent keys: text only.
4. **Sub-questions of a count** — when `asks_to_aggregate` is true and `counted_kind`
   names a kind, every source whose text mentions that kind is a distinct aspect of its
   own ("one instance in this source"), which is the multi-answer objective: a count needs
   every instance, and each lives in its own session.

Selection is greedy maximum coverage over sources: a source's candidate chunk is the one
of its chunks that covers the most still-uncovered aspects (ties: the lane-best), the
source with the largest gain is taken, the aspects it covers are struck out, and the loop
stops when the best gain is zero or the window is full. Then index-driven completion: for
each admitted chunk of a compiled page, the daily blocks its references name and the pages
its wikilinks name are looked up among the pool's sources; those not yet admitted get their
best chunk, until the window is full. Then the fill: everything else in lane order.

The window is changed as little as that requires. Admitted chunks that already sit inside
the window keep their place. Admitted chunks from outside the window are pulled in, in
lane order, and take the places of the last non-admitted chunks of the window, which are
deferred behind them; nothing is dropped, `_capped` still cuts last. Properties, each
pinned by a test:

- a pool holding one source comes out unchanged (there is nobody to cover for);
- a pool whose sources all hold a place already comes out unchanged, the sharpest case
  being a pool that fits the window;
- a source's only chunk is never deferred by the rule's own choice: only a chunk of a
  source that keeps another chunk inside the window is ever displaced;
- a source's chunks keep their order, except the one chunk admitted for coverage, which
  is its lane-best whenever the lane-best covers as much — so with equal coverage the
  order is exactly the lane's.

The quota is removed rather than kept beside this: coverage admits every source with a
distinct aspect before the fill, which is the quota's purpose, and it leaves the fill to
the lane score, which is where the quota paid all-turns@12 0.566 against 0.698
(2026-09-15). The baseline arm of the decision run is the commit before this one.

## What the recorded rows can bound, and what they cannot

`cache/benchmarks/full-2026-09-18/lme500.judged.jsonl`, 470 answerable rows. The quota's
bounds are the ones its note computed, re-run today with `replay_quota.py` (cap 2):

| longest session | quota must bind | missing a needed source | can gain one | loses evidence (floor) | rows demoted | of which judged correct |
|---|---:|---:|---:|---:|---:|---:|
| 1 × average | 369 of 470 | 41 | 20 | 7 | 14 | 5 |
| 2 × average | 158 of 470 | 41 | 8 | 3 | 8 | 1 |

By type at 1 × average: multi-session 19 missing / 11 can gain / 0 lose;
temporal-reasoning 19 / 9 / 0; single-session-assistant 1 / 0 / 5 lose (12 rows);
single-session-preference 2 / 0 / 2 lose; knowledge-update and single-session-user 0.

Cover-then-fill, same rows, same method (`replay_cover.py`, job scratch). It frees a slot
wherever the window holds a second row of a labelled source, not only a third, so its gain
ceiling is higher; and it never provably demotes a row, because a labelled source that
covers a query aspect keeps its best chunk, so its loss floor is zero and only a ceiling
can be stated:

| type | n | missing a needed source | can gain one | loses evidence (floor) | may lose (ceiling, questions) | rows at risk (ceiling) | of which judged correct |
|---|---:|---:|---:|---:|---:|---:|---:|
| knowledge-update | 72 | 0 | 0 | 0 | 10 | 10 | 10 |
| multi-session | 121 | 19 | 16 | 0 | 16 | 16 | 11 |
| single-session-assistant | 56 | 1 | 0 | 0 | 9 | 21 | 5 |
| single-session-preference | 30 | 2 | 0 | 0 | 6 | 8 | 5 |
| single-session-user | 64 | 0 | 0 | 0 | 4 | 4 | 3 |
| temporal-reasoning | 127 | 19 | 12 | 0 | 45 | 48 | 43 |
| **all** | **470** | **41** | **28** | **0** | **90** | **107** | **77** |

Read plainly: the quota can gain a needed source on at most 20 questions and must lose an
evidence row on at least 7; coverage can gain on at most 28 and must lose on none, but
could lose on up to 90 if every displaced tail row were evidence and every pulled source
covered a new aspect — the ceiling is loose by construction, since it counts every second
evidence row of a labelled source as at risk. The rows cannot say where between 0 and 90
the truth lies: the text and the source of each row were not recorded. That is exactly
what one run of the frozen tree measures. On LoCoMo nothing can be bounded at all until the
stand labels sessions from `locomo_evidence`.

## The precondition the biology report asked for

RESEARCH-D §2.4: before writing index-driven completion, count how many of the missing
sessions are linked to a found one by any pointer. On the stand's vault no compiled page,
receipt, claim or wikilink exists — `ingest_sessions` writes each session as a daily block
and builds a generation, nothing compiles — so the only store link between two sessions
there is the daily file they share. Over the 500 rows of `lme500.jsonl`, 45 questions miss
a labelled session (62 sessions):

| linked to a found one by the daily file | questions | sessions |
|---|---:|---:|
| yes — every labelled session on one calendar day, at least one found | 15 | 19 |
| no — none of the labelled sessions found at all | 8 | — |
| no — every labelled session on a distinct day | 20 | — |
| undecidable from the rows — mixed days | 2 | 3 |

So on the stand, at most 22 of 62 missing sessions (19 certain, 3 possible) sit in a file
already in the window, and the pointer that would reach them is the file itself — which the
lexical and dense legs already index by chunk, so a file-level pointer adds nothing a
better selection does not. The page-to-episode pointers this note implements exist only on
a vault that compiles pages; the stand has none, and the run will show their effect as
zero there. Their value is on the live vault, and that is not measured yet.

## Practice on this date

- Greedy maximum coverage: "It can be shown that this algorithm achieves an approximation
  ratio of 1 − 1/e" — G. L. Nemhauser, L. A. Wolsey and M. L. Fisher, *An analysis of
  approximations for maximizing submodular set functions I*, Mathematical Programming 14
  (1978), 265–294; and no polynomial algorithm does better: the problem "cannot be
  approximated to within 1 − 1/e + o(1) ≈ 0.632" — Feige, *A Threshold of ln n for
  Approximating Set Cover*, JACM 45(4), 1998. Quoted from the encyclopaedia entry, fetched
  today; the Springer record of the 1978 paper sits behind a login.
- The engineered analogue: "multi-answer retrieval, an under-explored problem that
  requires retrieving passages to cover multiple distinct answers for a given question";
  JPR "achieves significantly better answer coverage on three multi-answer datasets" — Min,
  Lee, Chang, Toutanova, Hajishirzi, *Joint Passage Ranking for Diverse Multi-Answer
  Retrieval*, EMNLP 2021, arXiv 2104.08445, fetched today.
- Index-driven completion: "a partial cue that activated the index could activate the
  neocortical patterns and thus retrieve the memory of the episode" — Teyler & Rudy,
  *Hippocampus* 2007; "the whole of the memory can be retrieved and completed from any
  part" — Rolls, *Front Syst Neurosci* 2013; both quoted from RESEARCH-D, opened today.
- Sessions first, then turns: RESEARCH-A §M2 — a rule that "selects **sessions** for
  coverage, and turns inside them by relevance", checked "without a model run, over the
  recorded candidate orders", which the recorded rows of this repository do not allow yet.
- Our own contrary measurement stands: a hard cap costs all-turns@12
  (`docs/research/2026-09-15-what-a-slot-should-reward.md`), which is why the fill here is
  the lane order and the only rearrangement is the pull of covering sources from outside
  the window.

## Sources

- [Maximum coverage problem — the greedy 1 − 1/e bound and Feige's threshold](https://en.wikipedia.org/wiki/Maximum_coverage_problem) — fetched 2026-09-22; cites Nemhauser, Wolsey, Fisher, Mathematical Programming 14 (1978) 265–294, and Feige, JACM 45(4) 1998.
- [Min et al., Joint Passage Ranking for Diverse Multi-Answer Retrieval, EMNLP 2021 (arXiv 2104.08445)](https://arxiv.org/abs/2104.08445) — fetched 2026-09-22.
- [Nemhauser, Wolsey, Fisher 1978 at Springer](https://link.springer.com/article/10.1007/BF01588971) — behind a login on 2026-09-22; quoted through the entry above.
- RESEARCH-A, RESEARCH-D, RESEARCH-E of 2026-09-22 (job scratch, not tracked) for Teyler & Rudy 2007, Rolls 2013, HippoRAG (NeurIPS 2024) and EmergenceMem.
- `docs/research/2026-09-19-one-session-does-not-take-the-window.md`, `docs/research/2026-09-15-what-a-slot-should-reward.md`, `docs/research/2026-09-16-one-score-over-the-lanes.md`.

## What only a run can settle

Whether the gain ceiling (28) or the loss ceiling (90) is nearer the truth; whether the
count aspects (one per source mentioning the kind) help the counting questions or crowd out
the turn-level evidence of single-session ones; and the decision rule of the plan: paired
bootstrap on the decide halves of both stands, no type losing more than 1.5 points, both
refusal errors reported. The baseline arm is the commit before this change; the stand's
LoCoMo rows must first carry `answer_session_ids` derived from `locomo_evidence` before its
session coverage can be read at all.
