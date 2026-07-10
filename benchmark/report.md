# LLM-Wiki Benchmark Report

Date: 2026-07-10 06:43:12
Mode: BM25 only
Queries: 60

## Results

| Metric | Value |
|---|---|
| Recall@1 | **95.0%** |
| Recall@3 | **100.0%** |
| Recall@5 | **100.0%** |
| Recall@10 | **100.0%** |
| MRR | **0.9667** |
| Latency p50 | **6.0ms** |
| Latency p95 | **9.0ms** |
| Latency avg | **7.0ms** |

## Comparison with competitors (published numbers)

| System | Recall@5 | MRR | Latency p50 |
|---|---|---|---|
| **LLM-Wiki (BM25)** | **100.0%** | **0.9667** | **6.0ms** |
| agentmemory (hybrid) | 95.2% | 88.2% | 14ms |
| agentmemory (BM25 fallback) | 86.2% | 71.5% | <1ms |
| Zep | 94.7% (LoCoMo) | n/a | 155ms |
| Mem0 | 91.6% (LoCoMo) | n/a | 880ms |

## Breakdown by query type

| Query type | Count | Recall@5 | Avg rank when found |
|---|---|---|---|
| exact_title | 30 | 100.0% | 1.0 |
| keywords_from_summary | 30 | 100.0% | 1.2 |

## Missed queries (for improvement)
