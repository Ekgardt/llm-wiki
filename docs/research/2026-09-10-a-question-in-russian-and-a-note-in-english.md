# A question in Russian and a note in English — 2026-09-10

**Finding (issue #29.3).** With `intfloat/multilingual-e5-small`, a Russian
question ranks Russian-language repository documents above the English note
on the same subject; every candidate sits in a 0.84–0.86 cosine band, so the
language of the text outweighs its topic. Same-language recall is right in
both directions. The users supplied four queries with their expected pages,
which is the start of a fixture.

**Two causes, in order.** The corpus is 92 % the checkout's own code, tests
and docs (issue #29.2); most of the Russian text the model prefers is in
`docs/`. The users themselves note that removing that noise may be enough for
e5-small. So the corpus question is measured first, and a model is changed
only if the failure survives it.

**Candidates, as of today.**

| model | params | dims | notes |
|---|---|---|---|
| `intfloat/multilingual-e5-small` (current) | 118M | 384 | cheapest; fails the RU→EN set here |
| `intfloat/multilingual-e5-base` | 278M | 768 | same family and prefixes; twice the memory |
| `intfloat/multilingual-e5-large-instruct` | 560M | 1024 | ~94 languages, instruction-aware |
| `BAAI/bge-m3` | 568M | 1024 | dense + sparse + multi-vector in one pass, 8192 tokens; the usual cross-lingual recommendation |
| `Qwen/Qwen3-Embedding-0.6B` | 0.6B | 1024 | instruction-aware, multilingual; competitive at its size on MMTEB |

Sources: BGE-M3 paper (arXiv 2402.03216) and card
https://huggingface.co/BAAI/bge-m3; Qwen3 Embedding report (arXiv 2506.05176)
and https://qwenlm.github.io/blog/qwen3-embedding/; jina-embeddings-v4
(arXiv 2506.18902) is 3.8B and multimodal, out of scope for a CPU vault;
2026 surveys https://www.bentoml.com/blog/a-guide-to-open-source-embedding-models
and https://www.stackai.com/insights/best-embedding-models-for-rag-in-2026-a-comparison-guide
name bge-m3 as the default multilingual choice and warn that MTEB averages
hide per-pair behaviour, which is why the measurement below is on our pairs.

**Measurement (before any change).**

1. Fixture `benchmark/crosslingual-v1.json`: the users' four queries plus
   thirty RU→EN and EN→RU pairs drawn from the published decision pages
   (question written in the other language, expected page named).
2. Metric: recall@5 and MRR@5 on the dense leg alone, then on the fused
   hybrid answer; the cost columns: model bytes on disk, cold load seconds on
   this CPU, seconds to embed the 7.5k-chunk corpus.
3. Run once with the present corpus and once with the corpus of #29.2; then
   the candidates, in the order of the table, stopping at the first that
   passes recall@5 ≥ 0.8 on both directions within twice e5-small's embed
   time.

**Decision.** No model change now. The fixture and the measurement are the
next step, after the corpus decision; a change is a dated note with the
numbers, per the README's "evidence pending".
