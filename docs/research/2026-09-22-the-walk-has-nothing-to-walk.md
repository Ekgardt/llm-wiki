# The walk has nothing to walk

Dated 2026-09-22. Mechanism 4 of the approved plan
(`docs/research/2026-09-22-what-the-sciences-really-lend-a-memory.md`) is a bounded graph
walk — personalized PageRank from the retrieved seeds over the evidence graph, after HippoRAG
— and both research reports behind it set the same precondition before any code: count, in
the recorded runs, how many of the sessions retrieval *missed* are linked to a session it
*found* by any pointer the graph records. If few are, the walk has nothing to walk. This note
is that count. No product file is edited; the finding is that none should be, yet.

Files: read only — `scripts/retrieval.py` (the graph lane: `GRAPH_WEIGHT`, `GRAPH_MAX_HOPS`,
`GRAPH_SEED_LIMIT`, `_graph_seeds`, `_prepare_graph_hits`, `expand_evidence_graph`),
`scripts/evidence_graph.py` (`EvidenceGraph.neighbors`, `reachable`), `scripts/co_activation.py`,
`scripts/knowledge_extractor.py`, `scripts/fact_keys.py`, `benchmark/longmemeval_vault.py`,
`benchmark/locomo_data.py`, `benchmark/longmemeval_coverage.py`. The measurement script lives
in the job scratch directory, not in the repository: a one-off count that returned zero is not
a product module.

## What the graph lane is today

`PROFILE_SIGNALS` in `scripts/retrieval.py` gives the graph signal to four profiles — `GRAPH`,
`REPO_MAP`, `IMPACT`, `GLOBAL` — and to none of the ones a memory question is routed to. Every
recorded LongMemEval and LoCoMo row ran under `HYBRID` (lexical, dense). So the "one hop, five
seeds, weight 0.5" lane (`GRAPH_MAX_HOPS = 1`, `GRAPH_SEED_LIMIT = 5`, `GRAPH_WEIGHT = 0.5`,
`GRAPH_PER_SEED_LIMIT = 4`, `GRAPH_GLOBAL_LIMIT = 12`) never touched a benchmark answer. A PPR
walk would be a new lane for `HYBRID`, not a change to the existing one.

## What a benchmark vault's graph contains

The stand (`benchmark/longmemeval_vault.py::ingest_sessions`) writes every haystack session
as one block of a daily file — `## [HH:MM:SS] session_end | <id>` and the rendered transcript
— and builds one generation over those daily files. The knowledge extractor
(`scripts/knowledge_extractor.py`) makes one `knowledge-page` node per file and draws edges
from three things a page can carry: `[[wikilinks]]` (`LINKS_TO`), a `superseded_by` field
(`SUPERSEDES`) and claims (`EVIDENCED_BY`). A transcript carries none of them. The co-activation
table (`scripts/co_activation.py`) is built from `[[wikilinks]]` inside daily entries — none.
Fact keys (`scripts/fact_keys.py`) are a `keys` column on the search table, not an edge, and
the stand keys turns only under `LLMWIKI_BENCH_FACT_KEYS=1`; both recorded runs have
`keyed_turns: null`.

So before the count, the structure already says what the count will be: the only nodes are
the daily pages, and there are no edges. The count was still made, on rebuilt vaults, because
the plan asks for the number and not the argument.

## Method

For every non-abstention LongMemEval row of `cache/benchmarks/full-2026-09-18/lme500.jsonl`
whose `coverage.all_sessions` is false (41 questions; 129 labelled sessions, 71 in the 12),
and for every LoCoMo multi-hop row of `cache/benchmarks/locomo-2026-09-19/locomo300.jsonl`
(43 questions in 10 conversations; the LoCoMo coverage block labels no sessions, so the
labelled sessions are read from the dataset's own `D<session>:<turn>` evidence labels), the
staging vault was rebuilt exactly as the stand builds it — `prepare_environment`, `_adopt`,
`ingest_sessions`, `build_generation`, `_warm_reranker`, `_retrieved_rows` under the
question's own profile — with `MEMORY_LLM_PROVIDER=fake` (the build makes no model call when
fact keys are off). The retrieved 12 name their session in the heading ancestry
(`session_end | <id>`); a labelled session named there is *found*, the rest are *missing*.
Then the generation's `evidence.sqlite3` was read: every node was attributed to the session
block whose byte range holds its occurrence (a node spanning the whole daily file belongs to
the day, not to a session), every `assertion` row became an undirected edge whatever its
type, and a missing session counted as *reachable* when any node of its block lies within two
edges of any node of a found session's block. The co-activation pairs and the `chunk_keys`
rows of the same generation were counted beside it.

The reproduction was checked against the record where it could be: for LongMemEval the
rebuilt `all_sessions` against the recorded one.

