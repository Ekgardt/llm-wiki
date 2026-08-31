# Code parity, first pairing: llm-wiki against codebase-memory-mcp

Date: 2026-08-28. Backlog item `CODE-07`
(`docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md` §12). This is the completion
evidence the superset contract demands: `CLAUDE.md` §1 says completion
"requires real paired task, token, latency, safety, and operator-attention
evidence. A finished subplan or smoke fixture is not product completion."

The stand, the gold and the task list were built earlier the same day
(`benchmark/run_code_parity.py`, `benchmark/code-parity-v1.json`,
`tests/test_code_parity_stand.py`). This note is the run.

## Headline

| side | correct | partial | wrong | tokens | answered | refused |
|---|---|---|---|---|---|---|
| `llm_wiki` — the surfaces the stand was configured to call | **0 / 13** | 0 | 13 | 456 | 2 | 11 |
| `llm_wiki_best` — the surface that actually serves the question | **10 / 13** | 0 | 3 | 3 455 | 11 | 2 |
| `cbm` — codebase-memory-mcp | **10 / 13** | 0 | 3 | 2 385 | 13 | 0 |

Two findings dominate everything below.

1. **Ten of the thirteen `llm_wiki` failures are one defect**, not thirteen.
   `scripts/code_graph.py` fetches the whole call graph and filters it in
   Python, under a hard-coded `max_rows=10_000` that does not scale with the
   repository. This vault's active generation holds **35 313 `CALLS` edges and
   19 153 `function`/`method` nodes**, so the ceiling refuses. Named below.
2. **On answer quality the two products tie at 10/13, and they miss the same
   three tasks.** They do not tie on cost: on the ten tasks both answered
   correctly, llm-wiki spends **3.4× the tokens** cbm does (333.5 vs 98.4 mean).

## Was codebase-memory-mcp drivable? Yes

Required by rule 3 to be established rather than assumed. The evidence:

* Binary present and running: `/home/user/.local/bin/codebase-memory-mcp`,
  with a resident `--cbm-daemon-internal` process (`ps aux`).
* Registered for Claude sessions in `~/.claude.json:1472`; `claude mcp list`
  reports `codebase-memory-mcp: ✔ Connected`.
* `--help` documents `codebase-memory-mcp cli [--json] <tool> [args]`,
  "Run one tool locally, then exit" — the same tools the MCP server exposes.
* Driven for real: `cli --json list_projects '{}'` returns the project
  `home-user-llm-wiki` → `/home/user/llm-wiki`, branch `work`. stdout is clean
  JSON; the allocator warnings and the raw-JSON deprecation notice go to
  stderr, so `capture_output=True` separates them.

So the cbm column in this note is measured, not reconstructed. No fabrication
was needed and none was done.

## What was measured against

* Repository `/home/user/llm-wiki`, branch `work`, `HEAD 0a81af8`.
* `sha256(scripts/mcp_server.py)` = `0b6ab201…`,
  `sha256(scripts/code_graph.py)` = `3486db5c…`,
  `sha256(scripts/graph_query.py)` = `3c95d0b0…`. Re-hashed after the run and
  **unchanged**, so all three columns saw one code state. This mattered:
  concurrent agents edited `scripts/` during the session, and
  `scripts/mcp_server.py` grew between two reads minutes earlier.
* llm-wiki active generation `generation-18cfd903a7a4e112-3ce112cb`.
* cbm graph: 25 636→25 724 nodes, 140 727→141 100 edges — it moved during the
  session, because cbm watches and refreshes in the background.
* 4 vCPU, load average 4.4–6.2 — a busy machine shared with other agents.
* Deadline 60 s per call, 10 s kill grace.

## Per-task result

Grades are mechanical: word-boundary matching of the hand-established gold
terms. `tokens` is `len(answer)//4`, an approximation and not a tokenizer.

