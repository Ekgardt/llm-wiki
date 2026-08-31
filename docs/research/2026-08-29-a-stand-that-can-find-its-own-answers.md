# A stand whose answer sheet lives inside the index it grades

*Dated research note, 2026-08-29. Written for rule 2 before changing how the two
vault stands decide what may be scored. What follows is current practice as of
this date, what this vault actually does, and which shape was taken and why.*

## The question

`benchmark/vault-retrieval-v1.json` and `benchmark/vault-application-v1.json`
hold every question, every gold page, and — for the application stand — every
token an answer must contain. `benchmark` is an approved corpus root
(`scripts/corpus_snapshot.py::APPROVED_CODE_ROOTS`), `.json` is an admitted
suffix, and both files are members of the active generation's source manifest.
The stands grade retrieval over an index that contains their answer sheets.

The commit that raised retrieval quality on this vault
(`75f842d`, 2026-08-29) named this and left it open: the retrieval sheet "was in
the top five in nine of ten cases before the fix."

## What the field calls this, and what it recommends

**Search-time data contamination** is the exact name. *Search-Time Data
Contamination* (arXiv 2508.13180) studies search-based LLM agents whose
retrieval step "surfaces a source containing the test question (or a
near-duplicate) alongside its answer, enabling agents to copy rather than
genuinely infer." They measured roughly 3% of questions across Humanity's Last
Exam, SimpleQA and GPQA as directly discoverable — agents named HuggingFace in
their reasoning chains — and when HuggingFace was blocked as a source, accuracy
on the contaminated questions fell about 15 points.

Three things in that work carry over here directly:

1. **The mitigation is a source blocklist at retrieval time**, not a rewritten
   benchmark. They blocked the host that served the key.
2. **Both numbers get reported.** The contaminated and the decontaminated score
   are published side by side; the drop *is* the finding.
3. **The logs are released** so the reader can audit which retrievals saw what,
   rather than trusting a summary.

*Generating Leakage-Free Benchmarks for Robust RAG Evaluation* (arXiv
2605.08838) adds the reporting discipline: it defines a **leakage error** — the
fraction of instances answerable without the retrieved context — and reports it
next to the accuracy, on the grounds that an accuracy without a leakage number
is not interpretable. Its own remedy is regeneration (semi-synthetic entity
replacement, renewable instances), which is a good answer for a public
benchmark aging against model training and a poor one here: these ten questions
are valuable *because* they are this vault's real cross-language case, and
synthesising them away would measure a different vault.

The broader contamination literature (arXiv 2502.17521, 2502.14425) offers
canary strings and encrypted or held-out label files. Canaries answer a
different question — did a *training* corpus swallow the benchmark — and they
are a detector, not a defence. Held-out labels, kept out of the graded tree
entirely, are the strongest shape and the one a leaderboard uses.

## What this vault actually does today

Measured, not assumed:

- Both sheets are in the active generation's `source-manifest.json`, along with
  `tests/test_intent_conditional_trust.py`, which pins one case's question next
  to that case's gold page.
- Each stand named **one** path in a module constant. The constants had gone
  stale in both directions: `run_vault_application` inherited
  `run_vault_retrieval`'s constant and so dropped the *retrieval* sheet from its
  ranking while leaving its own in, and neither stand knew about the test file.
- The `grep` baseline reads `*.md` under `knowledge/notes`, `knowledge/daily`
  and `docs` only. No sheet is reachable from there — a JSON under `benchmark/`
  and a Python file under `tests/` are outside both the roots and the suffix. So
  the baseline is uncontaminated by construction, and the empirical
  confirmation is that `grep_applied_at_5` reads 0.1429 while the application
  sheet alone satisfies 7 of 7 cases: had `grep` been able to see it, the
  baseline would be 1.0.

**The two directions are opposite, and this is the part that was never said.**

- For the retrieval stand a hit requires the gold path itself. A retrieved sheet
  can only take a slot. Excluding it can *raise* `hit@k` and can never lower it.
- For the application stand the sheet carries every expected token verbatim, so
  retrieving it would pass a case with no gold page at all. Excluding it can
  *lower* `applied@5` and can never raise it.

One indirect channel was checked rather than assumed, because it would have
broken the argument: the graph leg expands neighbours, so a retrieved sheet that
linked to its gold pages could pull them in and manufacture a hit. It does not.
In the active generation the two sheets contribute four nodes between them —
their own file and module definitions — and zero `dependency` rows. JSON is not
markdown, so no wikilink is extracted from a `gold_path` string.

Which makes the historical question answerable by inspection rather than by
re-running old code: the application stand excluded its own sheet **from the
scored text** from its first commit (`2affa04`, 2026-08-25), and the retrieval
sheet — the only other sheet its text could have read — satisfies 0 of 7 cases.
So no `applied@5` this vault ever recorded was passed by reading an answer
sheet. And no `hit@k` could be, by the shape of the metric. The exposure was
real and the sheet was genuinely retrieved; what it did was steal a place, which
pushes a number down.

