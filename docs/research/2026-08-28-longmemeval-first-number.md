# LongMemEval, our own run: the first honest number (`MEM-10`)

Date: 2026-08-28. Task: `MEM-10` in
`docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md`, section 12. The gate that task
sets is deliberately modest and comes first: **an honest number before any
claim of beating anyone.** The claims we are measured against — Mem0 93.4%
LongMemEval at <7000 retrieval tokens, Zep 90.2% — are the vendors' own, and
`docs/research/2026-08-23-*` already records that published memory-system
records did not survive audit (corrupted labels, lenient judges, corpora that
fit in one context window, reproductions falling 92.3% → 38.4% and 84% →
58.4%). This note does not dispute those numbers. It reports ours.

## What was run

The real product, not a mock. One disposable vault per question:

1. a fresh vault adopted onto the Reliability V3 pair
   (`installed_memory_repair.repair_installed_vault`);
2. every haystack session written through capture's own write path —
   `session_evidence.write_session_evidence` plus a transactional daily entry
   via `flush_memory.append_daily`;
3. one immutable corpus generation built and activated by the maintenance
   builder (`evidence_graph_builder.build_incremental_generation`);
4. the question answered by `retrieval.retrieve_via_search_memory` +
   `query_memory.grounded_qa` against the configured provider.

Harness: `benchmark/run_longmemeval.py`, worker
`benchmark/longmemeval_vault.py`, scoring `benchmark/longmemeval_score.py`.
Dataset: `longmemeval_s` (500 questions, ~50 haystack sessions each), the same
variant the published Mem0/Zep numbers were taken on, cached under
`cache/benchmarks/longmemeval/`. Never synthesised: the loader refuses with a
download instruction when the file is absent.

## The defect that had to be found first

A partial run on 2026-08-28 at 01:20 answered 1 question of 27 and failed the
other 26 with `provider_no_response` / `provider_invalid_json`. The obvious
reading — "the provider is slow on this machine" — was wrong.

Measured, same prompt, same flags, one minute apart:

| working directory of `claude -p` | seconds | answer |
|---|---|---|
| `/home/user/llm-wiki` (this repository) | **175.42** | 980 characters about a pytest permission prompt |
| a neutral temporary directory | **12.59** | `pong` |

The worker inherited the orchestrator's working directory, which is this
repository. `claude -p` loads that directory's `CLAUDE.md`; ours `@`-imports
`knowledge/index.md` and `knowledge/log.md` — about 300 KB of operating
instructions. Every benchmark call therefore ran as an agent turn under this
vault's operating contract and answered something else, or ran past the
timeout. `--setting-sources ""` was already passed (it was added on 2026-08-24
for exactly this class of bug, when the provider answered in the operator's
configured output style); it excludes `settings.json`, not `CLAUDE.md`.

Fix, in the benchmark only: the worker `chdir`s into its throwaway vault
before any provider call (`_leave_the_repository`), and the judge into a bare
temporary directory. Same question, before and after, 240 s ceiling:
**240.43 s / `provider_no_response`** → **14.11 s / a grounded answer**.

This is a benchmark-harness defect, not a product one — but it is worth
recording as a general hazard: any first-party call to the `claude` CLI made
from inside this repository inherits this vault's whole operating contract.

## Three more defects found by the same measurement

- **Stale results read as fresh.** Staging is keyed by question id and
  survives across runs, and `_run_worker` read `<id>.result.json` back
  without proof of authorship. A run started at 11:35 reported rows written at
  01:22 — byte-identical provider timings for workers that had just died. Fixed:
  the file is deleted before the worker starts.
- **`operation_id is already bound to a different request`.** LongMemEval
  haystacks repeat a session id with different turns (question
  `gpt4_76048e76` lists `8fcaf3a9_2` twice), and the harness derived the
  transactional idempotency key from the id alone. The product was right to
  refuse; the harness now keys on the haystack position too.
- **A correct answer scored as wrong.** Gold
  `25 minutes and 50 seconds (or 25:50)`, product answer `…was 25:50` —
  scored 0, because the parenthetical alias variant kept the `or` that
  introduces it. Fixed in `gold_variants`.

One product-side observation, reported and not patched (`scripts/` is owned by
other agents this session): `llm_client._call_claude` returns
`result.stdout or ""` with `check=False` and discards stderr, so a CLI that
exits nonzero with an empty stdout is indistinguishable from one that answered
nothing. Every failure in the 01:20 run reached the report as the single
string `grounded QA provider returned no response`. Likewise
`search_memory._get_embedder` swallows every exception, so a missing
`sentence-transformers` silently degrades the run to lexical-only retrieval
with `vector_state: absent` and no diagnostic — which cost time here, because
a worker started with the system `python3` instead of `uv run` retrieved
nothing at all and looked like a retrieval regression.

