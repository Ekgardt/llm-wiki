# Retrieval must not depend on the language of the question (2026-08-23)

## Why this was researched

The owner asked a plain question — what is this thing for? — and the answer is:
you ask your notes something and get an answer with the place it came from.
Demonstrating that on this vault showed the product does not do it for him.

Measured on the installed vault, the same six questions, three in Russian and
three in English:

    карантин транзакции          -> 1 result      quarantine transaction  -> 10
    потерянный захват сессии     -> 0 results     lost capture session    -> 10
    как компилируется дневник    -> 0 results     daily compile receipt   -> 10

The pages of this vault are written in English by project rule; the owner speaks
Russian. His own knowledge base answers him with nothing. He then stated the
requirement: the system must not depend on the language at all.

## What is actually broken

Two independent things, and only fixing both makes the question's language stop
mattering.

**The lexical leg cannot cross languages, and that is not a defect.** Search here
is SQLite FTS5, which ranks by token overlap. Current practice is blunt about it:
BM25 "falls apart cross-lingually because it depends on token overlap, and the
tokens don't overlap across languages". No amount of tuning changes that.

**The semantic leg was English-only, and never built.** The runtime embeds with
`BAAI/bge-small-en-v1.5` — an English model. And this vault has no vectors at
all: `cache/vectors*` does not exist and the generation reports
`vector_state: absent`, so every query so far has been lexical-only.

## What current practice says

Cross-lingual retrieval is carried by the dense leg with a multilingual encoder:
embeddings "map words, phrases, or sentences from different languages into a
shared vector space", so a query in one language retrieves content in another
"even without direct translation". In hybrid search the division of labour is
explicit — "the dense leg carries almost the entire weight in true CLIR; BM25
only helps when query and corpus share a language". Purpose-built cross-lingual
models are reported to beat translation and modular pipelines for both accuracy
and balanced language coverage.