| id | question | gold citation | `llm_wiki` | `llm_wiki_best` (surface) | `cbm` (tool) |
|---|---|---|---|---|---|
| T01 | which production functions call `fuse_rrf`? | `scripts/retrieval.py:2845` in `_fused_candidates` (:2842) | wrong — ceiling | **correct** 3.5 s 433 tok — `query` | **correct** 2.1 s 44 tok — `trace_path` |
| T02 | which functions call `write_session_evidence`? | `flush_memory.py:1221`, `:1257`, `backfill_sessions.py:132` | wrong — ceiling | **correct** 3.3 s 546 tok — `query` | **correct** 2.7 s 87 tok — `trace_path` |
| T03 | which project functions does `fuse_rrf` call? | `retrieval.py:1400,1404,1414,1415,1418` | wrong — ceiling | **correct** 5.7 s 287 tok — `query` | **correct** 2.4 s 75 tok — `trace_path` |
| T04 | where is `_page_diverse` defined? | `scripts/retrieval.py:2765` | wrong — ceiling | **correct** 3.3 s 68 tok — `snippet` | **correct** 3.3 s 107 tok — `search_graph` |
| T05 | where is `EDITORIAL_NAMES` defined? | `scripts/vault_editorial.py:43` | wrong — ceiling | **wrong** 5.8 s 74 tok — `query` | **wrong** 5.3 s 219 tok — `search_graph`+`search_code` |
| T06 | is `_flush_started` dead? | `integration_adapter.py:1845`, no call site | wrong — `operation_failed` | **correct** 6.6 s 196 tok — `query` ×2 | **correct** 2.0 s 30 tok — `trace_path` |
| T07 | is `_search_backends` dead? | `search_memory.py:4831`, no call site | wrong — `operation_failed` | **correct** 7.0 s 193 tok — `query` ×2 | **correct** 2.0 s 31 tok — `trace_path` |
| T08 | which modules import `page_status`? | `corpus_snapshot:22`, `search_memory:55`, `rebuild_memory_index:24`, `archive_stale:36`, `page_facts:22`, `lint_memory:61` | **wrong, answered** `[]` | **correct** 3.1 s 358 tok — `query` | **correct** 2.2 s 235 tok — `query_graph` |
| T09 | what does `scripts/retrieval.py` depend on? | `retrieval.py:16` → `provenance` | **wrong, answered** `[]` | **correct** 3.3 s 358 tok — `query` | **correct** 2.2 s 282 tok — `query_graph` |
| T10 | what reaches `fuse_rrf` within two caller hops? | hop 1 `_fused_candidates` :2845; hop 2 `retrieve` :3007 | wrong — ceiling | **correct** 7.3 s 672 tok — `query` ×2 | **correct** 2.1 s 47 tok — `trace_path depth=2` |
| T11 | which retrieval function calls `source_type_weight`? | `retrieval.py:1137` in `_weigh_by_trust` (:1121) | wrong — ceiling | **correct** 3.4 s 224 tok — `query` | **correct** 2.3 s 46 tok — `trace_path` |
| T12 | summarize the architecture | any credible summary names `mcp_server` | wrong — ceiling | **wrong** — ceiling | **wrong** 2.9 s 815 tok — `get_architecture` |
| T13 | which community does `fuse_rrf` belong to? | the retrieval cluster (`retrieval.py:1378`) | wrong — ceiling | **wrong** — ceiling | **wrong** 2.4 s 367 tok — `get_architecture clusters` |

I re-verified the gold by hand rather than trusting the file: `grep -rn
fuse_rrf scripts/` returns exactly one call site (`:2845`); exactly six modules
import `page_status`; `retrieval.py` has exactly one module-level project
import (`provenance`, line 16); neither `_flush_started` nor `_search_backends`
has any call site. One correction to the gold's wording: its "grep returns only
the definition" is now stale, because the stand's own files name both symbols —
the deadness claim is unaffected, since none of those mentions is a call.

## Why `llm_wiki` scored 0/13 — one defect, named

