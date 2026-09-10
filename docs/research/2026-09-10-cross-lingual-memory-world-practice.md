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

**Corpus built (owner's permission to measure, 2026-09-10).**
`benchmark/retrieval-v2-crosslingual.json` (the 14 documents, 45 queries, 27
cross-language) with its schema copy and `benchmark/model-matrix-crosslingual-v1.json`
(the v1 matrix plus `intfloat/multilingual-e5-small`, revision `614241f6`, so the
product's own encoder is an arm). `run_retrieval_v2.py` accepts both frozen
corpus ids. Arms run: BM25 only (L4); e5-small; bge-m3; Qwen3-Embedding-0.6B;
e5-large-instruct; e5-small and bge-m3 each with bge-reranker-v2-m3. The
query-expansion arm (Haiku) is not in the runner and is not measured here.

**Measured 2026-09-10 (4 cores, CPU, float32; reports in
`benchmark/results/crosslingual-2026-09-10/`).** MRR@10 on the 27
cross-language queries (xMRR), on all 45 (allMRR), and on the RU and EN
slices; model load and warm p50 latency per query.

| arm | xMRR | xNDCG | allMRR | RU | EN | load s | p50 ms |
|---|---|---|---|---|---|---|---|
| BM25 only (L4) | 0.348 | 0.501 | 0.581 | 0.529 | 0.536 | – | 1.7 |
| e5-small (ours) | 0.601 | 0.701 | 0.732 | 0.749 | 0.625 | 6.7 | 12.5 |
| Qwen3-Embedding-0.6B | 0.608 | 0.706 | 0.748 | 0.656 | 0.821 | 6.2 | 193 |
| e5-large-instruct | 0.638 | 0.729 | 0.767 | 0.691 | 0.824 | 7.2 | 132 |
| bge-m3 | 0.524 | 0.640 | 0.682 | 0.630 | 0.629 | 6.0 | 100 |
| e5-small + bge-reranker-v2-m3, depth 10 | **0.981** | 0.986 | 0.988 | 0.977 | 1.000 | 6.7 + 1.7 | 12.5 + 404 |
| bge-m3 + bge-reranker-v2-m3, depth 10 | 0.981 | 0.986 | 0.988 | 0.977 | 1.000 | 6.0 + 1.8 | 100 + 408 |

Reranker depths 10, 20 and 50 give the same numbers on this corpus (28
spans); depth 10 is the cheapest. Peak RSS with the reranker: 2.5 GB.

**Reading.** Swapping the encoder moves the cross-language MRR by at most
+0.04 (e5-large-instruct) and bge-m3 is *worse* than e5-small here, at
8–15× the query latency. The lever is the multilingual cross-encoder: on top
of our own e5-small it takes cross-language MRR from 0.60 to 0.98 and the
whole corpus to 0.99, for about 0.4 s per query and 1.7 s to load once.
The literature's "embeddings beat translation" holds against BM25, but the
gain that matters came from reranking, which the WSDM 2026 pipeline also
puts last. Not measured: the Haiku query-expansion arm (no runner support).

**Decision proposed to the owner.** Keep `multilingual-e5-small` as the
encoder; make `BAAI/bge-reranker-v2-m3` the product's default reranker at
depth 10, resident in the MCP server so the 1.7 s load is paid once, and
give its stage a budget that fits a cold CLI (the stand recorded
`optional_stage_timeout` for the reranker on every question). Encoder work
stops here until a bigger fixture says otherwise.

**Limits.** 45 queries over 14 synthetic documents; every candidate fits
in the top 10, so recall metrics saturate and MRR/nDCG carry the signal.
The owner's real pages are English with Russian questions; the fixture
mirrors that shape, not its size.

**Decided by the owner, 2026-09-10** («делай перепранжировщик»). Implemented
the same day.

**What was actually off.** Three gates kept the shipped reranker out of
almost every answer, and none of them was the model:

1. `reranker.configured_reranker_identity` returned nothing unless
   `LLMWIKI_RERANKER_MODEL` and an immutable revision were both set in the
   environment. On this machine `~/.claude/settings.json` sets them, so hooks
   and the MCP server had a reranker; a fresh install, the nightly unit and
   any shell without them had none, silently (`reranker_unavailable`).
2. `reranker.should_rerank` ran the stage only on a trigger: the GLOBAL
   profile, a `global_synthesis` or `cross_language` intent, the top lexical
   and dense hits disagreeing, or the top two fused scores within 5 %. The
   `cross_language` intent is a regex over the *question* — it fires only
   when one question mixes two scripts. A Russian question over English
   pages, the users' case 29.3, never matched it; the reranker answered
   "conditions_unmet" and the fused order stood.
3. `DEFAULT_RERANK_DEPTH` was 20, and 20 passages of up to 512 tokens cost
   4.2 s at int8 on four loaded cores (`2026-09-07-a-reranker-that-never-
   finished.md`); the MCP path grants an optional stage about 3.5–5 s, so
   even a triggered rerank often hit `optional_stage_timeout`.

**Change.** In `scripts/reranker.py`: the product default identity is
`BAAI/bge-reranker-v2-m3` at the matrix-pinned revision
`953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e` when the environment names no
reranker; `LLMWIKI_RERANKER_MODEL=off` is the explicit off switch; a
partial or mutable identity is still refused, not repaired. `should_rerank`
reranks every question in a rerank profile unless there is a named reason
not to (disabled, exact profile, quoted phrase or identifier, one
candidate); the four trigger heuristics and the `conditions_unmet` reason
are deleted. Depth is 10 over the fused pool of 20; the tail keeps its
fused order behind the scored head, per the 2026-08-26 tier rule. A load
that fails is recorded once with its reason and not retried per question,
as `search_memory._get_embedder` already does for the encoder. In
`scripts/mcp_server.py` the warm-up loads the reranker before its two
passes, so a resident server pays the 1.8 s load and the int8 quantisation
once and never inside a question. The CLI is unchanged: it reranks only
with `--rerank`, because a cold process cannot amortise the load.

**Cost, honestly.** The 0.4 s per question above was measured on the
benchmark's short passages in fp32. On this vault's passages (up to 512
tokens) the 2026-09-07 measurement is the one that applies: about 3.5 s
for ten pairs at int8 on four loaded cores, about 2 s quiet. Measured after
the change on the live vault — see the figures appended below.

**Not changed.** Weights are still loaded local-only; nothing in the
product downloads a model, for the encoder either. A fresh install without
the two models in its Hugging Face cache answers lexical-only and says so
in the trace (`model_unavailable`, `reranker_unavailable`). Model
acquisition at install is the next gap and is recorded in
`docs/ISSUES-2026-09-10.md`, not fixed here.

**Files.** `scripts/reranker.py`, `scripts/mcp_server.py`,
`tests/test_reranker.py`, `tests/test_retrieval_review_blockers.py`,
`tests/test_retrieval.py`, `tests/test_warmup_records_a_warm_stage_cost.py`,
`docs/STRUCTURE.md`, `docs/USER-GUIDE.md`, `README.md`, `README.ru.md`,
`README.zh-CN.md`, `CHANGELOG.md`, `docs/ISSUES-2026-09-10.md`,
`knowledge/notes/default-reranker-decision.md` (private).

**Measured on the live vault after the change (2026-09-10, one warm
process, four cores, five questions with `rerank=True`).** Load 8.3 s once
(fp32 load plus int8 quantisation); the rerank stage itself 166–471 ms for
ten real passages — well under the 3.5 s the 2026-09-07 note measured for
twelve pairs under load, because this vault's fused candidates are mostly
short sections, not 512-token pages. Two things seen on the way are not the
reranker's: every search took about 34 s, inside the generation catalog's
artifact hashing (the nightly has been failing since 2026-09-07 and the
generation is stale), and two of three Russian questions returned no rows
at all. Both are recorded as open in `docs/ISSUES-2026-09-10.md`.

**Measured again at the MCP budget, after the generation fix (2026-09-10,
one warm process, load 1.5–2.6, six questions, `deadline` 10 s as the MCP
server grants it).** The 166–471 ms above was measured while the read path
still refused the generation and the fused pool held short lexical
sections; with the hybrid pool of real chunks the warm rerank stage costs
2.2–3.6 s for ten pairs at int8, and under load 5.8 it was 3.9–6.5 s. At
the MCP budget the stage was applied on four of the five questions it was
eligible for (the fifth was an exact-match bypass, which is correct); one
question hit `optional_stage_timeout` and answered from the fused order.
Whole answers took 5.2–6.5 s, inside the 10 s operation budget. The open
risk is that window: the optional share (`OPTIONAL_STAGE_BUDGET_SHARE`,
`OPTIONAL_STAGE_TAIL_RESERVE_SECONDS` in `scripts/retrieval.py`) leaves
about 3.5–4.5 s, so on a loaded machine the stage is started and abandoned
on most calls, costing CPU and returning nothing. Options, none chosen here
because each changes quality or the budget contract and needs its own
evidence: depth 5 (halves the cost; LongMemEval must show what it loses),
a larger optional share for the rerank kind only, or an ONNX int8 export of
the cross-encoder. Recorded in `docs/ISSUES-2026-09-10.md`.
