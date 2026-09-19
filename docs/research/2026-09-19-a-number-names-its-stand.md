# A number names its stand: the refusal we never counted, the competitor table we never sourced, and the six figures our own documents told two ways

Date: 2026-09-19.

Files:
`benchmark/longmemeval_score.py`,
`benchmark/longmemeval_judge.py`,
`benchmark/run_longmemeval.py`,
`benchmark/report.md`,
`tests/test_a_refusal_with_the_evidence_in_hand_is_a_measured_error.py`,
`tests/test_quality_guards.py`,
`docs/COMPARISON-2026-09-06.md`,
`docs/COMPARISON-2026-09-07.md`,
`docs/COMPARISON-2026-09-08.md`,
`docs/COMPARISON-2026-09-13.md`,
`docs/COMPARISON-2026-09-13-memory.md`,
`docs/METRICS-2026-09-06.md`,
`docs/PLAN-to-beat-them-2026-09-07.md`,
`docs/REPORT-2026-09-12-what-works-now.md`,
`docs/research/2026-08-27-number-one-memory-market-research.md`.

One sentence: three separate ways a figure of ours stopped saying what it
measured — a refusal that the scorer could only report as a wrong answer, a
competitor table whose column header contradicted its own sources, and six
quantities each told two or three ways across dated documents that never named
the run they came from.

---

## 1. A refusal with the evidence in hand is a measured error

### What was found

On the recorded run of 2026-09-18, 26 of our 75 losses on the 470 answerable
questions are refusals — the system declined a question that had an answer.
Thirteen of those 26 had the gold string in the prompt word for word
(`/home/user/.claude/jobs/80be9db9/tmp/WHY-WE-LAG-2026-09-19.md`, sections 2
and 6, read against `cache/benchmarks/full-2026-09-18/`).

The scorer could not say so. `benchmark/longmemeval_judge.py::needs_judging`
returns `False` for any row whose `status` is not `answered`, so a refusal is
never sent to the judge. `_graded` then falls through to the deterministic
substring test, which scores an empty or refusing hypothesis zero. The verdict
is arithmetically right — a refusal on an answerable question *is* a loss — but
the report has no field anywhere that says how many of its losses were
refusals, or whether those refusals had the evidence in front of them.

`longmemeval_coverage.failure_split` cannot see them either: it counts rows with
`judge_correct is False`, and a refusal never has a judge verdict at all. So a
refusal falls out of the numerator of `judge_accuracy` as a zero and out of
`failure_split` entirely.

The opposite direction was equally invisible. The set carries 30 `_abs`
questions where silence is the right answer; we are silent on 26 of them. No
published competitor measures that at all, and our own report only reported it
folded into `judge_accuracy` through `_scored_abstention`, with nothing naming
it.

`longmemeval_score._abstention_split` exists and is per-category, but it asks a
different question: it splits refusals by whether a *labelled answer session*
reached the retrieved candidates (`answer_sessions_retrieved`). That is a fact
about retrieval, one stage earlier than the prompt the reader actually saw, and
it does not separate the answerable questions from the `_abs` ones at all.

### Decision

Add one run-level section, `abstention_calibration`, computed in
`longmemeval_score.py` and reported by both writers — `run_longmemeval.py`'s
report and `longmemeval_judge.py`'s report — because the judge pass rewrites the
numbers beside it and the two must not disagree. It depends only on `status` and
the recorded prompt-evidence fields, never on a judge verdict, so it reads the
same before and after the judge runs. Both directions, each figure printed as
`seen/denominator` and named:

* `refused` of `answerable` — refusals on questions that have an answer;
* `refused_with_evidence_in_prompt` of `refused_evidence_measured` — of those,
  how many had every dataset-labelled evidence turn in the prompt. A loss we
  could win.
* `refused_with_gold_text_in_prompt` of `refused_gold_text_applicable` — the
  stricter floor: the gold string was there verbatim. Reported only over rows
  whose gold is a span somebody said, because a rubric gold and an abstention's
  explanation were written by the dataset's authors and no prompt can contain
  them (`has_verbatim_gold`).
