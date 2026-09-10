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

**Fixture prepared 2026-09-10 (not yet a corpus).** `run_retrieval_v2.py` pins one
frozen corpus id and its bytes; the measurement extends `retrieval-v2.json` to a
versioned successor carrying these 24 cross-language queries over the same 14
documents (one question per evidence span, asked in the other language), with the
21 original queries as the monolingual control. The matrix already lists
`BAAI/bge-m3`, `Qwen/Qwen3-Embedding-0.6B`, `intfloat/multilingual-e5-large-instruct`
and the rerankers `bge-reranker-v2-m3`, `Qwen3-Reranker-0.6B`; `nomic-embed-text-v2-moe`
is to be added with a pinned revision.

| id | language | evidence | question |
|---|---|---|---|
| x-ru-en-01 | RU | `aurora-rollback-current` | Какой режим журнала SQLite и какой уровень synchronous использует Aurora сейчас? |
| x-ru-en-02 | RU | `aurora-wal-unsupported` | Поддерживается ли режим WAL в текущей локальной среде Aurora? |
| x-ru-en-03 | RU | `aurora-catalog-path` | По какому пути лежит каталог поколений графа доказательств Aurora? |
| x-ru-en-04 | RU | `aurora-collector-symbol` | Как называется точка входа сборщика снимка корпуса в Aurora? |
| x-ru-en-05 | RU | `aurora-sync-command` | Какой командой проверяют синхронизацию памяти Aurora? |
| x-ru-en-06 | RU | `aurora-coordinator-symbol` | Какой класс в Aurora является границей транзакции над Markdown? |
| x-ru-en-07 | RU | `aurora-untrusted-rule` | Как Aurora относится к строке-кандидату, которая просит изменить оценки: это данные или конфигурация? |
| x-ru-en-08 | RU | `aurora-old-wal` | Какой каталог использовала Aurora до июня 2025 года? |
| x-ru-en-09 | RU | `aurora-old-replaced` | Когда старое решение Aurora было заменено? |
| x-ru-en-10 | RU | `aurora-injection-text` | Что именно просит сделать текст-приманка в заметке Aurora? |
| x-en-ru-01 | EN | `beacon-at-least-once` | What delivery guarantee does the current Beacon protocol give? |
| x-en-ru-02 | EN | `beacon-exactly-once-conflict` | Does the old exactly-once delivery claim still hold for Beacon? |
| x-en-ru-03 | EN | `beacon-queue-path` | Where does Beacon keep its task journal? |
| x-en-ru-04 | EN | `beacon-cache-policy` | Where do Beacon search results live, and are they durable? |
| x-en-ru-05 | EN | `beacon-doctor-command` | Which command runs the Beacon health check? |
| x-en-ru-06 | EN | `beacon-worker-symbol` | What is the name of Beacon's main queue symbol? |
| x-en-ru-07 | EN | `beacon-amber` | What colour is the synthetic Beacon painted? |
| x-en-ru-08 | EN | `beacon-not-queue` | Does the Beacon distractor note describe the queue or delivery? |
| x-ru-zh-01 | RU | `cedar-current-delete-rule` | Разрешает ли текущее решение Cedar удалять каталог run при живой аренде? |
| x-ru-zh-02 | RU | `cedar-doctor-before-cleanup` | Что должна пройти безопасная очистка Cedar перед удалением? |
| x-ru-zh-03 | RU | `cedar-state-path` | Где лежит файл передачи проекта Cedar? |
| x-ru-zh-04 | RU | `cedar-logs-path` | По какому относительному пути Cedar пишет журналы выполнения? |
| x-ru-zh-05 | RU | `cedar-doctor-command` | Какой командой проверяют здоровье Cedar? |
| x-ru-zh-06 | RU | `cedar-context-symbol` | Как называется символ точки входа кода Cedar? |
