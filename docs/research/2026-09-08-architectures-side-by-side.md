# Every architecture side by side — why ours loses on their stand, and what it wins — 2026-09-08

Owner's question: our architecture was built for reasons; before changing it,
lay every architecture and its logic out, ours and theirs, with the pluses
and minuses, thoroughly.

Sources read today: Mem0 (arXiv:2504.19413), Zep/Graphiti (arXiv:2501.13956),
LongMemEval (arXiv:2410.10813), Mastra Observational Memory
(https://mastra.ai/research/observational-memory), OMEGA
(https://omegamax.co/blog/number-one-on-longmemeval), Mem0's public stand
(https://github.com/mem0ai/memory-benchmarks), Supermemory
(https://supermemory.ai/research/longmembench/), MemGPT (arXiv:2310.08560),
and our own `docs/ARCHITECTURE.md` with its 55 decision pages.

## 1. What each system actually is

**LLM Wiki (ours).** Authority is Markdown in git: raw session captures as
daily entries (one append per event, zero LLM calls at capture), compiled
pages written later in the background, project journals, a claim ledger.
Derived state — FTS5, vectors, tiers, graph — lives in immutable "generations"
rebuilt nightly. A question retrieves twelve session-entry chunks (about
10 KB each), packs them into a 122k-byte window, and the model must answer as
JSON claims, each citing a byte-range span that is then verified by hash and
word overlap; a claim that fails is dropped, and with none left the answer is
a named refusal. Everything is local, no daemon, no paid API, and every write
is a recoverable transaction.

**Mem0.** At ingest, two LLM calls per message pair: extract salient facts in
natural language (using a rolling summary and the last ten messages), then
for each fact retrieve the ten most similar stored facts and let an LLM
choose ADD / UPDATE / DELETE / NOOP. Storage is a vector index of facts
(~7k tokens per conversation against 26k raw). Retrieval is top-k facts by
embedding; the answer model gets the facts as context. On LongMemEval their
public stand retrieves **top-200 facts** and answers with gpt-4o; 94.4%.
Stated weakness: the graph variant is slower and worse on single- and
multi-hop; DELETE can silently remove a fact still needed.

**Zep / Graphiti.** Three-layer temporal graph: episodes (raw, lossless),
entities and relations extracted and deduplicated by several LLM calls per
message, communities summarised above them. Every edge carries four
timestamps — valid/invalid in the world, created/expired in the system — so a
superseded fact is invalidated, never deleted. Search is cosine + BM25 + graph
walk, fused by RRF, reranked by MMR, mention counts, distance or a
cross-encoder; the context is about 1.6k tokens of facts with validity
ranges. LongMemEval 71.2% (GPT-4o); weak on single-session-assistant (−9 to
−18 points), heavy ingest, Neo4j required.

**Mastra Observational Memory.** No retrieval at all. Two background agents
watch the conversation: an Observer turns messages into dated, prioritised
observations (a compressed event log, 3–6× smaller than raw); a Reflector
condenses observations when they exceed a threshold. The whole log sits at
the front of every prompt (~30k tokens on LongMemEval), so the cache is warm
and nothing is ever "not retrieved". 94.87% with gpt-5-mini; multi-session
87.2% "an apparent ceiling"; the cost is 30k tokens on every turn and the
observer's own model calls.

**OMEGA.** Local, one SQLite file, ~31 MB footprint: session chunks with
FTS5 and bge-small vectors, hybrid search, type weighting, semantic
reranking, dedup, time decay; queries are expanded with temporal context,
synonyms and inferred entities (recovers 3–5% of questions); **five prompts,
one per LongMemEval question category**; GPT-4.1 answers. 93.2% plain,
95.4% task-averaged. No evaluation code published; abstention handling not
described.

**Supermemory.** A learner model extracts "learnings" into a vector-graph
store at ingest; recall@15 puts ~720 tokens into the prompt; 95% with GPT-4o,
84.6% with GPT-5. Pipeline details are not published beyond that.

**Letta / MemGPT.** The agent manages its own memory like an OS: a small
core memory in context it edits by tool calls, a recall store of history,
an archival vector store; interrupts when the window fills. Zep could not get
it to answer LongMemEval at all; LoCoMo 0.740.

## 2. The same dimensions for everyone

| Dimension | LLM Wiki | Mem0 | Zep | Mastra | OMEGA | Supermemory |
|---|---|---|---|---|---|---|
| Unit stored | raw session entries + compiled pages + claims | NL facts | episodes + entity/edge graph + communities | dated observations | session chunks | extracted learnings |
| LLM calls at ingest | 0 (compile later, background) | 2 per message pair | several per message | observer + reflector, by threshold | 0 | extraction |
| Retrieval unit | 12 chunks × ~10 KB | top-200 facts | ~1.6k tokens of facts | none — all in context | hybrid top-k chunks | 15 memories |
| Context per question | 122k bytes budget, ~13k tokens used | large (200 facts) | 1.6k tokens | ~30k tokens | undisclosed | ~720 tokens |
| Answer policy | verified claims, cited, or a named refusal | always answers | always answers | always answers | always, 5 prompts by category | always answers |
| Time | dates resolved at write; valid_from/to on pages; bitemporal claims library | timestamps on facts | bi-temporal edges | dates on observations | time decay + query expansion | temporal-aware, undisclosed |
| Updates | supersede pages; claim lifecycle | ADD/UPDATE/DELETE by LLM | invalidate edges | reflector rewrites | dedup | undisclosed |
| Provenance | byte-range citations verified by hash | none | episode links | none | none | none |
| Local, no daemon | yes | self-host with DB, or cloud | Neo4j | framework, remote models | yes, SQLite | cloud / self-host |
| Search latency | 7–8 s (reranker) | 0.2 s p95 | 189 ms | none | <50 ms | <300 ms |
| LongMemEval | 0.874 answered / 0.750 all | 0.944 | 0.712 | 0.949 | 0.932 / 0.954 | 0.95 |

## 3. What the benchmark authors found matters (arXiv:2410.10813)

- **Granularity.** Splitting sessions into rounds helps; compressing further
  into summaries or facts *hurts* except for multi-session questions — raw
  text keeps what facts lose.
- **Key expansion.** Indexing each round under its extracted facts *as well
  as* its text: +9.4% recall@k, +5.4% accuracy. Facts as keys, text as value.
- **Time-aware query expansion.** Letting the model extract the time range a
  question asks about and filtering sessions by it: +6.8–11.3% on temporal
  questions.
- **Reading.** Chain-of-note plus JSON output: up to +10 points.
- What a memory needs: multi-path retrieval, explicit time, a good reading
  strategy, fine-grained values.

Ours already has multi-path retrieval, JSON reading and explicit time. It
lacks round-level values and fact keys, and it packs whole 10 KB entries.

## 4. Where our losses come from, measured (run 1, 186 answerable)

- The right session is among the twelve candidates for **98%** of questions
  and first for 89% — retrieval of the *session* is not the problem.
- The text that carries the answer reaches the model for only **55%** — the
  chunk boundary and the tail-shedding packer lose it. With the answer in
  the prompt we are right 83%; without it 64%.
- We are silent on 15% of answerable questions; 10 of those 28 had the answer
  in the prompt and a gate dropped the claim.
- Every one of those silences is a wrong answer on their metric; none of
  the six competitors ever refuses.

So: not the model, not session retrieval; the value granularity between
them and the answer contract's strictness. Both are ours to change without
touching what the architecture is for.

## 5. Pluses and minuses, honestly

**Ours — pluses.** Zero-cost capture (no LLM at write); the raw record is
lossless and human-readable; every answer is checkable to the byte and the
refusal is a first-class outcome (11/12 correct abstentions, 90% on
LIT-RAGBench, 67% on RefusalBench-NQ — numbers no competitor publishes);
supersession never deletes; local, no daemon, no vendor; transactions,
undo, DLP; one memory for many agents and projects. These are exactly the
properties a person managing many agents needs and the benchmark cannot see.

**Ours — minuses.** Values are 10 KB session entries, not rounds; no fact
keys beside the text; the packer sheds by rank and loses the answer; the
answer contract refuses where the benchmark wants a guess; search is
7–8 s because a cross-encoder runs at query time on long chunks; ~13k tokens
per question; compile is nightly, so today's facts are in raw entries only.

**Mem0 — pluses.** Facts are small, fast, cheap to read; ADD/UPDATE/DELETE
keeps the store current; top-200 facts make recall high. **Minuses.** Two
LLM calls per message forever; extraction loses what it did not think
salient; DELETE is silent; no citation, no refusal; the 94.4% is with 200
facts in the prompt and gpt-4o answering, not the 7k-token production shape.

**Zep — pluses.** The best-modelled time; invalidation with history;
episodes kept lossless; graph search for multi-hop. **Minuses.** Heaviest
ingest; Neo4j; worst LongMemEval of the group; entity resolution errors
compound.

**Mastra — pluses.** Nothing is ever not retrieved; prompt caching makes
30k tokens cheap per turn; the observation log is readable. **Minuses.** 30k
tokens on every turn and growing until reflected; a reflector rewrite is a
lossy step with no undo; no citations; multi-session ceiling 87%.

**OMEGA — pluses.** Local and fast; category-specific prompts and query
expansion are cheap wins. **Minuses.** Unpublished stand; task-averaged
headline; no provenance; no refusal.

**Supermemory — pluses.** 720 tokens for 95% is the best token efficiency
published. **Minuses.** Pipeline undisclosed; 84.6% with GPT-5 shows the
number depends on the reader; cloud.

## 6. What follows

Keep: Markdown authority, zero-LLM capture, verified citations, named
refusal, supersession, local-first, transactions. None of the six has these
and the owner's use — many agents, private projects, answers that can be
checked — is what they are for.

Adopt, in this order, each measured against the 0.035 spread:

1. **Round-level values with whole-session delivery.** Index rounds (the
   authors' CP1), but when a session is selected, give the model the whole
   session entry rather than one shed chunk — twelve sessions fit the window.
   Targets the 45% of questions whose answer never reached the model.
2. **Fact keys beside the text** (CP2, +9.4% recall): at compile, extract
   short facts per round and index them as additional keys pointing at the
   raw span. Raw stays the value; nothing is lost; capture stays free.
3. **Time-aware query expansion** (CP3): we already resolve the question's
   dates; add the extracted time range as a filter on sessions.
4. **Answer policy as a mode.** The product keeps refusal. The stand adds a
   `--policy answer` arm where a claim that fails its gate is reported as an
   uncited best answer instead of dropped — so the number the field compares
   is measured, and the refusal number stays measured beside it.
5. **Category-shaped prompts** (OMEGA): the declared derivation already tells
   us the shape; a prompt per shape is cheap.

Not adopting: LLM extraction at capture (cost on every message, silent
loss), whole-history-in-context (30k tokens per turn), a graph database, a
daemon, any paid API.

## 7. Each approach on a real question — where it wins, where it loses

**LLM Wiki.** *Wins:* "Which port did we pick for the MCP server in March,
and why?" — the answer cites the bytes of the daily entry; the owner opens
the file and sees the sentence. "Do I use Redis?" when nothing was ever
said — a named refusal, not a guess. *Loses:* "How many times did I go to
the gym?" — the gym sessions sit in five entries, the packer sheds the
fifth, the count is short; or a gate rejects the only claim and we are
silent where any guess would have scored.

**Mem0.** *Wins:* "What is my favourite coffee?" — one stored fact, 0.2 s,
7k tokens. *Loses:* "I moved to Berlin" then "I'm in Berlin for a week" —
the ingest LLM may UPDATE or DELETE the wrong fact and nobody can check;
the library version mentioned in passing was not "salient" and is gone.

**Zep.** *Wins:* "Where did I work in 2024?" — an edge with valid-from and
valid-to; "who worked with whom on project X" — graph hops. *Loses:*
"What did the assistant recommend for my recipe?" — assistant-side text is
poorly extracted (−9 to −18 points); every message costs several LLM calls
and a Neo4j.

**Mastra.** *Wins:* a long day with one agent — "what did we decide an hour
ago" is always in the window, cached, nothing was ever missed. *Loses:*
79 projects — 30k tokens on every turn of every one; after a reflector pass
the detail is condensed away with no undo and no citation.

**OMEGA.** *Wins:* "How many weeks between my two trips?" — temporal
expansion and a temporal prompt; local and <50 ms. *Loses:* asked about
something never said, it answers anyway; no citation to check; nobody can
re-run its stand.

**Supermemory.** *Wins:* thousands of cheap questions — 720 tokens each.
*Loses:* the number fell ten points with a stronger reader, which says the
reader was compensating for recall; the pipeline is a cloud box.

**Letta.** *Wins:* an agent that keeps its own persona and working notes and
edits them. *Loses:* the agent must decide what to save; on LongMemEval it
could not be made to answer at all.