* `refused_when_silence_expected` of `silence_expected` — correct refusals on
  the `_abs` questions. A win nobody else measures.
* `answered_when_silence_expected` of `silence_expected` — the same decision
  failing the other way.

Rejected: sending refusals to the judge. A refusal does not state the gold
fact, so the judge's answer is known before it is asked; it would cost a
provider call per refusal and would not change the verdict. What was missing was
never the verdict, it was the breakdown.

Rejected: folding this into the per-category rows. The two denominators here
(`answerable` and `silence_expected`) are disjoint sets of questions, and
`abstention` is already its own category — a per-category split would print
`silence_expected` as zero in six rows out of seven.

---

## 2. `benchmark/report.md` published a competitor table nothing supports

### What was found

The table under "Context only: published results on different corpora" put four
competitor rows under a `Recall@5` column:

| System | Recall@5 | MRR | Latency p50 |
|---|---|---|---|
| agentmemory (hybrid) | 95.2% | 88.2% | 14ms |
| agentmemory (BM25 fallback) | 86.2% | 71.5% | <1ms |
| Zep | 94.7% (LoCoMo) | n/a | 155ms |
| Mem0 | 91.6% (LoCoMo) | n/a | 880ms |

Three defects, each checked:

1. **Zep 94.7% and Mem0 91.6% are not retrieval recall.** Our own
   `docs/research/2026-08-27-number-one-memory-market-research.md` records them
   as LoCoMo *answer accuracy* claims: "Mem0 … заявляет 93.4% LongMemEval и
   91.6% LoCoMo", "Zep / Graphiti … заявляет 94.7% LoCoMo". Putting an
   end-to-end question-answering score in a column headed `Recall@5`, beside
   our own BM25 retrieval recall, invites exactly the comparison the line below
   the table says is not being made.
2. **Neither number survives its source.** Fetched 2026-09-19:
   `https://mem0.ai/blog/state-of-ai-agent-memory-2026` reports Mem0 LoCoMo
   92.5 and LongMemEval 94.4, and Zep LoCoMo 80.32% — neither 91.6% nor 94.7%
   appears on the page. `https://blog.getzep.com/state-of-the-art-agent-memory/`
   reports 94.8% on **DMR** (Deep Memory Retrieval) and does not mention LoCoMo
   at all. So 94.7% is most likely Zep's DMR figure copied to the wrong dataset
   at some point before 2026-08-27, and 91.6% traces to no page we can read
   today.
3. **The agentmemory rows are real, and the row mixed two corpora.** Fetched
   2026-09-19, `https://github.com/rohitg00/agentmemory` publishes R@5 95.2%,
   R@10 98.6%, MRR 88.2% for the hybrid path and 86.2% / 94.6% / 71.5% for the
   BM25 fallback — on **LongMemEval-S**, not on our generated corpus. The 14 ms
   latency in the same row is from a different corpus entirely
   (`coding-agent-life-v1`, 15 sessions). Our own
   `docs/research/2026-09-08-the-road-to-one.md` already links that benchmark
   file; `report.md` never did.

The table also sits under a gate that no longer runs. `benchmark/run_benchmark.py`
has said since 2026-09-10 that the legacy-60 and current-generated corpora
"measured BM25 over the pages this repository used to ship. Since 2026-09-10 the
repository ships no memory, so those gates have nothing to measure and are
retired."

### Decision

* **Remove the Zep and Mem0 rows.** Two numbers whose own vendors' pages
  contradict them, on a dataset we have never run, under a metric they never
  measured. There is nothing to repair; the honest form of that row is its
  absence.
* **Keep the agentmemory rows, sourced and split by corpus** — they are real,
  published, and linkable — and give every row its own `Metric`, `Dataset` and
  `Source` cell rather than a shared column header that one row contradicts.
  MRR is written as a ratio (0.882), the unit our own MRR uses; the source
  writes it as a percentage.
* **Say at the top that the run is retired**, with the date and the reason, so
  the numbers below are read as the last state of a retired gate and not as a
  current claim.
