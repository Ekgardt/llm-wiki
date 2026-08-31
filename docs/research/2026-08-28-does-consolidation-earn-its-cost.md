# Does consolidation earn its cost? A paired measurement (`MEM-13`)

Date: 2026-08-28. Task: `MEM-13` in
`docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md`, section 12. Written before the
stand was built, per rule 2; the measured numbers are appended below under
**Results**.

`MEM-10` (commit `2704125`,
`docs/research/2026-08-28-longmemeval-first-number.md`) scored
**multi-session 0.083, n = 12** — the weakest of the seven categories — and 26
of 50 questions ended in `insufficient_evidence`. Multi-session is exactly what
consolidation is supposed to serve: ten sessions about one thing should become
one answer. So the question is narrow and answerable: **on our data, with our
pipeline, does our consolidation move that number?**

## Which consolidation is on the answer path

The roadmap names `reflection.py`. Traced through the code graph
(`home-user-llm-wiki`, `trace_path` on `consolidate_day`, and callers of both
modules), this vault has two things called consolidation and they are not the
same:

| | `scripts/episode_consolidation.py` | `scripts/reflection.py` |
|---|---|---|
| schedule | nightly (`scheduled_nightly.py`) | weekly (`scheduled_weekly.py` step 5) |
| input | `knowledge/raw/sessions/<day>/*.md` | pages with ≥ 2 `## Update (…)` sections |
| output | one daily-log entry → compile → pages | the page rewritten, old body under `## History` |
| trigger threshold | a day with any session record | `REFLECTION_THRESHOLD = 2` |

**`reflection.py` has never had a candidate in this vault.** Measured on the
live vault, 2026-08-28: of 107 pages under `knowledge/notes/`, **zero** contain
a single `## Update (` section, so `find_reflection_candidates()` returns an
empty list and `reflect_page` has never run on real data. The only writer of
that section is `compile_memory.py:3048`, on an `update` operation against an
existing page; this vault's compile has been creating pages, not updating them.

That is not a defect this task fixes, but it settles what `MEM-13` can honestly
measure. Measuring `reflection.py` against a no-consolidation baseline would be
measuring a function against itself: with no candidates, both arms are the same
vault. The consolidation that actually reaches an answer is
`episode_consolidation`, and that is what this stand measures. The roadmap item
should be re-worded to say so.

## What the roadmap's gate actually is

`MEM-13`'s stated gate — "> 54% single-hop FactConsolidation, the HippoRAG-2
ceiling" — is a different benchmark measuring a different competency.
FactConsolidation sits under **MemoryAgentBench's Selective Forgetting**
competency: numbered facts where a counterfactual arrives with a higher serial
and the system must prefer the newer one, with the rule stated in the prompt.
HippoRAG-v2 reaches 54.0%, BM25 48.0%, Mem0/Contriever 18.0%, Zep/Graphiti
7.0%. That is conflict resolution over time — the subject of `MEM-12`
(selective forgetting) and `MEM-14` (deterministic freshness), not of episodic
consolidation. The name collides; the task does not. This note therefore does
**not** claim that gate, and does not run it. Stating the external comparison
without running it would be exactly the kind of borrowed number
`docs/research/2026-08-23-*` already found not to survive audit.

## What the stand does

`benchmark/consolidation_vault.py` answers one question twice, in one process,
on one vault, over one ingest:

1. the haystack is ingested exactly as `longmemeval_vault` ingests it — session
   evidence plus one transactional daily entry per session;
2. **arm `baseline`** builds a generation over the daily files as they stand and
   answers the question — this is the MEM-10 configuration;
3. `episode_consolidation.consolidate_day` then runs over every day that has
   session records, with the real provider, exactly as the nightly pass runs it;
4. **arm `consolidated`** rebuilds a generation over the daily files as they now
   stand and answers the same question the same way.

The two generations necessarily differ — arm `consolidated`'s corpus is arm
`baseline`'s corpus *plus* the consolidation entries, which is the whole point.
Everything else is held fixed: same vault, same ingested sessions, same builder
configuration and reuse config, same retrieval profile resolution and candidate
count, same answer budget, same provider, same process. Consolidation appends;
it never replaces the raw entries, because that is what the product does.

`benchmark/run_consolidation.py` orchestrates one process per question and
writes both a paired JSONL and two per-arm JSONL files in the exact MEM-10 row
shape, so `longmemeval_judge.py` and `longmemeval_score.aggregate` read them
with no new code.

### Two things the build had to accommodate

**Consolidation writes to *today's* daily file.**
`daily_log_append.append_daily` derives its filename from `datetime.now()` and
ignores the day it is consolidating, so the consolidation of a 2023 haystack day
lands in `knowledge/daily/<today>.md`. Both arms therefore discover their daily
files by scanning the directory rather than by reusing the ingest list —
otherwise arm `consolidated` would index a corpus that does not contain its own
consolidation output and the stand would report a guaranteed null result. This
is the product's real behaviour and is not changed here.

