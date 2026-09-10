# Cross-lingual memory: what the world does on 2026-09-10

**Question (owner).** Why a local encoder at all — why not the cheapest model
of the provider we already pay for? And what is the best current practice
for a question in Russian against a page in English?

**1. What the provider can and cannot give.**

- Anthropic ships no embedding model; its documentation points to Voyage AI
  (https://docs.claude.com/en/docs/build-with-claude/embeddings). Our
  providers are CLI subscriptions (Claude Code, Codex, OpenCode): they return
  text, never vectors. An index needs a vector per chunk (thousands) and per
  query; a chat model cannot produce one.
- Voyage 4 (Jan 2026) and Cohere embed-v4 are strong multilingual APIs;
  Voyage gives 200 M tokens free. Rejected all the same: the vault is the
  owner's private memory and would leave the machine for every chunk, and
  the zero-cost rule is "no paid API beyond subscriptions", not "free tier
  today". A local encoder keeps the memory local; that is why it is local.
- What the cheap subscription model *can* do is text work per query:
  translate or expand the question. That is one small call per search.

**2. Evidence: embeddings beat query translation.** Query Translation vs.
Cross-Lingual Embeddings (arXiv 2608.12820, Aug 2026): BGE-M3 reaches
Recall@15 of 96.2 % (Sinhala→English) and 95.6 % (Tamil→English), above
the best translation route (Google Translate, 92.4 % / 93.0 %), without
translation latency or translation errors; monolingual retrieval on those
pairs is under 10 %. Multilingual E5 and LaBSE sit below BGE-M3. Query
translation stays the cheaper, retrieval-time fallback (survey arXiv
2510.00908).

**3. The 2026 low-cost pipeline.** WSDM Cup 2026 multilingual retrieval
(English queries over 10 M Chinese, Persian and Russian documents): a
four-stage pipeline — LLM query expansion (GRF style) → BM25 candidates →
dense ranking → pointwise reranking of the top 20 with Qwen3-Reranker-4B
— reached nDCG@20 0.403 at low cost (arXiv 2602.16989; Naver's entry,
arXiv 2602.20986, adds learned sparse retrieval and score fusion). The
shape is exactly our hybrid: a lexical leg, a dense leg, a reranker. What
we lack for the cross-lingual case is (a) a lexical leg that can match at
all when the query and the page share no words, and (b) an encoder whose
cross-lingual alignment is strong enough.

**4. Encoders that run on a CPU (open weights).**

| model | params | notes |
|---|---|---|
| `intfloat/multilingual-e5-small` (ours) | 118M | fails the users' RU→EN set |
| `nomic-ai/nomic-embed-text-v2-moe` | 475M, 305M active | MoE, ~100 languages, the fast tier on CPU |
| `Qwen/Qwen3-Embedding-0.6B` | 0.6B | MMTEB 64.3, the strongest sub-1 GB model |
| `BAAI/bge-m3` | 568M | dense + sparse + multi-vector; the cross-lingual reference |
| `zeroentropy/zembed-1` | 4B | 2026 leaderboard top; too large for a CPU vault |

Sources: MMTEB (arXiv 2502.13595), Qwen3 Embedding (arXiv 2506.05176),
https://www.bentoml.com/blog/a-guide-to-open-source-embedding-models,
https://huggingface.co/zeroentropy/zembed-1-embedding.

**5. Decision for the measurement (no product change yet).** Four arms on
the fixture from `2026-09-10-a-question-in-russian-and-a-note-in-english.md`,
all on the new memory-only generation:

1. as is: e5-small, hybrid;
2. + query expansion by the cheapest subscription model (Haiku): an English
   pseudo-passage for the lexical leg, one call per query, cached by query;
3. encoder swap, in order of cost: nomic-v2-moe, Qwen3-Embedding-0.6B, bge-m3;
4. + a multilingual reranker on the top 20 (bge-reranker-v2-m3 or
   Qwen3-Reranker-0.6B) — the stand records that our reranker never applied.

Metrics: recall@5 and MRR@5 in both directions; cold load seconds, seconds
to embed the corpus, tokens per query for arm 2. Choose the cheapest arm
that passes recall@5 ≥ 0.8 both ways. The literature predicts arm 3 wins
on quality and arm 2 is the cheapest partial fix; the numbers decide.

**Limits.** All figures above are other people's corpora; our fixture is
thirty-odd pairs and answers only "which arm on this vault". Runs start on
the owner's word.