`scripts/code_graph.py:1268`, in `_store_find_callers`:

```python
edges = graph.edges(edge_types=("CALLS",), max_rows=10_000)
```

It pulls **every** `CALLS` edge in the graph and filters in Python. With 35 313
such edges, `EvidenceGraph._execute` raises
`ValueError("Evidence Graph query row ceiling exceeded")`. The same shape
appears at `:1265`, `:1335`, `:1339`, `:1413`, `:1414`, `:1417`, `:1532`,
`:1533`, `:1536`, `:1538`, `:1639`, `:1660`.

Traced, not guessed — the reproduced stacks:

* `callers` / `callees` / `symbol` (`symbol` calls `_architecture_callers`
  internally) → `code_graph.py:1268`.
* `summary` → `code_graph.py:1536`, `find_nodes(kinds=("function","method"),
  max_rows=10_000)` against 15 695 functions + 3 458 methods = 19 153.
* `find_dead_code` → `code_graph.py:1413`, same node query. It surfaces as the
  opaque `{"error": "operation_failed"}`, which does not name the ceiling.

The refusal is honest — nothing is silently truncated — but the ceiling is a
constant where the repository is a variable, so it fails on exactly the
repositories worth indexing. This is a defect report, not a patch: `scripts/`
belongs to other agents in this session and I did not touch it.

The `query` mode added 2026-08-28 (`scripts/graph_query.py`) does not have the
defect, because it starts from a bounded `find_nodes` and expands through
`neighbors` with per-hop ceilings instead of loading the graph. That is why
`llm_wiki_best` exists as a third column: reporting only the broken surface
would understate the product, and reporting only the working one would hide a
defect that ships today.

## The four contract dimensions

### Correctness

`llm_wiki_best` 10/13, `cbm` 10/13, `llm_wiki` 0/13. The two products miss the
**same three** tasks, and for different reasons worth separating:

* **T05** (`EDITORIAL_NAMES`) — both graphs index functions, methods, classes,
  files and modules, and **neither indexes module-level constants**. llm-wiki
  returns an empty node list; cbm's `search_graph` returns `total: 0` and its
  `search_code` fallback returns five *reference* sites in `lint_memory.py` and
  `corpus_snapshot.py` while the definition in `vault_editorial.py:43` never
  appears. A shared blind spot, not a llm-wiki weakness.
* **T12 / T13** — llm-wiki refuses (the ceiling). cbm answers, and answers
  wrongly: its summary never names `mcp_server`, and its 12 clusters are
  labelled `scripts` ten times over, placing nothing.

### Tokens

Measured on the ten tasks **both sides answered correctly**, so the comparison
is paired and not skewed by failures:

| | total tokens | mean per task |
|---|---|---|
| `llm_wiki_best` | 3 335 | 333.5 |
| `cbm` | 984 | 98.4 |

llm-wiki costs **3.4×** cbm for the same fact. The largest single contributor
is mechanical: **20.6 % of llm-wiki's answer bytes are opaque `node_id`
hashes** (`"node_id": "code:node:<32 hex>"`), which no caller uses. Dropping
that one field alone takes the 13-task total from 3 455 to 2 746 tokens. cbm
answers in a compact tabular form with a header line naming the columns;
llm-wiki answers in full JSON records. For a product whose own contract
requires it to "spend tokens frugally in operation" (`CLAUDE.md` §1), this is
the cheapest available win and it is not a graph problem.

### Latency

The wall times in the table are **cold-process** times and should not be read
as in-session latency. Both harnesses pay a fresh process per call:

* cbm CLI floor ≈ **2.07 s**, measured identically across three different tools
  (`trace_path` 2.10 s, `search_graph` 2.09 s, `get_architecture` 2.07 s) — so
  the graph work itself is below this harness's resolution. The one cbm tool
  that self-reports agrees: `search_code` says `elapsed_ms: 22–33`.
* llm-wiki subprocess floor ≈ 2.0 s of interpreter start and import.

