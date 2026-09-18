# A rubric is not a miss

Dated 2026-09-18. The research before changing what the LongMemEval stand reports
about evidence. File: `docs/research/2026-09-18-a-rubric-is-not-a-miss.md`.

Files: `benchmark/longmemeval_score.py`, `benchmark/longmemeval_judge.py`,
`benchmark/longmemeval_coverage.py`, `benchmark/longmemeval_vault.py`,
`benchmark/run_longmemeval.py`,
`tests/test_a_rubric_gold_is_never_scored_as_a_miss.py`,
`tests/test_the_report_says_how_much_evidence_reached_the_reader.py`.

## What was found

Measured over the recorded run `cache/benchmarks/full-2026-09-17/lme500.jsonl`
(500 questions) and its `lme500.judged.jsonl`, by question type:

| type | `gold_in_prompt` | judge said yes |
|---|---|---|
| knowledge-update | 60/78 | 65 of 67 graded |
| multi-session | 58/133 | 76 of 95 graded |
| single-session-assistant | 30/56 | 38 of 41 graded |
| single-session-preference | **0/30** | 3 of 3 graded |
| single-session-user | 57/70 | 34 of 34 graded |
| temporal-reasoning | 49/133 | 85 of 100 graded |
| overall | 254/500 = 0.508 | |

`gold_in_prompt` asks whether the gold answer's own text appears in the prompt
the reader received. Three separate things make that question unanswerable, and
in each case the stand answers it with `False` — a miss.

**One. A gold that is a rubric.** LongMemEval's own grader reads the preference
gold as a rubric, not as a fact. The authors' `evaluate_qa.py` template for that
type begins: "I will give you a question, a rubric for desired personalized
response, and a response from a model … The model does not need to reflect all
the points in the rubric" (https://github.com/xiaowu0162/LongMemEval,
`src/evaluation/evaluate_qa.py`, fetched 2026-09-18; the same text is already
copied character for character into `benchmark/longmemeval_official.py`). One
such gold on this run reads "The user would prefer responses that suggest
resources specifically tailored to Adobe Premiere Pro, especially those that
delve into its advanced settings". No session contains that sentence, because
nobody said it — the dataset's authors wrote it. `0/30` is not a measurement of
retrieval; it is the metric meeting a gold of a shape it cannot read.

**Two. A gold that is derived.** Of the rows the judge called right, 101 have
`gold_in_prompt` false: 46 temporal-reasoning, 45 multi-session, 10
knowledge-update. Their golds are computed or restated, not quoted — "6 days.",
"3.5 weeks", "Three times a week." The evidence was in the prompt; the answer had
to be worked out from it. Nothing in the sessions ever says "6 days".

**Three. A judge that never spoke, filled in by the same substring test.**
160 of the 500 rows carry `judge_correct: null` — the CLI answered with prose
instead of a verdict (`docs/research/2026-09-17-the-task-is-named-to-the-model.md`).
`longmemeval_judge._row_verdict` falls back to `longmemeval_score.contains_answer`
for those rows. For a rubric gold that fallback can only return false, so
`lme500.judge-report.json` reports `single-session-preference` at
`{"n": 30, "judge_accuracy": 0.1}` — three correct out of thirty — when the judge
graded three rows and called all three right, and said nothing at all about the
other twenty-seven. The same fallback is what makes `longmemeval_score`'s
`accuracy` read `0.0` for that type in `lme500.log`.

**And two numbers for one word.** The report writes `coverage` (four figures per
type per depth: `session_recall` 0.955, `all_sessions` 0.912, `turn_recall`
0.8925, `all_turns` 0.802 overall), measured over what *retrieval returned*.
Separately each row carries `evidence_requested` / `evidence_missed`, measured
over what the *compiler could place* — 9550 requested, 0 missed, a flat 1.0.
`_print_summary` prints none of them, so a reader quoting "coverage" is picking
one of five unlabelled numbers taken at three different stages of the pipeline.
`failure_split` compounds it: the report is written before the judge runs, so it
reads `{"wrong": 0, "evidence_in_hand": 0, "evidence_missing": 0}` for a run with
199 wrong answers — silence rendered as a clean bill.

## Decision

1. **A metric that is undefined reports nothing, never zero.** Where the gold is
   a rubric, `em`, `contains`, `f1`, `correct` and the gold-text prompt signal
   are `None`, and the per-type report carries the count of rows the metric
   applied to beside every figure it computed. The category definition lives in
   one place, `longmemeval_score.RUBRIC_CATEGORIES`, and the judge imports it
   instead of keeping its own copy.
