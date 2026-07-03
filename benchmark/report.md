# LLM-Wiki Benchmark Report

Date: 2026-07-03 22:58:10
Mode: BM25 + Vector (hybrid RRF)
Queries: 52

## Results

| Metric | Value |
|---|---|
| Recall@1 | **88.5%** |
| Recall@3 | **100.0%** |
| Recall@5 | **100.0%** |
| Recall@10 | **100.0%** |
| MRR | **0.9423** |
| Latency p50 | **41.3ms** |
| Latency p95 | **68.1ms** |
| Latency avg | **406.8ms** |

## Comparison with competitors (published numbers)

| System | Recall@5 | MRR | Latency p50 |
|---|---|---|---|
| **LLM-Wiki (hybrid)** | **100.0%** | **0.9423** | **41.3ms** |
| agentmemory (hybrid) | 95.2% | 88.2% | 14ms |
| agentmemory (BM25 fallback) | 86.2% | 71.5% | <1ms |
| Zep | 94.7% (LoCoMo) | n/a | 155ms |
| Mem0 | 91.6% (LoCoMo) | n/a | 880ms |

## Breakdown by query type

| Query type | Count | Recall@5 | Avg rank when found |
|---|---|---|---|
| exact_title | 26 | 100.0% | 1.1 |
| keywords_from_summary | 26 | 100.0% | 1.1 |

## Missed queries (for improvement)
