# Token-budgeted answers: what a code answer may drop, and what it must say

Date: 2026-08-28. Backlog item `CODE-06`
(`docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md` §12). This note is the research
that has to exist before the answer path is reshaped, and it exists because of
a number the product measured about itself the same day.

## The measurement that forces this

`docs/research/2026-08-28-code-parity-first-pairing.md` (`CODE-07`, commit
`837743c`) paired llm-wiki against codebase-memory-mcp on 13 code tasks. On the
**ten tasks both sides answered correctly** — a paired comparison, not one
skewed by failures — llm-wiki spent **3.4× the tokens** cbm did (mean 333.5 vs
98.4). The note named the largest mechanical contributor: **20.6 % of
llm-wiki's answer bytes were opaque `node_id` hashes**, `code:node:<32 hex>`,
which no caller uses.

Re-measured today on the current working tree (baseline run, `HEAD 008876c`,
`sha256(scripts/mcp_server.py) = 0b6ab201…`), after another agent's `NEW-121`
work unblocked the `callers`/`callees` modes:

| side | answer chars over 13 tasks | opaque-id bytes | share | rows carrying one |
|---|---|---|---|---|
| `llm_wiki` (configured surfaces) | 13 409 | 2 124 | **15.8 %** | 36 |
| `llm_wiki_best` (`query` / `snippet`) | 13 834 | 2 850 | **20.6 %** | 50 |

The 20.6 % reproduces the `CODE-07` figure exactly. The new fact is that the
`llm_wiki` column now pays the same tax under a different key: the `callers`
mode emits `symbol_id` whose value is the same `code:node:<32 hex>` string. A
rule keyed on the field *name* would have caught one of the two. A rule keyed
on the *value shape* catches both, which is why the implementation is written
that way.

A second, smaller redundancy is visible in the same data: `owner` costs
**11.3 %** of the `llm_wiki_best` bytes and is derivable from `path`
(`scripts/retrieval.py` → `scripts.retrieval`) — and in the `dependencies`
answers it is `null` on every row.

This is not a graph problem and not a correctness problem. It is answer shape,
and `CLAUDE.md` §1 requires the product to "spend tokens frugally in
operation".

## What the competitors do

