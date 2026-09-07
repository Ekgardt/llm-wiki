# A reranker that never finished — 2026-09-07

## What the runs said

Three judged runs of 200, 600 questions, 15.3 machine-hours. Per question,
means: ingest 18.4 s, generation build 30.6 s, retrieval 9.8 s, answer 32.3 s
(all of it the provider), total 91.8 s.

Retrieval is bimodal: 117 questions at ~0 s (the EXACT profile, no optional
stage) and **483 questions at exactly 12 s** — `OPTIONAL_STAGE_MAX_SECONDS`.
The rerank stage hit its ceiling on every HYBRID question, was abandoned
(`fallback_reason = optional_stage_timeout`, the fused order stands), and its
12 s were paid anyway: 1.6 of the 15.3 hours.

Inference, stated as one: the reranker switched on 2026-09-04 has taken part
in almost no measured answer since. The gains attributed to it are not its.

## Why it never finished

Measured on this machine (4 cores), the pinned `BAAI/bge-reranker-v2-m3`,
twelve pairs at 512 tokens, batches of two:

| condition | seconds |
|---|---|
| fp32, machine quiet | 10.0 |
| fp32, the test suite running beside it | 18.5–20.8 |
| fp32, 256 tokens | 4.8 |
| **int8 dynamic (Linear layers), 512 tokens** | **4.2** |
| int8 dynamic, 256 tokens | 2.8 |

The stand runs two workers on four cores, so each reranker sees the loaded
condition: 18–21 s against a 12 s bound. Cold load is not the cause — the
bundle loads in 1.8 s; the embedder's 7.5 s is paid inside the build stage.

## What the field says

- sentence-transformers documents ONNX, OpenVINO and int8 dynamic
  quantisation as the CPU speed-ups for cross-encoders, and how to load a
  quantised file from local files only
  (https://sbert.net/docs/cross_encoder/usage/efficiency.html).
- A cross-encoder at int8 loses 1–2 nDCG@10 on hard benchmarks; fp16 holds
  full accuracy but fp16 on CPU is not fast
  (https://zeroentropy.dev/concepts/cross-encoder/).
- Int8 ONNX of `bge-reranker-base` on a quad-core: 20–30 s → 8–15 s batched
  (https://github.com/microsoft/onnxruntime/issues/19494).
- Smaller rerankers exist (MiniLM-L6: ~50 ms per 100 pairs) but are English
  and change the approved model
  (https://futureagi.com/blog/best-rerankers-for-rag-2026/).

## Rank agreement, measured here

Forty real pages of this vault, four queries (two English, one Russian, one
about counts), Spearman of each variant against fp32 at 512 tokens:

| query | int8 ρ | int8 top-3 kept | 256-token ρ | 256 top-3 kept |
|---|---|---|---|---|
| refused write retried | 0.879 | 2/3 | 0.649 | 2/3 |
| когда индекс может отставать | 0.883 | 3/3 | 0.760 | 1/3 |
| retired cursor and antigravity | 0.951 | 1/3 | 0.915 | 2/3 |
| generations of the undo trail | 0.812 | 2/3 | 0.686 | 1/3 |

Int8 at the same speed as 256 tokens keeps the order better in three of four.
Neither is the fp32 order; both are a reranker that runs, against one that
does not.

## Decision

Quantise the pinned model's Linear layers to int8 at load time, dynamic, no
calibration, no new files, same weights and revision;
`LLMWIKI_RERANKER_PRECISION=fp32` restores the old path. The bundle reports
its precision so a trace can say which reranker scored. The torch API used
(`torch.ao.quantization.quantize_dynamic`) is functional and marked deprecated
in torch 2.13 in favour of `torchao`, which is not installed; the migration is
one call when it is.

Not done, with reasons: 256 tokens (loses more order at the same speed); a
smaller model (changes the approved model, English-only); ONNX int8 export (a
2 GB artifact and an install step — the next step if 4.2 s is still too much
under load); raising the bound (pays more for the same stall).

The stand's other overhead — a fresh process and a fresh embedder per
question, about 10 s × 600 — is a stand cost, not a product one, and is left
for the measurement phase.

## The rest of the 92 seconds, profiled

One question (`cProfile`, the test suite running beside it, so absolute
numbers are inflated and proportions are what count):

- **Ingest, 22 s**: 50 daily appends at ~150 ms each — one append is 13
  SQLite commits at `synchronous=FULL`, 10 `fsync`s and 97 connections, the
  one-connection-per-operation shape adopted after the 2026-08-17 corruption.
  In production a session end pays this once; the stand pays it fifty times
  per question. Batching the sessions of a day into one append was
  considered and refused: `_dated_block` writes the resolved dates after each
  entry, so a batched entry is not the entry the product writes, and the
  measurement would no longer be of the product.
- **Build, 29 s**: 18 s is `multilingual-e5-small` encoding 178 chunks. Int8
  dynamic quantisation of the embedder was measured and gained nothing
  (20.8 s → 20.4 s on 131 chunks; top-5 agreement 5/5) — the time is not in
  the Linear layers at this size — so it is not applied.
- **Answer, 32 s**: the provider, all of it.
- A persistent worker that keeps the models loaded across questions would
  save the ~10 s of imports and embedder load per question, but the product
  reads its vault root at import (`prepare_environment` must run "before any
  scripts import"), so one process cannot serve two disposable vaults. Not
  done; would need the product to take its root at call time.

## What this changes for the measurements

Every run from here has a reranker in it. The next three runs are therefore
not comparable to the 0.6983 baseline on retrieval alone; the comparison must
say so.
