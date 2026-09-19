# The lane refit cannot see what it threw away

Date: 2026-09-18
Files: `benchmark/fit_lane_score.py`, `scripts/lane_score.py`,
`tests/test_the_lane_score_refit_is_one_command.py`,
`tests/test_the_reader_window_is_twelve_and_its_constants_are_pinned.py`

## The task, and what the data turned out to be

The seven constants of `scripts/lane_score.py` were fitted on 2026-09-16 over 189
questions of three types. The job was to refit them on all six types, offline, from what
the runs already recorded, and to answer honestly whether the reader's window —
`benchmark/longmemeval_vault.py::QA_CANDIDATES`, currently 12 — should widen.

Two facts about the recorded data decide the outcome, and both were measured, not assumed.

**The lane matrix holds the winners only.** `run_question` records
`lane_matrix(question, rows)` where `rows = _retrieved_rows(searchable, profile)` and that
call passes `limit=_qa_candidates()` — twelve. Inside `scripts/retrieval.py` the ordered
pool is truncated by `_capped(_visible_order(...), progress.limit)` *before* it is returned.
So every row the fitter reads is a candidate the current ranking already put in the reader's
window. Checked on the snapshot of `cache/benchmarks/full-2026-09-18/lme500.jsonl` taken at
353 rows: `retrieved` is 12 for all 353, and `len(lane_matrix)` is 12 for all 353. Nothing
from position 13 downward was ever written down.

The consequence is arithmetic. The fitter's own metric is "is every labelled row inside the
reader's depth"; with twelve rows and a depth of twelve, the answer is yes for every
question under every conceivable weight vector. Running the shipped command on that file
prints, per type and overall:

```
321 questions with labelled evidence, reader depth 12

question type                       n   shipped     refit
multi-session                     121    1.0000    1.0000
overall                           321    1.0000    1.0000
single-session-preference          24    1.0000    1.0000
single-session-user                64    1.0000    1.0000
temporal-reasoning                112    1.0000    1.0000

won 0, lost 0
```

and then offers seven constants to paste, among them `DENSE_SILENT = 1.7479` against the
shipped `-1.1359` — a sign flip on a term that decides order, from a measurement that
cannot disagree with itself. That is the defect worth fixing: a command that reports a
result it did not measure.

This is the textbook distinction between the two biases of learning from logged rankings.
Ovaisi et al., *Correcting for Selection Bias in Learning-to-rank Systems* (arXiv
2001.11358), separate position bias — "the fact that higher ranked results are more likely
to be clicked even if they are not the most relevant results" — from selection bias, where
"clicked documents are reflective of what documents have been shown to the user in the
first place". Our file carries only the second kind, in its purest form: the label set is
exactly the presented set. Joachims et al., *Unbiased Learning-to-Rank with Biased
Feedback* (arXiv 1608.04468), state the same premise for the first kind — "position bias in
search rankings strongly influences how many clicks a result receives, so that directly
using click data as a training signal in Learning-to-Rank (LTR) methods yields sub-optimal
results" — and their remedy, a propensity estimate, needs a known probability that an item
was shown. For an item truncated away at rank 13 that probability is zero, and no
reweighting recovers it.

**Two of the six types are not in the file yet.** The 2026-09-18 run walks the dataset in
the same grouping as 2026-09-17: the 353 completed rows are exactly the same 353 question
ids, and they hold four types — multi-session 133, temporal-reasoning 120,
single-session-user 70, single-session-preference 30. `knowledge-update` (78) and
`single-session-assistant` (56) are the last two blocks and have zero rows. The
2026-09-17 run has all six types and all 500 rows but predates the recorder: it carries no
`lane_matrix` at all. So "refit on all six types" has no six-type file to be refitted on
today, independent of the truncation problem.

## What the data can still answer

**Within the window, ordering loses nothing.** `evidence_missed` is 0 for all 353 rows: the
compiler placed every evidence chunk it was handed. `packer_dropped` averages 1.81 of the
twelve items, but `rendering_shed` is 0 everywhere and no evidence went with it. The
deepest position an evidence row occupies is, per type, mean 1.67 (single-session-user),
4.21 (single-session-preference), 4.81 (multi-session), 5.25 (temporal-reasoning) — well
inside twelve. Re-ordering the twelve cannot move an evidence row into a window that
already contains it.

That the ranking is not vacuous at *shallower* depths is easy to show and worth recording,
because it says where a refit would pay if the window ever shrank. On the same 321
questions, "all evidence inside depth", shipped against a five-fold cross-validated refit:
depth 4, 0.6480 → 0.6386; depth 6, 0.7445 → 0.7757; depth 8, 0.8349 → 0.8629. The gains sit
on multi-session and temporal-reasoning; single-session-preference loses at depth 4 (0.7083
→ 0.5000) and depth 6 (0.7917 → 0.7083). None of it is the reader's depth.

**Widening the window buys nothing measurable.** One run on disk measured the thing
directly: `cache/benchmarks/full-2026-09-14/lme-retrieval.jsonl`, a retrieval-only run of
201 questions that records `coverage_depths` at k12, k24 and k48 over one ranked list of 48.