The timeline, for anyone rereading an old number:

| date | commit | state |
|---|---|---|
| 2026-08-24 | `78c4ce0` | retrieval stand created with **no** self-exclusion at all |
| 2026-08-24 | `526eebc` | its own sheet dropped from its ranking, by literal |
| 2026-08-25 | `2affa04` | application stand created; its sheet excluded from the scored text, never from the ranking |
| 2026-08-25 | `b6925e5` | `tests/test_intent_conditional_trust.py` appears, pinning `dead-task-restore`'s question beside its gold page; neither stand ever knew |
| 2026-08-29 | `75f842d` | the dense-admission weight pushes the sheets down as a side effect |
| 2026-08-29 | this change | derived set, both stands, ranking and text |

So: **no `hit@k` or `applied@5` this vault has recorded was inflated by the
answer key, at any date.** The only date on which a recorded number could have
been *deflated* by an unexcluded sheet is 2026-08-24, between `78c4ce0` and
`526eebc` — and after that, on every date, by the two sheets neither stand
excluded from its ranking. Today that deflation is worth 0.1 of `hit@5`. Saying
the history is "partly self-scored" would be the comfortable answer and it would
be wrong; the sheets were in the index, they were retrieved, and they made the
product look slightly worse than it is.

## The shape taken

A **derived retrieval blocklist**, matching the STC mitigation, with both
numbers reported, matching both papers.

- `benchmark/answer_key.py` derives the set from disk each run: a file belongs
  to the answer key when it states a case in the stand's own words — the
  question or task string, verbatim. That is precisely the property that makes
  retrieving it cheating, and deriving it means a new copy joins the set the day
  it appears rather than the day someone remembers. Gold pages are subtracted as
  a floor so a case can never be silenced by its own exclusion.
- Both stands drop the same set, from the ranking and from the scored text.
- The `grep` baseline drops the same set. On this vault that changes nothing,
  and it is there so the two sides stay comparable if a sheet ever moves.
- `benchmark/run_stand_contamination.py` scores one retrieval three ways —
  nothing excluded, the constants as they stood, the derived set — so the drop
  is published rather than assumed, and writes the full observations so a reader
  can recompute without re-running retrieval.

**Rejected: moving the sheets out of the corpus.** It is the stronger shape —
a held-out key cannot be retrieved by any code path, in any future stand, and
needs no exclusion logic to stay correct. Two ways exist and both were rejected
for today. Dropping `benchmark` from `APPROVED_CODE_ROOTS` would blind the
product to 83 real sources and is a corpus-rule change requiring owner sign-off.
Moving the two files under a directory the collector already skips
(`SKIP_DIRECTORIES` holds `gaps` and `raw-sources`) would work with no code
change at all and is worse than the disease: it would hide the reason inside an
unrelated constant, and the next reader would find a benchmark fixture in a
directory named for knowledge gaps. A path change is also an architecture
change under `CLAUDE.md`. The exact diff needed for a clean version — an
explicit ignore glob naming the sheets — is reported to the owner rather than
made, because `scripts/corpus_snapshot.py` is owned by another agent today.

**Rejected: renewable questions.** The leakage-free-RAG remedy. It answers
benchmark aging, which is not the failure here, and it would replace the one
property that makes these ten questions worth asking — that they are the
vault's own cross-language case.

## What it is worth today, measured

One retrieval per question through the CLI entry point, depth 8, scored three
ways off the same ranking. The active generation is
`generation-18d0425dd6fa9f88-7c7e7a5f`; the machine carried three other agents
and a load average of 10–18 throughout, which is why this is a paired
comparison and not a comparison of separate runs.

| | raw | shipped constants | derived set |
|---|---|---|---|
| retrieval `hit@1`, n=10 | 0.4 | 0.4 | 0.4 |
| retrieval `hit@5`, n=10 | **0.7** | 0.8 | **0.8** |
| retrieval `grep_hit@5` | 0.0 | 0.0 | 0.0 |
| application `applied@5`, n=7 | 0.8571 | 0.8571 | 0.8571 |
| application `grep_applied@5` | 0.1429 | 0.1429 | 0.1429 |

The whole of it is one case. `benchmark/vault-retrieval-v1.json` was returned at
**rank 1** for `citation-relevance` and nowhere else in either corpus; the gold
page for that case sits at rank 6 among real documents, so the sheet's slot was
the difference between a miss and a hit at five. Everything else — both sheets
across the other sixteen questions, and
`tests/test_intent_conditional_trust.py` everywhere — never entered depth 8 at
all.

So the honest reading, and it is not the one the question expected:

- **The contamination cost this vault 0.1 of `hit@5` today. It did not buy
  anything.** Removing the answer key raises the number, because the sheet was
  stealing a place rather than filling one.
- **`applied@5` is untouched: 0.8571 under every policy.** No sheet was
  retrieved for any of the seven tasks, and the one sheet that could have
  passed a case by itself has had its text excluded since the stand's first
  commit.