Measured resident instead, running all 16 `llm_wiki_best` calls inside one
process: `import mcp_server` costs 0.58 s once, then **1.35 s mean per call**
(16 calls, cold pass) and 1.60 s on a second pass under rising load — i.e. the
real in-session cost is roughly a third of the 3–7 s the table shows. The
equivalent number for cbm cannot be isolated from a script: its daemon is
already resident, but the CLI that talks to it is not, so its ~2 s floor hides
however little the daemon actually spends. **Neither product's true in-session
latency is established by this run**; what is established is that llm-wiki's
resident per-call cost is ~1.35 s and cbm's daemon-side cost is at most a small
fraction of 2 s.

### Safety — defined here as the confidently wrong answer

The dangerous failure is not the refusal; it is the answer a reader would act
on that is false. Three grades, applied to every wrong answer:

* **refused** — no answer, named error. Useless, but nobody is misled.
* **flagged-wrong** — wrong, but carrying a machine-readable caveat about its
  own completeness.
* **silent-wrong** — wrong, delivered with no caveat, formatted exactly like a
  correct answer.

| side | refused | flagged-wrong | silent-wrong |
|---|---|---|---|
| `llm_wiki` | 11 | 2 | **0** |
| `llm_wiki_best` | 2 | 1 | **0** |
| `cbm` | 0 | 0 | **3** |

The two `llm_wiki` flagged-wrong answers are T08/T09: `dependencies: []`, the
false answer, but shipped alongside `graph_complete: false` and
`unresolved_count: 76044`. A caller checking that field knows not to trust the
empty list. The `llm_wiki_best` one is T05: `nodes: []` next to
`frontier_truncated: false` and `refused_expansions: []` — bounded and honest
about *the graph*, though a reader could still take it as "the constant does
not exist". The caveat is about truncation, not about corpus scope, and that
gap is worth closing.

cbm's three are the dangerous kind. Its T12 summary is a complete-looking
statistical portrait — node labels, edge types, languages — that never names
the repository's documented entry point. Its T13 clusters are confident,
cohesion-scored, and labelled `scripts` ten times out of twelve. Its T05 ranks
five reference sites above a definition it never found. Nothing in any of the
three says "this may be incomplete".

**The honest qualifier, stated plainly: llm-wiki's zero is bought with
refusals.** It is safe on this stand largely because it declined to answer 13
of 29 calls. A tool that refuses cannot mislead, and it also cannot help. The
defensible claim is narrow: *where llm-wiki answers, it has not yet been caught
answering wrongly without saying so* — 11 answers, 10 correct, the one miss
flagged. That is a 29-call sample and it is not a safety guarantee.

### Operator attention

Defined as work the operator or agent must do that the tool did not: calls
issued, plus fallbacks forced by a non-answer.

| side | calls for 13 tasks | non-answering calls | fallbacks forced |
|---|---|---|---|
| `llm_wiki` | 14 | 11 | 11 |
| `llm_wiki_best` | 16 | 2 | 2 |
| `cbm` | 14 | 0 | 0 |

Every non-answer sends the operator to `grep`. Beyond the count, two structural
costs that the numbers do not show:

* llm-wiki needs **two calls** where cbm needs one, twice. `trace_path`
  `depth=2` returns both hops; `graph_query` hops return only the final
  frontier, so reaching two hops takes a 1-hop call and a 2-hop call (T10). The
  dead-code questions (T06/T07) likewise need an existence call plus a
  callers call, because the query mode has no negation by design and the
  product's own `find_dead_code` is the tool broken by the ceiling.
* Choosing the surface is itself operator attention. `llm_wiki_best` scores
  10/13 only because a human worked out that `query` and `snippet` answer what
  `callers`, `callees`, `symbol`, `summary` and `find_dead_code` refuse. An
  agent reading the tool schema would have called the broken ones — the stand's
  first column *is* that agent, and it scored 0.