## Results

**n = 50**, stratified sample of `longmemeval_s`, seed 13, concurrency 2,
provider `claude` with a 240 s per-call ceiling. Started 11:44 UTC, last row
written 2026-08-28T12:24:55Z, judge pass finished 12:31:49Z — about 41 minutes
for the 50 questions on a machine whose load average sat at 10–11 across four
cores throughout, shared with other agents. Raw rows:
`cache/benchmarks/longmemeval/results-n50-seed13-run2.jsonl`; judged rows and
report alongside it.

**Provider failures: 0 of 50.** Every question was answered, abstained on, or
refused by our own verification gate. That is the headline number of the day,
because the previous run scored 0 of 27 for that reason alone.

| category | n | judge acc | determ. acc | EM | F1 | est. prompt tokens | retrieve s | answer s |
|---|---|---|---|---|---|---|---|---|
| abstention | 4 | **1.000** | 1.000 | 1.000 | 1.000 | 4485 | 12.22 | 23.21 |
| single-session-assistant | 5 | **0.800** | 0.800 | 0.000 | 0.076 | 5292 | 13.75 | 25.94 |
| single-session-user | 6 | **0.500** | 0.500 | 0.000 | 0.148 | 4772 | 8.20 | 14.88 |
| knowledge-update | 7 | **0.429** | 0.286 | 0.000 | 0.055 | 3670 | 10.50 | 26.08 |
| single-session-preference | 3 | **0.333** | 0.000 | 0.000 | 0.092 | 3420 | 12.23 | 27.27 |
| temporal-reasoning | 13 | **0.154** | 0.154 | 0.000 | 0.029 | 4101 | 11.29 | 18.98 |
| multi-session | 12 | **0.083** | 0.083 | 0.000 | 0.002 | 4113 | 10.21 | 24.26 |
| **overall** | **50** | **0.360** | **0.320** | 0.080 | 0.127 | **4222** | **10.93** | **22.28** |

`judge acc` is the LLM-judge verdict where the judge spoke (18 answered rows)
and the deterministic score elsewhere; `determ. acc` is normalized containment
of the gold answer throughout. The two differ on exactly two rows, both cases
where the product stated the fact in prose the overlap metric could not see.

### What the system did with the 50 questions

| outcome | count |
|---|---|
| answered | 18 |
| abstained — `insufficient_evidence` | 26 |
| abstained — `conflicting_evidence` | 1 |
| abstained — `unsupported_time_scope` | 1 |
| refused by our own verification gate | 4 |
| provider never answered | **0** |

The four gate refusals are the product working as designed, and they are
counted as not-correct, not excluded: `cited span states different figures
than the claim it is offered for` (three) and `answered status requires claims
with citations and no abstention reason` (one).

The shape of this table is the real result. **When the product answers, the
judge calls it right 14 times out of 18 — 0.78 precision. It answers 18 times
out of 50.** Accuracy here is bounded by abstention, not by wrong answers, and
the abstention is our own citation-verification contract refusing to state a
fact it cannot bind to bytes. That is the opposite failure mode from the one
the published 93.4% numbers were audited for.

Retrieval reached the question in 47 of 50 cases; on 3 the retriever returned
no rows at all, and the answer abstained. All 50 generations were built with
`vector_state: complete`, so both retrieval legs were live.

### Cost

- **Estimated prompt tokens per question: mean 4222, median 4302, max 5976,
  measured on 49 of 50 rows. Zero questions exceeded 7000.** This is the whole
  grounded-answer prompt — retrieved spans plus question — so it is the same
  envelope Mem0's "<7000 tokens per retrieval" claim describes. Ours fits
  inside it with room to spare, at 0.32–0.36 of their claimed accuracy.
- Retrieval latency: mean 10.93 s, median 12.19 s, max 19.92 s.
- Answer latency (provider): mean 22.28 s, median 21.34 s, max 57.03 s.
- Per-question wall clock: mean 76.87 s, of which ~19 s is ingesting 50
  haystack sessions and ~24 s is building a whole corpus generation from
  scratch. Those two are benchmark scaffolding, not steady-state cost: a real
  vault ingests once and indexes nightly.
- Judge pass: 18 graded rows, mean 5.55 s each.

### One teardown artefact, named