**Graphify** advertises token-budgeted subgraphs rather than raw query output.
Read first-hand from its README today
([Graphify-Labs/graphify](https://github.com/Graphify-Labs/graphify)), the
budget appears as an explicit caller-supplied number in three places:

> `graphify extract ./docs --token-budget 30000   # smaller semantic chunks for local/small models`

> `/graphify query "..." --dfs --budget 1500`

> `GRAPHIFY_MAX_OUTPUT_TOKENS=32768 graphify extract ./docs --backend claude  # raise output cap for dense corpora`

Stated honestly, because the market note
(`docs/research/2026-08-27-number-one-memory-market-research.md`) is the reason
Graphify is the comparator: the README documents a budget *flag* on extraction
chunking and on query traversal; it does **not** publish how many tokens its
MCP tools (`query_graph`, `get_node`, `get_neighbors`, `shortest_path`, …)
return, and it does not document what happens to an answer that exceeds the
budget. So the surface is real and the number is caller-supplied; the
fail-closed behaviour below is not copied from Graphify, because Graphify does
not describe one.

**codebase-memory-mcp** has no token argument at all, and still wins the token
comparison — it pays the cost differently, by shipping a compact tabular form
with one header line naming the columns instead of full JSON records per row.
Its `search_graph` tool description, read from the live tool schema on this
machine, offers the same idea as a shape enum rather than a count:

> `detail` — "ids: bare qualified-name enumeration (one column) — cheapest form for wide sweeps where per-row metadata is noise. default: full rows."

So both competitors let the caller ask for less. Neither gives the caller a
guarantee about what happens when less is not enough.

## External practice on context-budget shaping

Anthropic's engineering guidance for tool authors is the closest thing to a
specification for the consumer that actually reads these answers, since the
consumer here is Claude Code
([Writing effective tools for agents, Anthropic](https://www.anthropic.com/engineering/writing-tools-for-agents)).
Four points from it bear directly on this design:

1. There is a hard ceiling in the client: "We restrict tool responses to 25,000
   tokens by default". A budget argument on our side is therefore bounded by a
   real number, not an invented one — 25 000 is the maximum a budget can
   usefully name.
2. The recommended mechanism is "some combination of pagination, range
   selection, filtering, and/or truncation with sensible default parameter
   values".
3. On identifiers specifically, it says to return `name`, `image_url`,
   `file_type` in preference to `uuid`, `256px_image_url`, `mime_type` — i.e.
   the opaque machine identifier is the field to drop first. That is exactly
   the `node_id` finding, arrived at independently by measurement.
4. Where downstream calls genuinely need the technical identifiers, the advice
   is to keep both behind a flag: "expos[e] a simple `response_format` enum
   parameter in your tool, allowing your agent to control whether tools return
   `"concise"` or `"detailed"` responses". That is the shape of the
   `include_node_ids` opt-in below.

The wider write-up of the same problem reports that clients disagree on the
limit — Claude Code rejects results above 25 000 tokens, Claude Desktop uses
roughly a 150 000-character cap, and Cursor and VS Code Copilot document no
clean number and instead truncate or degrade
([MCP Output Too Large, Morph](https://www.morphllm.com/mcp-output-too-large),
found via search; the page itself returned HTTP 429 when fetched, so those
per-client numbers are reported, not verified here). A related survey of
patterns for oversized results is
[Extending ResourceLink (arXiv 2510.05968)](https://arxiv.org/pdf/2510.05968).

**One search result I checked and could not confirm.** A search summary
asserted that MCP specification 2025-03-26 introduced `_meta.truncated` to
signal a truncated response. I fetched that spec page
([MCP 2025-03-26, Tools](https://modelcontextprotocol.io/specification/2025-03-26/server/tools))
and read it: it defines tool results as content items (text, image, audio,
embedded resource) plus `isError`, and it contains **no size limit and no
truncation-signalling field** of any kind. Its only nearby advice is that
clients should "validate tool results before passing to LLM". Recorded as a
correction rather than repeated, per rule 3.

That negative result is load-bearing for the design: **the protocol carries no
place to say "this answer was cut".** If the answer body does not say it,
nothing does.

## Why the budget must fail closed

The `CODE-07` safety table graded every wrong answer as *refused*,
*flagged-wrong*, or *silent-wrong* — wrong, delivered with no caveat, formatted
exactly like a correct answer. llm-wiki scored **0 silent-wrong** across 29
calls; cbm scored 3. That zero is the product's one measured advantage over the
comparator, and the note is explicit that it is "bought with refusals".

A token budget is a truncation mechanism. A budget that dropped rows quietly
would spend exactly that advantage to buy back tokens — it would manufacture
silent-wrong answers on purpose, on the surface where the product is currently
clean. So:

* Any answer that lost rows carries `truncated: true` and the count of what was
  lost, in the answer body, where the protocol has no field for it.
* A budget too small to hold even the answer's frame is a **named refusal**
  (`answer_budget_too_small`, stating the budget and what the frame costs), not
  a shortened answer. A caller that asked for 20 tokens gets told no; it does
  not get three rows and a false impression of completeness.
* Dropping a field is a schema decision that applies to every answer, so it is
  named once per answer (`omitted_fields`) rather than argued per row.

This continues the line already in the module it wraps: `scripts/graph_query.py`
turns each engine ceiling into a named `refused_expansions` entry precisely
"so nothing is truncated silently".

## The decision

A new module `scripts/answer_budget.py`, applied at the `scripts/mcp_server.py`
boundary for `get_architecture` and `find_dead_code` only. It is applied at the
boundary and not in `scripts/code_graph.py` or `scripts/graph_query.py` because
those files belong to other agents in this session (`NEW-121`); shaping the
answer where it leaves the product is in any case the right layer, since it is
the only layer that sees every mode's answer in one shape.

**Opacity is decided by value, not by key name.** A field is opaque when its
value is a graph identifier of the form `code:<kind>:<32 hex>` — the form
`scripts/code_extractor.py:226` mints. This catches `node_id` in the `query`
mode and `symbol_id` in the `callers`/`callees` modes with one rule, and it
deliberately spares the *readable* `symbol_id` the live dead-code fallback
emits (`scripts.module::function`, from `_assign_symbol_id`), which carries
module and name and is not a hash.

**The reduction ladder, in order — least informative first:**

1. **opaque identifiers** — fields whose value is `code:<kind>:<hash>`.
   Dropped by default; restored by `include_node_ids: true`.
2. **derivable fields** — `owner`, recoverable from `path`/`file`. Dropped only
   under budget pressure.
3. **rows** — trimmed from the tail of the longest row list. Only under budget
   pressure, and always announced.

Never dropped at any step: `path`, `file`, `line`, `name`, `function`,
`qualified_name`, `symbol`, `status`, `error`, and the graph-honesty fields
`source_generation`, `graph_complete`, `unresolved_count`, `fallback`,
`frontier_truncated`, `refused_expansions`. The hash goes before the
`file:line`, which is the citation the answer exists to deliver, and before the
fields the envelope's own quality scoring reads.

`refused_expansions[].refused_node_ids` is **kept** even though it holds the
same opaque strings. It falls outside the rule naturally — it is a list value,
not a scalar field — and that is the intended outcome: it appears only when an
expansion was actually refused, and it is the evidence for the refusal. Paying
for it is paying for the fail-closed guarantee.

**Two new arguments, both optional:**

* `budget_tokens` (integer, 32…25 000) — absent by default. 25 000 is the
  client ceiling quoted above; 32 is below the cost of any real answer frame,
  so the refusal path is reachable and testable.
* `include_node_ids` (boolean, default `false`) — the `response_format`-style
  escape hatch.

**Is there a consumer of `node_id`? Established, not assumed.** The evidence:

* `scripts/graph_query.py` accepts a start filter of `name`, `path`, `kind`
  only (`_START_KEYS`) — a node id returned by the `query` mode **cannot be
  fed back into the query mode**.
* No MCP tool input schema in `scripts/mcp_server.py` accepts a node or symbol
  identifier as an argument.
* `grep` over `tests/`, `benchmark/`, `skills/`, `rules/`, `integrations/`,
  `docs/*.md` finds no reader of an *answer's* `node_id`/`symbol_id`. Every
  hit is either inside the graph layer's own tests (`test_evidence_graph_*`,
  `test_code_extractor`, `test_graph_query`'s fake graph) or inside
  `scripts/code_graph.py`'s internal dedup and edge keys — all of which run
  **before** the answer reaches this boundary and are untouched by shaping it.

So no consumer exists today. `include_node_ids` therefore ships not for a
consumer but for the operator debugging a generation by hand, and for whatever
future surface takes an identifier as input — and the default is `false`
because a field with no reader is pure cost.

**Token estimate.** `len(json) // 4`, the same approximation
`benchmark/run_code_parity.py` uses, so the budget and the stand measure the
same quantity. It is not a tokenizer, and the module says so where a caller
will read it. Anthropic ships `messages.count_tokens` for the real number, but
calling it would put a network round trip and an API key inside a local,
offline, zero-cost answer path — the vault's contract forbids that
(`CLAUDE.md` §6, "no paid API beyond existing agent subscriptions"). An
approximation that never fails is the right trade for a budget whose job is to
bound, not to bill.

## What it measured

**The stand's before/after is not the measurement, and here is why.** The
baseline run finished at `sha256(scripts/code_graph.py) = 430e3c50…`; the
second run saw `7a80457e…`, with `scripts/evidence_graph.py` newly modified as
well. Another agent's `NEW-121` work landed between them, unblocking
`mode=summary` and `mode=community`, and it moved `T12` from a refusal to a
208 786-token answer. Comparing those two runs would credit this change with
another task's effect and with a 12 000 % token *increase*. Discarded.

The paired measurement instead calls every task's real surfaces **once**, on
one code state, in one process, captures the unshaped answer, and derives the
shaped one by applying the reduction to those same bytes — the reduction is a
pure function, so this is exact rather than an approximation. Grading uses the
stand's own `_grade_text` and gold.

**Correctness: unchanged on every task, both sides. Zero grade changes.** That
was the pass condition and it is met, not traded.

| | ten CODE-07 tasks, mean tokens | all 13 tasks, total | answers over the 25 000 client ceiling |
|---|---|---|---|
| `llm_wiki_best` before | 333.5 | 412 070 | T12, T13 |
| `llm_wiki_best` after | **274.6** (−17.7 %) | **9 321** (−97.7 %) | **none** |
| `llm_wiki` before | 13 163.8 | 540 402 | T06, T07, T12, T13 |
| `llm_wiki` after | **10 560.7** (−19.8 %) | **112 211** (−79.2 %) | T06, T07 |

Against cbm's 98.4-token mean from the `CODE-07` run — not re-measured here,
so this ratio carries that run's date — the paired gap narrows from **3.4× to
2.79×**. The remaining gap is answer *form*, not answer *content*: cbm ships
grouped table rows under one header line, llm-wiki ships one JSON record per
row. That is the next available win and it is not in this change.

### The largest opaque payload in the product was not a field

Written into the design mid-measurement, because measuring found it.
`mode=summary` on this repository answers with **4 078 communities, each a bare
list of `code:node:` strings — 199 770 of the answer's 208 786 tokens**, naming
no symbol, no file and no line. A rule that only looked at scalar fields would
have left it untouched: the hashes are list elements, not values of a key.

Hence the value rule extends to collections — *a structure holding nothing but
hashes carries nothing but hashes*. Measured effect, correctness held:

* `T12` (`mode=summary`) **208 786 → 6 387 tokens**, still graded `correct`.
  It now fits under the client's 25 000-token ceiling it previously exceeded
  8×, which means the answer is deliverable at all.
* `T13` (`mode=community`) **199 875 → 114 tokens**, grade unchanged (it was
  already wrong, on both products, for reasons `CODE-07` records).

`refused_node_ids` is protected **by name** rather than left to fall through
the shape rule, because a fail-closed guarantee should not rest on an accident
of shape.

**One default answer lost its only content, and it should be said plainly.**
`mode=community`'s entire payload was those hashes, so by default it now
returns the graph-report fields plus `omitted_fields: ["communities"]` and
nothing else. That is a real reduction in what the mode says. It is still the
better answer: 199 875 tokens is 8× a ceiling that would have made the client
reject the whole result, so the mode was undeliverable, and the shaped answer
at least names what it dropped and how to ask for it. The proper fix is not
here — `detect_communities` should name symbols instead of minting hash lists,
and that lives in `scripts/code_graph.py`, which belongs to another agent's
task today. Recorded as a defect, not patched.

### What the budget buys, and what it costs

Set on purpose, the budget behaves as designed and the cost is visible. All
four figures below are live calls against this repository, re-measured after
the collection rule landed:

* `mode=query` on `fuse_rrf` at `budget_tokens=200`: 4 rows trimmed,
  `body_tokens: 149`, `omitted_fields: ["node_id", "owner"]`,
  `truncated: true` — the ladder ran in order and the answer says so.
* The same call at `budget_tokens=120`: **refused**,
  `answer_budget_too_small`, `minimum_tokens: 122`. Two tokens short, and it
  says no rather than shipping something shorter than it promised.
* `mode=summary` at `budget_tokens=8000`: fits with fields alone,
  `body_tokens: 6368`, no rows lost.
* `mode=summary` at `budget_tokens=2000`: 115 rows trimmed, `body_tokens:
  1929`, `truncated: true` — and on the stand that answer **grades wrong**,
  because the gold term sat in the trimmed tail. The caveat is machine-readable,
  so it is a flagged-wrong and not a silent-wrong, but a budget set too low
  costs correctness. That is why there is no default budget, and why the number
  is the caller's decision.

## What this does not establish

* **`len//4` is not a tokenizer.** Every token figure here and in the
  measurement is an approximation; a real tokenizer will differ, and the budget
  will therefore be approximately, not exactly, honoured.
* **13 tasks, one repository, one day.** The same limits the `CODE-07` note
  states apply unchanged to the before/after measurement.
* **Nothing here measures whether an agent chooses `budget_tokens` well.** The
  argument exists; that an agent uses it wisely is unmeasured and not claimed.
* **The ladder's second step is untested against preference.** That a reader
  would rather lose `owner` than a row is an argument from derivability, not a
  measured preference.
* **The cbm column was not re-measured.** The 2.79× uses cbm's 98.4-token mean
  from the `CODE-07` run on a different code state; cbm also re-indexes in the
  background, so its own number moves. The llm-wiki halves of the comparison
  are paired; the cross-product ratio is not.
* **The budget was exercised at two settings, not swept.** 2 000 tokens and the
  refusal floor; nothing establishes a good default, which is why there is no
  default.
* **`mode=community`'s new default answer is untested against a reader.** That
  114 tokens naming an omission beats 199 875 tokens of hashes is an argument
  from the client ceiling, not a measured preference.

**One check the whole change fails, deliberately.**
`tests/test_quality_guards.py::test_all_script_imports_resolve_in_git` and
`::test_no_untracked_imported_modules` both fail while
`scripts/answer_budget.py` is untracked — correctly, since a clean clone would
break. The remedy is the one those tests name, `git add
scripts/answer_budget.py`, and it was not run because this task forbids
staging. Every other test in every set importing the changed modules passes:
647 passed, 1 skipped, 2 failed, both of them this.

## Sources

* `docs/research/2026-08-28-code-parity-first-pairing.md` — the 3.4× and 20.6 %
  measurement; run artifacts `benchmark/code-parity-first-pairing-2026-08-28.json`.
* `docs/research/2026-08-27-number-one-memory-market-research.md` — why
  Graphify and codebase-memory-mcp are the comparators.
* [Graphify-Labs/graphify (GitHub)](https://github.com/Graphify-Labs/graphify) —
  `--token-budget`, `--budget`, `GRAPHIFY_MAX_OUTPUT_TOKENS`, MCP tool list.
* [Writing effective tools for agents (Anthropic)](https://www.anthropic.com/engineering/writing-tools-for-agents) —
  25 000-token client cap; pagination/filtering/truncation; drop the `uuid`,
  keep the `name`; `response_format` enum.
* [MCP specification 2025-03-26, Tools](https://modelcontextprotocol.io/specification/2025-03-26/server/tools) —
  checked: no size limit, no truncation-signalling field.
* [MCP Output Too Large (Morph)](https://www.morphllm.com/mcp-output-too-large) —
  per-client limits; reported via search, page returned 429 on fetch.
* [Extending ResourceLink (arXiv 2510.05968)](https://arxiv.org/pdf/2510.05968) —
  patterns for large tool results.
* codebase-memory-mcp `search_graph` tool description, live schema on this
  machine — the `detail: "ids"` cheap form.