**The stand must be able to tell that it saw anything.** A stand that silently
stopped recognising consolidation output would report "consolidation changes
nothing" with total confidence. Three guards: `marker_is_live()` re-derives a
real block from `episode_consolidation.render_block` and checks the marker still
occurs in it (asserted by a test); `entries_written` counts the markers on disk
after consolidation; and `prompt_consolidation_hits` counts them in the prompt
the answer step actually built — the authoritative "did it reach the model"
number, since a retrieval row carries its text only when the backend supplied
one.

### Why the statistic is paired

This vault has a recorded lesson (log, 2026-08-26) that its ten-question stands
wander by one case between runs of identical code, because the optional
retrieval legs are deadline-bounded and drop out under load. Two independent
accuracies at n ≈ 12 would report that wander. Pairing removes every
per-question difficulty term, and the only evidence that remains is the
discordant pairs — questions one arm got right and the other got wrong.
`consolidation_score.mcnemar_exact` is the exact two-sided binomial over exactly
those, so a reported difference either survives the coin-flip null or is named
as noise. A provider failure on either arm drops the pair rather than scoring
it, for the reason MEM-10 gives: nothing was produced to compare.

## What current practice says to expect

Two 2026 results bear directly on the shape of this vault's consolidation.

*Beyond Static Summarization: Proactive Memory Extraction for LLM Agents*
(arXiv 2601.04463) names the two properties `episode_consolidation` has by
construction. It is **ahead-of-time**: the model summarises before knowing what
will be asked, "a blind feed-forward process that discards potentially important
details". And it is **one-off**: extraction happens once, with no feedback loop.
Their ablation puts numbers on the cost: one-pass extraction alone reaches
92.64% memory accuracy but only 54.03% integrity and 50.60% QA accuracy, against
73.80% / 88.12% / 62.26% for the full method — "a conservative strategy that
ensures correctness but misses many details". Our consolidation is precisely
that conservative one-pass: every item must quote a line that really occurs in
the day's records, or it is dropped. High precision, low recall, by design.

*WhenLoss: Diagnosing Write and Retrieval Bottlenecks in Long-Context Memory
Systems* (arXiv 2605.24579) makes the general point this stand is built around:
write-side loss and retrieval-side failure are different bottlenecks, and which
one dominates depends on the configuration — so it must be measured, not
assumed.

One asymmetry in our favour and one against. In our favour: we append rather
than replace, so consolidation cannot lose anything the baseline had; the
failure mode available to us is *no effect* or *dilution*, not loss. Against us:
`consolidate_day` never crosses a day boundary. A multi-session question whose
evidence is spread over several days — the median LongMemEval multi-session
question spans 11 distinct days — cannot be answered by any single consolidated
item this design can produce, because no prompt ever sees two of those days at
once. That is a structural prediction, and the measurement below tests it.

## Two controls, run before the numbers were believed

A stand that reports "no difference" has to prove it was capable of seeing one.
Two probes, both against the real `episode_consolidation` call path the stand
uses, on 2026-08-28:

**The call path works.** A throwaway vault seeded with one real session record
copied out of this vault (`knowledge/raw/sessions/2026-08-20/`, 163,687 bytes,
one record): **one provider call, 8 durable items, 1 daily entry written**, each
item carrying a verbatim quote and a session source. So when the stand reports
zero items, that is not a broken consolidator.

**Where a LongMemEval day yields nothing, it is the model's own empty array, not
our quote validation dropping everything.** For question `60036106`, day
`2023-05-20`, 4 records, a 39,234-character prompt, the provider returned
literally `'[]'` and `grounded_lessons` kept 0 of 0. The two causes are
indistinguishable in the item count and mean opposite things, so this had to be
checked rather than assumed. The consolidation prompt opens "You read a day of
software work sessions" and instructs the model to skip "routine work, status
chatter"; a day of personal chat about commuting is, by that instruction,
correctly empty.

This is **not** uniform across the dataset — the run below records days that
produced 3 and 4 items — so "consolidation emits nothing on LongMemEval" would
have been an overclaim. What varies is how much of a chat day the prompt
recognises as durable.

## Results

**n = 24 attempted, 23 graded pairs.** LongMemEval `multi-session` slice, seed
13, concurrency 2, provider `claude` with a 240 s per-call ceiling. Started
13:38 UTC 2026-08-28, last row at 3,026 s. One pair dropped: `c4a1ceb8`, whose
baseline arm came back `provider_invalid_json` — a provider failure, and
nothing was produced to compare. Raw rows:
`cache/benchmarks/longmemeval/consolidation-multi-session-n24-seed13.jsonl`;
per-arm streams, judged streams, and the paired report alongside.

| | baseline | consolidated |
|---|---|---|
| n (graded pairs) | 23 | 23 |
| accuracy, deterministic containment | **0.130** | **0.217** |
| accuracy, LLM-judge-or-deterministic | **0.087** | **0.087** |
| F1 | 0.087 | 0.087 |
| answered | 3 | 4 |
| `insufficient_evidence` | 16 | 16 |
| mean estimated prompt tokens | 3678 | 3711 |
| mean chunks in the generation | 57.00 | 57.26 |
| mean retrieve seconds | 10.11 | 5.96 |
| mean answer seconds | 23.27 | 21.47 |

