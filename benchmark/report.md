# LLM-Wiki Benchmark Report

Date: 2026-08-03 12:48:54
Mode: BM25 only
Queries: 66 canonical known-item queries over 33 active selector winners
Environment: one local Windows / Python 3.14 run; latency is machine-specific

## Results

| Metric | Value |
|---|---|
| Recall@1 | **92.4%** |
| Recall@3 | **100.0%** |
| Recall@5 | **100.0%** |
| Recall@10 | **100.0%** |
| MRR | **0.9596** |
| Latency p50 | **36.4ms** |
| Latency p95 | **43.7ms** |
| Latency avg | **37.5ms** |

## Comparison with competitors (published numbers)

| System | Recall@5 | MRR | Latency p50 |
|---|---|---|---|
| **LLM-Wiki (BM25)** | **100.0%** | **0.9596** | **36.4ms** |
| agentmemory (hybrid) | 95.2% | 88.2% | 14ms |
| agentmemory (BM25 fallback) | 86.2% | 71.5% | <1ms |
| Zep | 94.7% (LoCoMo) | n/a | 155ms |
| Mem0 | 91.6% (LoCoMo) | n/a | 880ms |

## Breakdown by query type

| Query type | Count | Recall@5 | Avg rank when found |
|---|---|---|---|
| exact_title | 33 | 100.0% | 1.0 |
| keywords_from_summary | 33 | 100.0% | 1.2 |

## Missed queries (for improvement)
