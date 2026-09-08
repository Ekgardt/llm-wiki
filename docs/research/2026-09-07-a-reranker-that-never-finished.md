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

## Two workers, four cores, eight threads

Each worker's torch opened four intra-op threads, so the two workers ran
eight on four cores. Measured with the machine quiet, two workers embedding
131 chunks side by side: 28.5 s each at four threads, **15.6 s each at
two**. PyTorch's tuning guide says the same: with M processes on N cores
set each to `floor(N/M)` threads to avoid oversubscription
(https://docs.pytorch.org/tutorials/recipes/recipes/tuning_guide.html). The
stand now sets `OMP_NUM_THREADS` to that share for each worker unless the
operator set one. Expected on the runs: embedding and reranking in roughly
half the time per question; unmeasured on a full run until the next one.

## Addendum 2026-09-08 — the quantised copy cost 2.5 GB of memory

The first stand run with int8 lost a worker to the kernel OOM killer at
3.8 GB anonymous memory, and the long-lived MCP server sat at 4.3 GB. Measured
in one process (`/proc/self/status`, anonymous vs file-backed):

| state | anonymous | file-backed |
|---|---|---|
| fp32 loaded, idle | 785 MB | 324 MB |
| fp32 after scoring twelve pairs | 804 MB | 1 484 MB |
| int8 by `quantize_dynamic` copy, fp32 deleted, gc | 3 606 MB total | — |
| int8 **in place**, heap trimmed | 1 066 MB | 1 485 MB |

The copy owns every weight — including the 1 GB word-embedding table the
fp32 model only maps from disk — and glibc keeps the freed heap. In place, the
int8 model costs 0.27 GB more anonymous memory than fp32 and scores twelve
pairs in 4.9 s. `_cpu_precision` now quantises in place and calls
`malloc_trim`. The 2.6 GB file-backed pages are the page cache and are
evictable; they are not what the OOM killer counts.

Two other things the night measured: ONNX export is closed for now (the
installed `optimum` 1.27 does not import against `transformers` 5.13 and the
`onnx` package is not installed — both dependency decisions, not for
tonight); and the stand's two workers do not fit beside VS Code Server and
two MCP servers on 16 GB, so the runs go one worker at a time.

## What this changes for the measurements

Every run from here has a reranker in it. The next three runs are therefore
not comparable to the 0.6983 baseline on retrieval alone; the comparison must
say so.