14 of the 50 worker processes exited with SIGABRT (`terminate called without
an active exception`) *after* writing their result — a native thread left by
the embedding model at interpreter shutdown, already recorded in
`knowledge/log.md` on 2026-08-26. The measurement completes first, so the row
is kept and the exit code is carried in it as `worker_exit`. No row was lost
to this, and no row was invented because of it.

## What these numbers are not

- **Not the paper's metric.** LongMemEval's official protocol grades with
  per-question-type GPT-4o judge prompts. Nothing on this machine can
  reproduce that judge. What is reported here is (a) a deterministic
  text-overlap metric — normalized containment of the gold answer, plus exact
  match and token F1 — and (b) where stated, an LLM-judge pass using one
  generic grading prompt and the *same local `claude` provider that produced
  the answers*. A same-family judge is a known leniency risk; both are labelled
  wherever they appear and neither is one-to-one comparable with a published
  93.4%.
- **Not the full 500.** The sample is stratified proportionally across
  (question type, abstention) with a fixed seed, so the same command names the
  same questions on any machine — but a sample of this size has a wide
  confidence interval and per-category rows are single digits. Read the
  per-category numbers as direction, not as rates.
- **Not throughput-representative.** This machine has exactly one LLM
  provider, the `claude` CLI, and it was shared with other agents throughout
  (load average 10–11 on 4 cores). Every latency here is an upper bound in the
  wrong direction, and the provider is the bottleneck, not retrieval.
- **Provider failures are not wrong answers.** A question the provider never
  answered produced nothing to compare against the gold. Counting it as wrong
  would publish this machine's single-provider throughput as the memory
  system's recall, so `accuracy` is a mean over `scored` rows and
  `provider_failures` is its own column. `benchmark/longmemeval_score.py` and
  the judge both enforce this.

## Deliberate deviations from the stock product path

- The nightly maintenance pass indexes compiled pages; here the ingested daily
  evidence files are named explicitly as `daily_paths` so the haystack is in
  the generation. Without this the benchmark would measure the compile
  pipeline's latency, not retrieval.
- `grounded_qa` is given a 28 672-byte input budget instead of the stock
  8 192. One LongMemEval session entry is ~10 KB, so under the stock budget
  every span is shed and the answer refuses itself. The larger budget keeps
  the whole prompt at roughly 7k estimated tokens — the same retrieval
  envelope Mem0's "<7000 tokens" claim describes, so the cost comparison stays
  fair rather than being won by starving ourselves.
- Session evidence is written for every haystack session, but the corpus is
  built over the daily entries. This mirrors the 2026-08-24 finding
  (`MEM-01`, reversed) that indexing raw session records buries the compiled
  pages that answer questions.

## Reproduce

```bash
uv run python benchmark/run_longmemeval.py --sample 50 --seed 13 \
    --concurrency 2 --provider claude --provider-timeout 240 \
    --results cache/benchmarks/longmemeval/results-n50-seed13-run2.jsonl \
    --report cache/benchmarks/longmemeval/report-n50-seed13-run2.json
uv run python benchmark/longmemeval_judge.py \
    --results cache/benchmarks/longmemeval/results-n50-seed13-run2.jsonl
```

The run is resumable: already-answered question ids are skipped, so an
interrupted run continues where it stopped.

One note on the artefacts: the report JSON the run wrote at the end used the
scorer as it stood when that process started, which was one commit before the
`(or 25:50)` alias fix, and read 0.300 overall. It was regenerated from the
untouched raw JSONL with the current scorer and reads 0.320 — the single
knowledge-update row that fix was made for. The raw rows were not edited; only
the derived aggregate was recomputed.

## What is still open

- **n = 50, not 500.** The full run is the same command with `--full`. At the
  measured 76.9 s per question and concurrency 2 on a shared machine, that is
  roughly 5.5 hours of wall clock and was not run here.
- **LoCoMo is untouched.** `MEM-10` names both benchmarks; this note covers
  LongMemEval only.
- **The judge is same-family.** A cross-family judge, or the paper's
  per-type prompts against a different model, would be the next honest
  strengthening — and would most likely move the number down, not up.
- **The bottleneck to attack is abstention, not wrong answers.** 26 of 50
  questions ended in `insufficient_evidence` with retrieval having returned
  rows in 23 of them, which points at the grounding gate and the span budget
  rather than at recall. `multi-session` (0.083) and `temporal-reasoning`
  (0.154) are where the gap to the published claims lives, and both are the
  categories `MEM-11` (bitemporal claims) is aimed at.