* **Hold the line with a test.** `tests/test_quality_guards.py` gains
  `test_every_borrowed_number_says_where_it_came_from`: every non-LLM-Wiki row
  of that table must carry a non-empty metric, a non-empty dataset and a source
  that is a URL or a repository path, and the file must carry the retirement
  notice. A number without a source cannot come back.
* **Correct the market research note in place** with a dated line: its Zep
  "94.7% LoCoMo" is contradicted by Zep's own blog. The note already carries one
  such correction ("cited here 2026-08-27 under the wrong title … corrected
  2026-08-28 after the paper was actually fetched"), so this follows its own
  established form.

---

## 3. Six quantities our documents told two or three ways

Every figure below was chased to a JSON artefact. The finding is milder than
the audit's wording and worse in one place than it looked: almost none of these
are wrong numbers. They are **correct measurements of different runs, in
documents that never named the run**. Two are genuinely unsourceable.

### 3.1 Code parity, 13 tasks: "11 – 9" against "10 – 10"

Both are real, a day apart, and both files are tracked:

| claim | document | artefact | what the artefact says |
|---|---|---|---|
| we 11, they 9 | `COMPARISON-2026-09-06.md`, `COMPARISON-2026-09-07.md`, `METRICS-2026-09-06.md` | `benchmark/code-parity-2026-09-06.json` | llm_wiki 11/13, cbm 9/13, tokens 8 113 / 2 128, seconds 149 / 29 |
| we 10, they 10 | `COMPARISON-2026-09-08.md`, `PLAN-to-beat-them-2026-09-07.md` | `benchmark/code-parity-2026-09-07.json` | llm_wiki 10/13, best 10/13, cbm 10/13, tokens 7 450 / 5 835 / 3 380, serena 9/13, trace_mcp 8/13 |

`COMPARISON-2026-09-06.md` names its file; the others do not. Not a
contradiction — an unnamed run.

### 3.2 Code parity, 16 tasks, same day 2026-09-12: "13 and 14 against 15" against "16 and 16 against 14"

Five families of artefacts carry that date, and they differ in two ways at once:

| family | cbm_project | us | our best | cbm | cbm tokens |
|---|---|---:|---:|---:|---:|
| `-run{1,2,3}` | `…-tasks` | 13/16 | 14/16 | 15/16 | 5 565 |
| `-after-run{1,2,3}` | `…-tasks` | 16/16 | 16/16 | 15/16 | 5 632 |
| `-final-run{1,2,3}` | `…-tasks` | 16/16 | 16/16 | 15/16 | 5 632 |
| `-vault-run{1,2,3}` | `home-user-llm-wiki` | 16/16 | 16/16 | 14/16 | 10 863 |
| `-vault-fast-run{1,2,3}` | `home-user-llm-wiki` | 16/16 | 16/16 | 15/16 | 10 638 |

So our 13 → 16 is our own code changing during that day (`run` → `after` →
`final`), and the competitor's 15 → 14 is which project index it was pointed at
plus its own run-to-run variance, which `benchmark/aggregate_code_parity.py`
has documented since 2026-08-29: "codebase-memory-mcp was measured
non-deterministic on an unchanged repository". `REPORT-2026-09-12-what-works-now.md`
reports the `-run` family (13 and 14 against 15, tokens 9 869 and 7 685 against
5 565 — exact); `COMPARISON-2026-09-13.md` reports 16 and 16 against 14. The
conclusion flips because the runs differ, not because one of them lied.

### 3.3 `COMPARISON-2026-09-13.md` cites artefacts that cannot contain its table

That document's five-side table names
`benchmark/code-parity-v2-2026-09-12-*.json` as its artefacts. **Not one file in
that glob carries a `serena` or a `trace_mcp` side** — they hold three sides
only. Its cbm total of 10 641 tokens matches none of the fifteen files
(10 863 and 10 638 are the two vault values). Its paired ratio, "4 186 against
9 785 on 14 tasks, 0.43×", appears in no surviving artefact.

Nothing dated 2026-09-13 survives under `benchmark/` or `cache/benchmarks/`.
The run that document reports is gone, exactly like the 0.770 LongMemEval figure
of the same date.

The nearest surviving measurement of the same 16-task stand is three consecutive
runs of 2026-09-14, in `cache/benchmarks/full-2026-09-14/parity{1,2,3}.json`
with `parity_aggregate.log`: llm_wiki 16/16, llm_wiki_best 16/16, cbm 15/16,
serena 12/16, trace_mcp 9/16, and paired tokens **5 895 against 10 588 on 15
tasks — 0.56×**. That is the figure to publish; 0.43× is superseded.

### 3.4 Tokens per question: 12 222 against 12 099 against 13 400

| figure | document | artefact | verdict |
|---|---|---|---|
| 12 222 | `COMPARISON-2026-09-07.md`, `MEASUREMENT-2026-09-07.md` | `benchmark/longmemeval-fixed-n200-r{1,2,3}.json` and `-after-n200-r{1,2,3}.json`, `mean_est_total_prompt_tokens` = 12 221.6061 in all six | correct |
| 12 099 | `PLAN-to-beat-them-2026-09-07.md` headline table | `OPTIONS-2026-09-07.md` line 91: it is a row of the **answer-budget sweep at n=50**, "122 880 → 0,6983, 12 099 ток." | wrong table: a sweep cell quoted as the run's figure |
| 13 400 | `COMPARISON-2026-09-08.md` | `cache/benchmarks/longmemeval/second-look-n200-seed101-r1.report.json`, `mean_est_total_prompt_tokens` = 13 542.76 (three-run mean 13 550) | right run, number rounded to something the artefact does not say |

### 3.5 Warm search: 2.5 s against 7–8 s

Not a contradiction: two different legs, one of which lost its label.
`METRICS-2026-09-06.md` section 5 measures both — "тёплый запрос без реранкера
2,5 с", "тёплый запрос с реранкером … 12 с", and the three individual queries
after the batching fix at "7,8 / 8,1 / 8,6 с". `COMPARISON-2026-09-07.md`
carries both rows and labels them. `COMPARISON-2026-09-08.md` carries one row,
"Поиск, тёплый | 7–8 с", with the leg dropped. The fix is the label, not the
number. Neither belongs to the LongMemEval runs, whose own
`mean_retrieve_seconds` is 9.9–10.9 s on the n=200 family and 0.13–3.30 s on the
`second-look` family.

### 3.6 Seconds per answer: 88 / 89.5 / 65 / 57.1

All four are real, and three of them name no run:

* **88** — `COMPARISON-2026-09-07.md`; `longmemeval-fixed-n200-r1.json`,
  `mean_total_seconds` = 88.4371.
* **89.5** — `MEASUREMENT-2026-09-07.md`; the mean of the `after` family,
  (91.7059 + 88.5288 + 88.373) / 3 = 89.54. The same document's before/after
  pair is internally exact: 105.2 s and 29 627 tokens are
  `longmemeval-grounding-n200-r1.json` to four figures.
* **65** — `COMPARISON-2026-09-08.md`; `second-look-n200-seed101-r1.report.json`,
  `mean_total_seconds` = 65.3559.
* **57.1** — `COMPARISON-2026-09-13-memory.md`; the 2026-09-13 run, whose
  artefacts are gone.

### 3.7 Two per-type tables 6–11 points apart

Also two runs, and this one is fully recomputable:

`COMPARISON-2026-09-08.md`'s column is `second-look-n200-seed101-r1.judge-report.json`
exactly — assistant 0.9091, user 0.8462, knowledge-update 0.7241, temporal
0.7255, multi-session 0.6458, preference 0.6667, overall 0.75. Its "верных среди
отвеченных 0,874" recomputes from `second-look-n200-seed101-r1.judged.jsonl` as
139 correct of 159 answered = 0.8742, and its "верных отказов 11/12" is the
abstention row.

`COMPARISON-2026-09-07.md`'s column is the `fixed` family of the day before,
averaged over three runs and taken among the answered, whose overall 0.698 is
the mean of 0.715, 0.68 and 0.70. Different run, different denominator, both
labelled "верных среди отвеченного".

### 3.8 29 035 against 29 627 tokens

`METRICS-2026-09-06.md` reports 29 035 tokens, 9.8 s retrieve, 35.7 s answer.
The nearest surviving artefact, `benchmark/longmemeval-grounding-n200-r1.json`,
reads 29 627.0459, 9.9596 and 36.4502. No run file matching 29 035 is on disk
under `benchmark/` or `cache/benchmarks/`; the per-question JSONL of that run
was never kept.

### 3.9 The LoCoMo failed reproduction is EverMemOS, not Mem0

`docs/research/2026-09-06-what-locomo-is-worth-measuring-on.md` says it plainly:
"EverMemOS заявляет 92,32% при потолке 93,57%, а сторонняя попытка воспроизвести
дала 38,38%." `docs/COMPARISON-2026-09-07.md` section 4 puts the same sentence
immediately after "Mem0 заявляет 92,5, то есть впритык к потолку", so it reads as
Mem0's failed reproduction. Attributing another vendor's failure to the wrong
vendor is the single fastest way to make a whole table untrustworthy.

### Decision

Correct in place what is an error; label in place what is an unnamed run; and
where the artefact is gone, say so in the document instead of leaving a number
nobody can check. Concretely:

1. `COMPARISON-2026-09-07.md` — attribute 92.32 → 38.38 to EverMemOS.
2. `COMPARISON-2026-09-07.md`, `COMPARISON-2026-09-08.md`,
   `PLAN-to-beat-them-2026-09-07.md`, `METRICS-2026-09-06.md`,
   `REPORT-2026-09-12-what-works-now.md` — each gains a dated provenance block
   naming its artefact, and the figures above that the artefact does not support
   are corrected or marked.
3. `COMPARISON-2026-09-13.md` and `COMPARISON-2026-09-13-memory.md` — each gains
   a dated block saying the run's artefacts are not on disk, that the cited glob
   cannot hold the table, and which surviving measurement supersedes it.
4. No dated document is rewritten to hide what it said. The correction is added
   beside the number, with today's date, the way `2026-08-27-number-one-memory-market-research.md`
   already corrects its own citation.

---

## Sources

* `https://mem0.ai/blog/state-of-ai-agent-memory-2026` — fetched 2026-09-19:
  Mem0 LoCoMo 92.5, LongMemEval 94.4; Zep LoCoMo 80.32%, LongMemEval 71.2
  (GPT-4o). Neither 91.6% nor 94.7% is on the page.
* `https://blog.getzep.com/state-of-the-art-agent-memory/` — fetched 2026-09-19:
  94.8% on Deep Memory Retrieval; LongMemEval improvements "up to 18.5%"; LoCoMo
  not mentioned.
* `https://github.com/rohitg00/agentmemory` — fetched 2026-09-19: LongMemEval-S
  R@5 95.2%, R@10 98.6%, MRR 88.2%; BM25 fallback 86.2% / 94.6% / 71.5%;
  `coding-agent-life-v1` hybrid latency 14 ms.
* `benchmark/code-parity-2026-09-06.json`, `benchmark/code-parity-2026-09-07.json`,
  `benchmark/code-parity-v2-2026-09-12-*.json` (fifteen files),
  `benchmark/longmemeval-{fixed,after,grounding}-n200-r*.json` — read directly.
* `cache/benchmarks/full-2026-09-14/parity{1,2,3}.json` and
  `parity_aggregate.log`; `cache/benchmarks/longmemeval/second-look-n200-seed101-r{1,2,3}.*`
  — read directly; both live in the disposable cache, not in git.
* `benchmark/run_benchmark.py` module header — the BM25 gate retirement of
  2026-09-10.
* `docs/research/2026-08-27-number-one-memory-market-research.md`,
  `docs/research/2026-09-06-what-locomo-is-worth-measuring-on.md`,
  `docs/research/2026-09-08-the-road-to-one.md`.