Two compact candidates keep the 384 dimensions this runtime already stores:
`intfloat/multilingual-e5-small` (100 languages, 12 layers, 384d) and
`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (50+ languages,
384d). E5 is the newer line and is trained with the `query:` / `passage:`
prefixes, which it needs to score well.

## The measurement that decided it

Three real questions from this vault against three real page summaries, cosine
similarity, both models on this machine:

| question (ru) | bge-small-en | multilingual-e5-small |
|---|---|---|
| ночное обслуживание, 26 часов | 0.509 (right, by 0.04) | 0.827 (right, by 0.07) |
| карантин транзакции повтор | 0.523 (right, by 0.004) | 0.855 (right, by 0.07) |
| потерянный захват сессии | 0.476 (**wrong** passage) | 0.807 (right, by 0.02) |

The English model scores everything between 0.43 and 0.52 — that is noise, and
one of its two "hits" is luck with a margin of four thousandths. The
multilingual model puts the right passage first every time with a real margin.

## What this changes here

The runtime embedding model becomes `intfloat/multilingual-e5-small`, pinned by
revision, keeping 384 dimensions so the stored vector shape does not change. The
E5 prefixes are applied on both sides — `query:` for questions and `passage:` for
pages, including the page-encoding path that previously applied no prefix at all.
Existing vectors carry the model id in their manifest and are rejected and
rebuilt, so no stale English vectors survive the change.

The lexical leg stays exactly as it is. It is the right tool when the question
and the pages share a language, and it is not asked to do more.

## What the field offers as of 2026-08-23, and what is worth taking

The owner asked which current achievements solve this with the most effect for
the least cost. Four families matter, and only the first is required.

**One multilingual encoder for the dense leg.** This is the whole fix for
cross-language retrieval and the cheapest thing on the list.
`intfloat/multilingual-e5-small` — 118M parameters, 384 dimensions, 100
languages — is what this vault now uses, measured above. The strongest small
alternatives of 2026 are Snowflake Arctic-Embed 2.0, IBM Granite Embedding
Multilingual R2, Google EmbeddingGemma and Qwen3-Embedding-0.6B; Qwen3-Embedding
leads MMTEB among open models and carries a 32k context, at roughly five times
the parameters. For a corpus of this size the small model is not the bottleneck.

**One model that also replaces the lexical leg.** BGE-M3 produces dense,
learned-sparse and multi-vector representations from a single pass over 100+
languages with an 8192-token context. Its sparse leg is the part that matters
here: learned term weights expand across languages where BM25 cannot, so the
system would stop having a leg that only works when question and pages share a
language. The price is 568M parameters and 1024 dimensions — a stored-vector
shape change — and it is reported to run on 8 GB laptops, so it stays inside the
local-first constraint.

**A multilingual reranker, if any reranker is enabled.** The optional
cross-encoder here is unset. If one is ever switched on it must be multilingual
or it undoes the gain by reordering cross-language hits badly:
`bge-reranker-v2-m3` (278M, CPU-feasible, about 1.2 s per 100 pairs) or the
listwise `jina-reranker-v3` (0.6B).

**Efficiency levers this vault does not need yet.** Matryoshka representation
learning lets a vector be truncated — 256 of 768 dimensions at under 3% quality
loss — and int8 scalar quantization costs almost nothing in recall while cutting
storage fourfold. Binary quantization is the one to avoid at small dimensions:
at 384 it trails int8 by over 17% recall, and at 64 it collapses. With 62 pages
none of this is worth spending a day on; it becomes relevant at hundreds of
thousands of chunks.

**The measurement that decides the order of work.** None of these choices
matters until the dense leg actually exists in the installed vault. Vectors have
never been built here — `cache/vectors*` is absent and the generation reports
`vector_state: absent` — so live search is still lexical-only and a Russian
question still returns nothing. Building and refreshing vectors is the next step;
the model choice above only decides how well that step pays.

## Why one chunk per page had to come with it

Switching the encoder and building the vectors was not enough to make the result
useful. Measured on this vault, every Russian question returned the same large
Russian document filling all four places with its own chunks, and no English
decision page appeared at all. Dense similarity legitimately pulls toward text in
the language of the question, and a long document has many chunks to offer.

Current practice names this exactly: retrieved top-K chunks "are not diverse and
cluster around the same few paragraphs", so the reader "sees the same information
multiple times compressed differently rather than getting three times as much
signal". The standard answers are Maximal Marginal Relevance, which ranks each
candidate by its marginal value against what is already selected, and greedy
semantic deduplication, which drops a chunk too similar to one already kept and
is reported to cut input tokens by 30-50% while sharpening answers.

Both compare candidates to each other. This vault does not need that cost: its
duplication is structural, not semantic — several chunks of one page. Ordering by
page, one chunk each before any second chunk, buys the same diversity for one
pass over the already-ranked list and no extra model call. Nothing is dropped;
the remaining chunks follow, so a caller that wanted them still receives them.

Measured immediately after, on the same questions: four Russian questions that
had returned one document each now return four distinct pages, and the right
English decision page ranks first for "как устроен повтор после карантина" and
second for "зачем нужна аренда владельца для языкового сервера".

## Sources

- Milvus, "How do embeddings enable cross-lingual search?" —
  https://milvus.io/ai-quick-reference/how-do-embeddings-enable-crosslingual-search
- ZeroEntropy, "Cross-lingual retrieval: search across languages, explained" —
  https://zeroentropy.dev/concepts/cross-lingual-retrieval/
- intfloat/multilingual-e5-small model card —
  https://huggingface.co/intfloat/multilingual-e5-small
- sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 —
  https://www.promptlayer.com/models/paraphrase-multilingual-minilm-l12-v2/
- Chen et al., "M3-Embedding: Multi-Linguality, Multi-Functionality,
  Multi-Granularity Text Embeddings" — https://arxiv.org/pdf/2402.03216
- Enevoldsen et al., "MMTEB: Massive Multilingual Text Embedding Benchmark" —
  https://arxiv.org/abs/2502.13595
- "Qwen3 Embedding: Advancing Text Embedding and Reranking Through Foundation
  Models" — https://arxiv.org/pdf/2506.05176
- "Arctic-Embed 2.0: Multilingual Retrieval Without Compromise" —
  https://arxiv.org/pdf/2412.04506
- "Granite Embedding Multilingual R2 Models" — https://arxiv.org/pdf/2605.13521
- BAAI bge-m3 model card (dense, sparse and multi-vector in one pass) —
  https://build.nvidia.com/baai/bge-m3/modelcard
- "Top Reranker AI Models 2026" (bge-reranker-v2-m3 and jina-reranker-v3 sizes
  and CPU latencies) —
  https://local-ai-zone.github.io/guides/best-ai-reranker-models-ultimate-ranking-2026.html
- Sentence Transformers, "Embedding Quantization" —
  https://sbert.net/examples/sentence_transformer/applications/embedding-quantization/README.html
- "Scaling Vector Search: Comparing Quantization and Matryoshka Embeddings" —
  https://towardsdatascience.com/649627-2/
- "RAG with Retrieval-Time Semantic Deduplication" (clustered top-K, 30-50%
  fewer tokens) — https://heyneo.com/blog/rag-retrieval-semantic-deduplication
- "Reducing Redundancy in Retrieval-Augmented Generation through Chunk
  Filtering" — https://arxiv.org/html/2604.24334v1
- "RAG Chunking Strategies: A 2026 Retrieval Playbook" —
  https://www.digitalapplied.com/blog/rag-chunking-strategies-2026-retrieval-quality-playbook

## Open questions

The optional cross-encoder reranker is chosen by `LLMWIKI_RERANKER_MODEL` and is
not configured on this machine. A monolingual reranker would undo the gain by
reordering cross-language hits badly, so if one is ever enabled it has to be
multilingual too.

Whether the compile should also write a short summary in the asking language is
not answered here. The measurement above says it is not needed for retrieval;
it would be about reading, not finding.
