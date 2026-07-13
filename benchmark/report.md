# LLM-Wiki Benchmark Report

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

## Context only: published results on different corpora

| System | Recall@5 | MRR | Latency p50 |
|---|---|---|---|
| **LLM-Wiki (BM25)** | **100.0%** | **0.9702** | **6.1ms** |
| agentmemory (hybrid) | 95.2% | 88.2% | 14ms |
| agentmemory (BM25 fallback) | 86.2% | 71.5% | <1ms |
| Zep | 94.7% (LoCoMo) | n/a | 155ms |
| Mem0 | 91.6% (LoCoMo) | n/a | 880ms |

These rows are not head-to-head comparisons: datasets and tasks differ.

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
