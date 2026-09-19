# LLM-Wiki Benchmark Report

**Retired 2026-09-10.** This gate measured BM25 over the wiki pages the
repository used to ship. Since 2026-09-10 the repository ships no memory, so the
`legacy-60` and `current-generated` corpora have nothing to measure, and
`benchmark/run_benchmark.py` refuses `--legacy-only` outright. The frozen
synthetic corpus `benchmark/retrieval-v2.json` is the retrieval gate now. What
follows is this gate's last recorded state, kept as a record — not as a claim
about the product today. See
`docs/research/2026-09-10-the-repository-ships-no-memory.md`.

Date: 2026-07-13 13:22:14
Mode: BM25 only
Queries: 112
Corpus: current-generated-v2

## Results

| Metric | Value |
|---|---|
| Recall@1 | **94.6%** |
| Recall@3 | **100.0%** |
| Recall@5 | **100.0%** |
| Recall@10 | **100.0%** |
| MRR | **0.9702** |
| Latency p50 | **6.1ms** |
| Latency p95 | **10.5ms** |
| Latency avg | **7.1ms** |

## Context only: numbers published by other projects

These rows are not head-to-head comparisons: the datasets and the tasks differ,
and nothing here was reproduced on this machine. So every row carries four
things of its own — the metric it actually is, the value, the dataset it was
taken on, and a source that can be opened.
**A number with no source does not belong in this table.**

| System | Metric | Value | Dataset | Source |
|---|---|---|---|---|
| LLM-Wiki (BM25) | Recall@5 | 100.0% | current-generated-v2, 112 queries | benchmark/report.md |
| LLM-Wiki (BM25) | MRR | 0.9702 | current-generated-v2, 112 queries | benchmark/report.md |
| LLM-Wiki (BM25) | Latency p50 | 6.1ms | current-generated-v2, 112 queries | benchmark/report.md |
| agentmemory (hybrid) | Recall@5 | 95.2% | LongMemEval-S | https://github.com/rohitg00/agentmemory |
| agentmemory (hybrid) | MRR | 0.882 | LongMemEval-S | https://github.com/rohitg00/agentmemory |
| agentmemory (BM25 fallback) | Recall@5 | 86.2% | LongMemEval-S | https://github.com/rohitg00/agentmemory |
| agentmemory (BM25 fallback) | MRR | 0.715 | LongMemEval-S | https://github.com/rohitg00/agentmemory |
| agentmemory (hybrid) | Latency | 14ms | coding-agent-life-v1, 15 sessions | https://github.com/rohitg00/agentmemory |

Read the two sides apart. Our Recall@5 is retrieval over 112 generated queries
against pages this repository no longer ships; agentmemory's is retrieval over
LongMemEval-S. Its 14ms comes from a third corpus again — `coding-agent-life-v1`
— so it sits in a row of its own instead of on the end of the Recall@5 one,
which is where it used to be. MRR is written here as a ratio, the unit our own
MRR uses; the source writes the same two values as 88.2% and 71.5%.

**Removed 2026-09-19:** two rows that read `Zep | 94.7% (LoCoMo)` and
`Mem0 | 91.6% (LoCoMo)` under this table's old `Recall@5` column. Both are
end-to-end answer-accuracy claims, not retrieval recall, on a dataset we have
never run — and neither survives its source. Fetched 2026-09-19,
`https://mem0.ai/blog/state-of-ai-agent-memory-2026` reports Mem0 LoCoMo 92.5
and Zep LoCoMo 80.32%, and `https://blog.getzep.com/state-of-the-art-agent-memory/`
reports 94.8% on Deep Memory Retrieval without mentioning LoCoMo at all. There
was nothing to repair in those rows, so they are gone. See
`docs/research/2026-09-19-a-number-names-its-stand.md`.

## Breakdown by query type

| Query type | Count | Recall@5 | Avg rank when found |
|---|---|---|---|
| exact_title | 34 | 100.0% | 1.0 |
| keywords_from_summary | 34 | 100.0% | 1.1 |
| partial_title | 27 | 100.0% | 1.1 |
| slug_match | 17 | 100.0% | 1.0 |

## Legacy 60-query gate

Corpus: `legacy-60-v1` (60 frozen query/gold-path pairs).
Recall@5: **100.0%**; MRR: **0.9694**.
Gate: Recall@5 >= 100%.

## Missed at Recall@5