| question type | n | all_turns@12 | all_turns@24 | all_turns@48 | turn_recall@12 | turn_recall@24 | turn_recall@48 | all_sessions@12 | all_sessions@24 |
|---|---|---|---|---|---|---|---|---|---|
| multi-session | 101 | 0.2475 | 0.2475 | 0.3168 | 0.4718 | 0.4738 | 0.5332 | 0.9505 | 0.9703 |
| single-session-preference | 30 | 0.2000 | 0.2000 | 0.2000 | 0.2611 | 0.2611 | 0.2778 | 0.9667 | 1.0000 |
| single-session-user | 70 | 0.6143 | 0.6143 | 0.6429 | 0.6286 | 0.6286 | 0.6571 | 1.0000 | 1.0000 |
| overall | 201 | 0.3682 | 0.3682 | 0.4129 | 0.4949 | 0.4959 | 0.5382 | 0.9701 | 0.9851 |

Doubling the window from 12 to 24 leaves the all-turns share **identical** to four decimal
places and moves turn recall by +0.0010. Everything the deeper tail holds sits between 25
and 48, and even there it is +0.0447 on all-turns. Since 16 and 20 are prefixes of 24, and
the 13–24 band is worth nothing, they are worth nothing either: no ordering of the same
pool can make a prefix of an interval that contributes zero contribute more than the
interval. The run is from before the lane score shipped, so its *prefix at 12* is not
today's twelve; what it bounds is the content of the band, which is a property of the pool,
not of the order.

The cost side. No run on disk ever built a prompt from more than twelve candidates, so a
token price for 16, 20 or 24 cannot be quoted from a measurement — saying otherwise would
be inventing it. What is measured: the reader's prompt costs 4148 estimated tokens on
average (median 3727) at twelve, 9422 counting the whole prompt (median 6887); the packer
already drops items for budget on 94.9 % of questions (mean 1.81 of twelve). The product's
own prior measurement, recorded in `_qa_candidates`, is that a four-fold widening moved the
estimate from 4500 to 4727 because the answer budget binds and the surplus is shed. So
widening costs retrieval and compile work on candidates the packer throws away, buys a
measured zero in evidence, and risks pushing a real span out of the budget in favour of a
near-duplicate. Law 4 settles it.

## Decisions

1. **The seven constants stay as they are.** There is no six-type file to fit them on, and
   the four-type file cannot evaluate a change at the depth the reader uses. Pasting the
   printed constants would be asserting an improvement nobody measured. They are pinned by
   a test so that a future paste is a deliberate act with a research note behind it.

2. **The window stays 12.** Decided, not deferred: doubling it is measured at +0.0010 turn
   recall and +0.0000 all-turns, against retrieval and compile work on candidates that are
   shed 95 % of the time. `QA_CANDIDATES` is pinned by a test that names this note, so the
   number is a decision on the record rather than a default nobody revisited.
   `LLMWIKI_BENCH_QA_CANDIDATES` remains the way to sweep it in an experiment.

3. **`benchmark/fit_lane_score.py` stops handing over constants it could not evaluate.**
   A question whose matrix is no deeper than the requested depth cannot fail the metric, so
   it carries no information about the order. The command now counts those, refuses with a
   plain sentence and a non-zero status when *none* of the questions is informative, and
   otherwise reports the informative count next to the table so the reader knows how much of
   it was real. This is the class of the defect, not the instance: any future run whose
   recorder truncates at the reader's depth gets the same refusal.

4. **Left to the owner, with the reason.** The recorder must write the lane matrix over the
   ranked *pool*, not over the capped window, or no refit is ever possible. The ordered pool
   exists in `scripts/retrieval.py` one step before `_capped`; the stand-side change is in
   `benchmark/longmemeval_vault.py`, which another agent holds this round, and it only pays
   off in a fresh measurement run — and runs are the owner's to permit. Named here so the
   next run is not wasted the same way.

## Sources

- Ovaisi, Ahsan, Zhang, Vasilaky, Zheleva, *Correcting for Selection Bias in Learning-to-rank
  Systems*, arXiv:2001.11358 — fetched 2026-09-18. Quoted above verbatim.
- Joachims, Swaminathan, Schnabel, *Unbiased Learning-to-Rank with Biased Feedback*,
  arXiv:1608.04468 — fetched 2026-09-18. Quoted above verbatim.
- `cache/benchmarks/full-2026-09-18/lme500.jsonl`, snapshot at 353 of 500 rows.
- `cache/benchmarks/full-2026-09-17/lme500.judged.jsonl`, 500 rows; 301 `yes`, 39 `no`,
  160 unreadable verdicts treated as unknown, never as wrong.
- `cache/benchmarks/full-2026-09-14/lme-retrieval.jsonl`, 201 rows with `coverage_depths`.

## What only a fresh run can settle

Whether the constants should change at all. That needs a run whose recorder keeps the pool
— then the metric stops being vacuous, the two missing types are present, and a held-out
split by question id can say whether a refit wins or loses per type. Until then the honest
report is the one above: no gain was measured, so no constant moved.