**Paired difference: not significant, and on the judge exactly zero.**

| | deterministic | LLM judge |
|---|---|---|
| baseline-only correct | 0 | 0 |
| consolidated-only correct | 2 | 0 |
| both or neither | 21 | 23 |
| accuracy delta | +0.087 | 0.000 |
| McNemar exact p | **0.50** | **1.0** |

**Cost of the consolidated arm: 222 provider calls and 2,918 s of provider time
across 23 questions** — mean 9.65 calls, 126.9 s, and 516,442 prompt characters
per question, producing a mean of 1.04 durable items.

### The delta is the noise floor, and that is measurable here

**22 of the 23 graded pairs sent the model a byte-identical answer prompt.** In
those 22 the two arms are not two configurations; they are the same question
asked twice. Both deterministically-discordant pairs are inside that set:

| pair | baseline prompt chars | consolidated prompt chars | consolidation text in prompt |
|---|---|---|---|
| `d23cf73b` | 15,037 | 15,037 | 0 |
| `2788b940` | 13,596 | 13,596 | 0 |

So the entire +0.087 is the same prompt answered differently twice — and the
judge agrees, scoring both of those answers wrong and leaving zero discordant
pairs. That gives this vault a number it did not have: **with a byte-identical
prompt, this provider disagrees with itself on 2 of 23 multi-session questions,
8.7 percentage points.** Any paired delta at or below that is noise, which is
the quantified form of the 2026-08-26 log entry about one-case wander.

Exactly one pair, `129d1232`, actually differed: 14,966 → 18,011 prompt
characters with one consolidation hit. That pair was concordant — both arms
answered, both scored the same.

### Where consolidation stopped, measured at four levels

| stage | questions (of 23) |
|---|---|
| consolidation produced ≥ 1 durable item | **6** (24 items total, from 222 calls) |
| an entry was written to the daily log | **6** (one entry each) |
| a consolidated chunk appeared among retrieved candidates | **5** |
| consolidation text survived the budget into the answer prompt | **1** |

The funnel is the finding. It is not one failure but three, in series: most days
of personal chat yield nothing the prompt recognises as durable; a day that does
yield something contributes a single chunk to a ~57-chunk generation; and that
chunk, when retrieved, is shed by the context budget before the model sees it.

### Two incidental findings, not fixed here

- **Retrieval returned zero rows on 4 of 23 baseline questions**
  (`d6062bb9`, `gpt4_d84a3211`, `f0e564bc`, `3fe836c9`), with prompts collapsing
  to ~60 estimated tokens. MEM-10 saw the same shape (`retrieve_seconds` 0.09
  and 0.31 on two multi-session rows). Both arms are affected equally, so it
  does not bias this comparison, but it is a live retrieval defect worth its own
  item.
- **The arms are not order-symmetric in cost.** Baseline always runs first and
  pays the cold model load: mean retrieve 10.11 s against 5.96 s, mean build
  24.77 s against 16.50 s. Since 22 of 23 prompts were byte-identical, this did
  not change what was retrieved, but a future version of this stand should
  alternate arm order rather than rely on that.

### Answering the question

On this data, our consolidation does **not** earn its cost. It costs about ten
provider calls and two minutes per question, and it changed one answer prompt
out of twenty-three and zero verdicts. The honest verdict is a null result with
a measured mechanism — not "consolidation is useless", but "on personal-chat
multi-session questions, almost nothing it produces reaches the model, and the
last stage of the funnel is our own context budget, not the consolidator."

The one place it demonstrably does produce knowledge is our own material: the
control above turned a single real session record into 8 quoted durable items
from one call. Whether *those* items improve *our* answers is the measurement
that is still missing, and the last section says why it could not be taken here.

## What this does not measure

- **`reflection.py`.** It has no candidates in this vault, so there is nothing
  to compare. Naming that is the honest outcome for the half of `MEM-13` that
  asked for it.
- **The FactConsolidation gate.** Different benchmark, different competency
  (selective forgetting / conflict resolution). Not run, not claimed.
- **Consolidation as a replacement for raw sessions.** The product appends, and
  this stand measures the product.
- **Cross-day consolidation.** It does not exist to measure. The stand reports
  the structural limit; it does not implement a fix.
- **A paired accuracy number on this vault's own questions.** It was looked for
  and is not available. The vault's two gold sets (`benchmark/vault-retrieval-v1.json`,
  10 cases; `benchmark/vault-application-v1.json`, 7 cases) name gold pages that
  were authored by hand or by compile, not by consolidation, so a difference
  between a corpus with and without consolidation output could not be attributed
  to consolidation. The 23 pages the 2026-08-24 compile wrote from consolidated
  entries are no longer identifiable on disk: `knowledge/notes/` holds 107 files,
  none under an archive directory, and only three mention `raw/sessions` or the
  2026-08-24 daily at all. Building a gold set over our own sessions would mean
  authoring the answers from the same records the system reads — the circularity
  that `docs/research/2026-08-23-*` warns about — so it was not done here.