2. **The judge's silence stays silent.** `_row_verdict` may substitute the
   deterministic score only where that score means something; on a rubric row it
   returns no verdict, and the per-category row says how many were ungraded.
3. **The measurement that works for every type is the dataset's own evidence.**
   LongMemEval flags the turns that carry the answer (`has_answer`) and names the
   sessions (`answer_session_ids`). Whether those turns reached the prompt is
   defined for a rubric gold and for a derived gold alike, so the run records
   `evidence_in_prompt` beside the old signal. The old signal keeps its meaning
   under a name that states it: `gold_text_in_prompt` — the gold string appeared
   verbatim — reported only over the rows where a verbatim gold exists.
4. **Every evidence figure is named by the stage it measures and printed.**
   `retrieval_coverage` (what search returned), `prompt_evidence` (what the
   reader saw), `compile_evidence` (what the compiler could place). The summary
   table prints the prompt-stage figures with their denominators, so no one has
   to pick a number out of the JSON by hand. `failure_split` says when no judge
   verdict exists instead of reporting zero wrong answers.

Two more instances of the same defect surfaced while re-scoring the recorded run
with the fixed code, and are decided the same way:

5. **An abstention's gold is an explanation, not a span.** It read 0 of 30 for
   exactly the reason the rubric did — the authors' abstention template hands
   the judge "an unanswerable question, an explanation, and a response from a
   model" (`longmemeval_official.ABSTENTION`). It leaves the gold-text
   denominator too.
6. **`judge_accuracy` was never only the judge.** Where no readable verdict came
   back — 160 of 500 rows — `contains_answer` stood in, unannounced, and the
   blend was published as one number. The blend stays, because a text score is
   real evidence where the gold is a value somebody said, but each per-category
   row now carries `judged` and `from_text_score`. On the recorded run the
   overall 473 graded rows are 340 judge verdicts and 133 substring tests, and
   the `abstention` row's 0.9 contains no judge verdict at all — that category
   is never sent to the judge.

## What the recompute showed

Re-scored over `lme500.jsonl` and `lme500.judged.jsonl` with no new run:

| category | accuracy before → after | gold text in prompt | judged before → after |
|---|---|---|---|
| abstention | 0.9 → 0.9 /30 | n/a (30 held out) | 0.9 /30 → 0.9 /30, 0 by judge |
| knowledge-update | 0.8472 → 0.8472 /72 | 60/72 = 0.8333 | 0.9583 /72 → 0.9583 /72 |
| multi-session | 0.6529 → 0.6529 /121 | 58/121 = 0.4793 | 0.7355 /121 → 0.7355 /121 |
| single-session-assistant | 0.6429 → 0.6429 /56 | 30/56 = 0.5357 | 0.8571 /56 → 0.8571 /56 |
| single-session-preference | **0.0 → none** (30 held out) | **n/a (30 held out)** | **0.1 /30 → 1.0 /3**, 27 ungraded |
| single-session-user | 0.8594 → 0.8594 /64 | 57/64 = 0.8906 | 0.8906 /64 → 0.8906 /64 |
| temporal-reasoning | 0.4094 → 0.4094 /127 | 49/127 = 0.3858 | 0.7087 /127 → 0.7087 /127 |
| overall | 0.62 → **0.6596 over 470** | 254/440 = 0.5773, 60 held out | 0.766 /500 → **0.8097 over 473**, 27 ungraded |

The old headline `gold_in_prompt` 254/500 = 0.508 becomes 254/440 = 0.5773 with
sixty questions named as out of scope instead of counted as misses, and it is
labelled as the floor it is. `failure_split` on the judged rows reads
`{judged: 340, wrong: 39, evidence_in_hand: 20, evidence_missing: 19}` where the
published report said `{wrong: 0}`.

Not decided here, and left to the owner: re-running the 500 questions. The
recorded run is re-scored from its own rows; `evidence_in_prompt` is recorded by
runs from this change onward, because the prompt text was never kept on the row.

## Source

- LongMemEval, `src/evaluation/evaluate_qa.py`, preference template, fetched
  2026-09-18 via raw.githubusercontent.com — quoted above.
- This vault's own recorded run `cache/benchmarks/full-2026-09-17/`
  (`lme500.jsonl`, `lme500.judged.jsonl`, `lme500.report.json`,
  `lme500.judge-report.json`), read 2026-09-18.
- `docs/research/2026-09-01-a-category-graded-by-the-wrong-question.md` — the
  judge prompt for this category was fixed then; the text metrics were not.
- `docs/research/2026-09-17-the-task-is-named-to-the-model.md` — the 160
  unreadable verdicts.