## A reproducibility finding about cbm

cbm's `get_architecture` is **not deterministic across runs on an unchanged
repository**. T12 graded `correct` in the first run and `wrong` in the second:
the summary named `mcp_server` once and not the second time. Five further
`get_architecture` calls returned 3 262 bytes ×4 and 3 269 bytes ×1, none
naming `mcp_server`; the first run's answer was 3 276 bytes. Three distinct
lengths, no source change.

The cause is visible in cbm's own output: its node count moved from 25 636 to
25 724 and its edges from 140 727 to 141 100 during this session, because it
watches and re-indexes in the background. That is a real feature with a real
cost — a benchmark against cbm cannot assume a stable index, and neither can a
user comparing two answers an hour apart. llm-wiki has the opposite property by
design: answers name their immutable generation
(`generation-18cfd903a7a4e112-3ce112cb`) and are stable until one is published.

The two graphs are also not the same object and should not be compared
node-for-node: cbm extracts 22 edge types (including `USAGE`, `WRITES`,
`TESTS`, `DECORATES`) and 48 193 `CALLS`; llm-wiki extracts 9 edge types and
35 313 `CALLS`. Different extraction, not a completeness verdict either way.

## Limits — what this run does not establish

* **One repository, one machine, one day.** 13 tasks on llm-wiki itself, on a
  4 vCPU box at load average 4.4–6.2 shared with other agents. Nothing here
  generalizes to other languages or other repositories.
* **13 tasks is a small sample.** A single task moves any rate by 7.7 points.
  The 10/13 tie is a tie at this size; it is not evidence of equality.
* **Grading is mechanical string matching**, not comprehension. It cannot see a
  right answer phrased unexpectedly, and a bare gold number could in principle
  collide with an unrelated number. Every answer is stored in
  `benchmark/code-parity-*.json` so any such collision is auditable.
* **`tokens` is `len(text)//4`.** Real tokenizer counts will differ.
* **Latency is cold-start dominated** and is not in-session latency; cbm's
  in-daemon cost was not isolated at all.
* **T12/T13 have soft gold.** "Any credible summary names `mcp_server`" and
  "names the retrieval cluster" are weaker criteria than a file:line citation.
  Both sides fail them, so the tie does not turn on them, but they are the two
  tasks where a grader could reasonably disagree.
* **The `llm_wiki` column measures surfaces, not the product's ceiling**, and
  the `llm_wiki_best` column measures a human's surface choice, not what an
  agent would do unaided. Both are stated; neither alone is the product.
* **No task in this stand exercises the `provenance` or `coverage` modes.**
  They exist and dispatch; their parity is unmeasured.

## What this closes and what it does not

`CODE-07` asked for a real paired run with task, token, latency, safety and
operator-attention evidence. All five are here, measured, for 13 tasks against
a drivable cbm. That much is done.

It does not yet support retiring codebase-memory-mcp. On this stand llm-wiki
matches it on correctness only through a surface an agent would not pick by
itself, costs 3.4× the tokens, and needs two calls where cbm needs one — while
five of its documented `get_architecture` modes and `find_dead_code` refuse
outright on a repository of this size. The `max_rows=10_000` ceiling in
`scripts/code_graph.py` is the single highest-value fix the run identified, and
the `node_id` field is the cheapest.

## Sources

* Run artifacts: `benchmark/code-parity-first-pairing-2026-08-28.json`
  (three columns) and `benchmark/code-parity-asconfigured-2026-08-28.json`
  (the first, two-column run whose T12 disagreement exposed cbm's
  non-determinism).
* Task list and hand gold: `benchmark/code-parity-v1.json`.
* Stand: `benchmark/run_code_parity.py`; tests
  `tests/test_code_parity_stand.py`.
* Gap: `docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md`, `CODE-07`.
* The query mode measured here: `scripts/graph_query.py`, decided in
  `docs/research/2026-08-28-bounded-graph-query-mode.md`.