## Numbers

LoCoMo multi-hop, all 43 recorded questions rebuilt (10 conversation vaults, one per
conversation since every question of a conversation shares its haystack):

| | count |
|---|---|
| questions | 43 |
| questions with at least one labelled session outside the 12 | 28 |
| of those, with at least one labelled session inside the 12 (a seed exists) | 23 |
| labelled sessions / found / missing | 119 / 73 / 46 |
| missing sessions within two edges of a found one | **0** |
| missing sessions in the same daily file as a found one | 0 |
| graph nodes, by kind, summed over the 10 vaults | `knowledge-page` only, one per daily file |
| graph edges, any type | **0** |
| nodes attributed to a session block (as opposed to a whole daily file) | 0 |
| co-activation pairs | 0 |
| chunks carrying a fact key / chunks | 0 / 16 371 |

LongMemEval, questions with `all_sessions` false, rebuilt one vault per question. A rebuild
costs 45–69 s a question (ingest, build with vectors, warm reranker, retrieve), so the sample
was cut at the first 10 of the 41 after ten minutes, as the coordinator asked; the remaining
31 kept running in the background and are reported in the job report if they finished. All 10
are `multi-session` questions, which is the type the walk targets.

| | count |
|---|---|
| questions (of 41) | 10 |
| questions with at least one labelled session outside the 12 | 7 |
| of those, with a seed inside the 12 | 7 |
| labelled sessions / found / missing | 39 / 29 / 10 |
| missing sessions within two edges of a found one | **0** |
| missing sessions in the same daily file as a found one (a container, not an edge) | 5 |
| graph nodes, by kind | `knowledge-page` only, one per daily file (77 over 10 vaults) |
| graph edges, any type | **0** |
| nodes attributed to a session block | 0 |
| co-activation pairs | 0 |
| chunks carrying a fact key / chunks | 0 / 4 861 |
| rebuilt `all_sessions` agrees with the recorded row | 7 of 10 |

The three disagreements all go one way — the rebuilt 12 hold *more* labelled sessions than
the recorded 12 (`6d550036`: 0 recorded, 3 rebuilt; `60472f9c` and `gpt4_ab202e7f`: all
found). The retrieval code moved between the run and the rebuild: the per-session quota of
2026-09-19 (`3294a7b5`) and the lane matrix change which 12 are shown. That moves the
found/missing split, not the graph — the graph has the same zero edges under either code,
which is the quantity this note measures.

Both legs together: 56 missing sessions with a seed to walk from, 0 within two hops.

## What this means

The precondition fails at zero, not at a fifth. A walk along links recorded at encoding time
needs links recorded at encoding time, and the benchmark vault records none: its only source
is verbatim transcripts, which carry no wikilinks, no claims, no supersession. HippoRAG gets
its edges from an extraction step at indexing (open information extraction over every
passage into an entity graph); the equivalent here is the ledger of things and events —
plan item 7, an architecture change waiting for the owner's yes — or, more cheaply, fact
keys shared between turns, which the stand has but the recorded runs did not pay for. Until
one of those writes edges, PPR over the evidence graph on a benchmark vault is PPR over
isolated points and returns the seeds.

On the live vault the picture differs — 505 `LINKS_TO` edges among 58 knowledge pages and 92
`EVIDENCED_BY` claims in the active generation — but the plan's decision rule adopts a change
only on the decide half of both public benchmarks, and there the walk cannot move a number.

Decision: the walk is not built. Mechanism 4 stays in the plan behind item 7 (the ledger)
or behind a decision to key the haystack on the stand; either one is what gives the walk
something to walk, and the count in this note is what to rerun once it does.

## Sources

- Gutiérrez, Shu, Gu, Yasunaga, Su. *HippoRAG: Neurobiologically Inspired Long-Term Memory
  for Large Language Models.* NeurIPS 2024, arXiv 2405.14831 — peer-reviewed; abstract opened
  2026-09-22: "HippoRAG synergistically orchestrates LLMs, knowledge graphs, and the
  Personalized PageRank algorithm to mimic the different roles of neocortex and hippocampus
  in human memory." Node specificity, damping 0.5 and the 2Wiki / HotpotQA figures are quoted
  from the paper in `RESEARCH-E-physics-math-2026-09-22.md` §6 (job scratch).
- `RESEARCH-D-biology-2026-09-22.md` §2.4 and `RESEARCH-E-physics-math-2026-09-22.md` §6 (job
  scratch): both name this count as the step before any code.
- `docs/research/2026-09-22-a-class-level-plan-to-pass-the-field.md` — the decision rule.
- Recorded runs: `cache/benchmarks/full-2026-09-18/lme500.jsonl`,
  `cache/benchmarks/locomo-2026-09-19/locomo300.jsonl` and their staging question files.