- **The derived set buys nothing over the stale constants today** — 0.8 either
  way. Its value is that it will catch the next copy of a sheet without anyone
  remembering to, and that it covers the application stand's ranking, which the
  constants did not. Claiming a measured gain here would be false.

### The 0.1 is bought by the depth compensation, and that is worth stating

The same measurement repeated at `--depth 5` — the stand's own answer size, the
native 40-row pool, no compensation — gives `hit@5` **0.7** under all three
policies. The sheet still sits at rank 1 for `citation-relevance`; dropping it
without deepening the request leaves four real rows, and the gold page is at
rank 6 among real documents either way.

So there are two true numbers answering two different questions, and the stand
must say which one it reports:

- **0.7** — what an operator actually gets today, from a vault that does
  contain the sheet, asking for five.
- **0.8** — what the vault's retrieval is worth once its own measurement
  artifact is subtracted: had the sheet never been written, the pool would hold
  one more real document and the fifth slot would be real.

The stands report 0.8, because a benchmark exists to measure the product and
not to measure the cost of the benchmark's own file. The uncompensated 0.7 is
one command away (`run_stand_contamination.py --depth 5`) so the compensation
cannot quietly become the score.

### Both stands after the change, end to end

`--path cli`, three rounds for the retrieval stand:

    product_hit_at_1: 0.4   product_hit_at_5: 0.8   grep_hit_at_5: 0.0
    spread across runs: {'min': 0.8, 'max': 0.8}   unstable cases: []
    budget_degraded_cases: []                      gates passed: True

    product_applied_at_5: 0.8571   grep_applied_at_5: 0.1429
    gain_over_grep_at_5: 0.7142    gates passed: True
    missed: clear-capture-counters

## The machine, which is not contamination and must not be reported as if it were

The stands default to `--path mcp`, and the MCP wrapper imposes a ten-second
operation budget. Measured on this machine today, before any change, with three
other agents running and a load average of 17–19: the retrieval stand's median
`hit@5` over three rounds was **0.5** with a spread of 0.4 to 0.8, nine of ten
cases carried `optional_stage_timeout`, and the application stand reported
`applied@5` **0.0** — every case, because the budget returned no rows at all.
The same code on the same generation through the CLI entry point, which has no
deadline, gives 0.8 and 0.8571 with zero spread across three rounds.

None of that is contamination and none of it is the exclusion. It is the
machine, and it is why every number above is a paired comparison scored off one
retrieval rather than a comparison between runs.

Repeated on the same path after the change, once the load had fallen to 7–10,
it recovers to exactly the numbers the CLI path gives:

    --path mcp, 3 rounds:  hit@1 0.4, hit@5 0.8, grep 0.0, gates passed
                           spread {'min': 0.5, 'max': 0.8}
                           unstable: citation-relevance, dead-task-restore,
                                     self-resolving-health
                           budget_degraded: 9 of 10
    --path mcp:            applied@5 0.8571, grep 0.1429, gates passed

Nine of ten cases still lose an optional stage to the budget even at load 7–10,
and three of ten still answer differently between rounds. A single `mcp` run of
either stand on a busy machine is not a measurement of retrieval, and the
`budget_degraded_cases` line is there to say so before anyone quotes the
number.

## The honest limits of the exclusion

- Excluding from the ranking requires asking for a deeper list, or the exclusion
  silently shortens it. The request now goes to `limit + len(sheets)`, and
  `_candidate_pool` grows with the requested limit (40 at 5, 48 at 6, 64 at 8),
  so the compensation is not free and is not perfectly neutral.
  `run_stand_contamination.py --depth 5` reports the uncompensated number beside
  it so the compensation cannot quietly become the score.
- The exclusion is *retrieval-time*. The sheets remain indexed, so any future
  caller that does not consult the set can still find them.
- `applied@5` has a separate weakness this note does not fix and should not
  hide: between 2 and 12 indexed files carry each case's full token set, so a
  case can pass on a page that is not the gold page. That is a property of
  scoring on verbatim tokens, not of the answer key.
- The `api` control path was not measured. It reranks with the cross-encoder and
  did not finish seventeen questions in thirty-six minutes on a machine at load
  10–18, so it was stopped rather than reported. The `warm()` helper alone cost
  351 seconds today, because it deliberately goes through that path first.
- Three other agents were rewriting `scripts/corpus_snapshot.py`,
  `scripts/repository_index.py` and `scripts/search_memory.py` in this working
  tree while these numbers were taken. Each measurement is internally consistent
  — one process, one code state, one generation — but two measurements taken an
  hour apart are not guaranteed to share retrieval code.

## Sources

- [Search-Time Data Contamination](https://arxiv.org/pdf/2508.13180)
- [Generating Leakage-Free Benchmarks for Robust RAG Evaluation](https://arxiv.org/html/2605.08838)
- [Recent Advances in LLM Benchmarks against Data Contamination](https://arxiv.org/html/2502.17521v1)
- [A Survey on Data Contamination for Large Language Models](https://arxiv.org/html/2502.14425v2)
