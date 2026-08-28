# A zero that means "unresolved", and a community that names nothing

**Date:** 2026-08-28
**Register items:** `NEW-124` (who-calls answers zero for methods), `NEW-125`
(communities name hashes, so the budget drops them)
**Rule 2 scope:** two design changes to the shape of a code answer —
`find_callers` gains a named partial, `detect_communities` gains named members,
a stated bound, and a symbol anchor.

---

## 1. The two questions this note had to settle

1. When a static extractor cannot bind a call's receiver, what may the answer
   say? Is "0 callers" ever a legitimate rendering of "I could not resolve it"?
2. When a whole-graph derived structure is too large to name, what is the
   honest answer shape — drop it, hash it, cut it, or refuse?

---

## 2. What current practice says about unresolved dispatch

The field's own vocabulary is *soundness* (every call that happens at run time
appears in the graph) against *precision* (every edge in the graph can happen).
Three measurements are worth carrying:

- **Static call graphs miss a lot, and the field knows it.** *Call Graph
  Soundness in Android Static Analysis* (ISSTA 2024, arXiv:2407.07804) measured
  13 static analysis tools missing, on average, **61 %** of the
  dynamically-executed methods. For Python specifically, only **49 %** of the
  edges present in dynamic call graphs were found in static ones
  (*On the Recall of Static Call Graph Construction in Practice*).
- **Precision and soundness trade against each other**, so a tool that only
  emits edges it can prove is *expected* to be incomplete. That is the design
  this vault already chose: `code_extractor` emits a `CALLS` assertion only for
  a uniquely resolved target, and records everything else as an
  **observation** with a controlled reason (`dynamic_dispatch`,
  `ambiguous_target`, `unresolved_reference`, `missing_dependency`).
- **The literature's failure mode for LLM-built call graphs** is *fabrication*
  — inventing interprocedural paths that do not exist (arXiv:2505.12118). So
  the remedy for an incomplete answer is never "guess an edge".

Nothing in that body of work licenses rendering an unresolved receiver as an
absence. The rule it does support is the one this change implements: **emit no
edge you cannot prove, and say out loud that you could not prove some.**

### Measured on this repository

The evidence graph already held everything needed. Of 76 044 observations,
**46 385** are `dynamic_dispatch` on `CALLS` and 21 626 `unresolved_reference`,
against 35 313 resolved `CALLS` assertions. Only **2 619** of those resolved
assertions point at a `method` node, out of 3 458 method nodes — which is why
the defect showed up on methods and not on module functions.

`self.method()` is *already* resolved at extraction time
(`CodeExtractor._resolve_expression` binds `self.<attr>` against the enclosing
class's scope). Of the 776 `self.*` dynamic-dispatch observations, only 92 are
a bare `self.<name>`, and those are instance attributes holding callables
(`self._clock`, `self._callback`, `self._encoder`) — not methods. So there is
**no cheap extraction-time win left**, and changing the extractor would
invalidate every published generation for a gain measured at roughly zero. The
fix belongs at query time.

### The decision

`find_callers` keeps `callers` as *proved edges only* — no edge is invented —
and adds three fields to the report:

| field | meaning |
|---|---|
| `unresolved_callers` | bounded sample of call sites whose receiver could not be bound and whose called attribute is this name; each row carries file, line, the calling function, the call text and the reason |
| `unresolved_caller_count` | **exact**, even when the sample is cut |
| `unresolved_callers_truncated` | whether the sample was cut |

Rejected: merging the two lists (that is fabrication); reporting only a count
(a number with no citation cannot be checked); refusing the whole answer (the
row-ceiling refusal `NEW-116` closed was already judged too blunt).

Matching is on the **tail** of the recorded call text, so
`queue.recover_expired_leases` and `_queue().recover_expired_leases` both answer
for `recover_expired_leases`. It is compared with `substr(text, -n)` rather than
`LIKE '%.name'` because a Python name carries `_`, which `LIKE` reads as a
single-character wildcard — `LIKE` would also match `recover-expired-leases`.
A test holds that line.

---

## 3. What current practice says about naming a clustering

`detect_communities` runs deterministic weighted Louvain. The practice around
Louvain output is consistent on two points:

- **Communities are labelled, not left as node ids.** Neo4j GDS and comparable
  engines return a community id per node and expect the caller to join it back
  to the node's properties; the *answer* a human reads always carries names.
- **Small communities are dropped or thresholded.** Published pipelines treat a
  community as "valid" only above a size threshold; below it the partition is
  noise, not structure.

This repository's partition is exactly that shape: **4 078 communities over
17 194 members**, of which **1 698 have size 2** and 1 148 have size 3 — 70 %
of the "communities" are pairs or triples.

### The size wall, measured

| answer shape | estimated tokens |
|---|---|
| all 4 078 communities, every member named | **899 071** |
| all 4 078, ≤10 members each | 729 189 |
| 200 communities, ≤10 members | 110 692 |
| 100 communities, ≤10 members | 38 545 |
| **30 communities, ≤10 members** | **11 836** |

`answer_budget.MAX_BUDGET_TOKENS` is 25 000 — "the client ceiling Anthropic
documents for tool responses". The non-community part of a `summary` answer
measures ~9 000 tokens. So 30 × 10 is the largest bound that leaves the shared
`summary` answer inside the ceiling, and it is chosen by that arithmetic rather
than by taste. Ordering is **largest first**, following the precedent already in
this module (`_stored_hotspots` returns the top of a ranking and sets
`hotspots_truncated`).

### The part that no bound can fix

The parity stand's T13 asks *"Which module community does `fuse_rrf` belong
to?"*. `fuse_rrf`'s community has **5 members and ranks 729th of 4 078 by
size**. No bound that fits 25 000 tokens reaches rank 729; the whole listing
that would reach it costs 36× the ceiling. This is not a tuning problem — a
bounded whole-graph listing **cannot** answer a symbol-anchored community
question at this scale, and saying otherwise would be untrue.

So `detect_communities` gains a `symbol` anchor, exactly as who-calls has one.
Measured: `symbol="fuse_rrf"` answers in **291 tokens** with
`scripts.retrieval.fuse_rrf` at `scripts/retrieval.py:1378` and
`scripts.retrieval._fusion_weights` beside it — the retrieval cluster T13 asks
for, against **13 477** tokens of unanchored listing that never reaches it.

Rejected: reordering the listing to favour non-test code so that
`scripts/retrieval.py` lands in the visible prefix. Measured first: the top 30
communities hold 114 `scripts/` members against 186 `tests/` ones, so the
listing is not actually test-dominated, and reordering would have been tuning
the ranking to a benchmark rather than to a reader.

**Not delivered here, and it is the reason T13 still grades wrong:** the MCP
tool's argument contract rejects `symbol` for `mode=community`
(`_ARCHITECTURE_ARGUMENTS["community"]` and `_architecture_community` in
`scripts/mcp_server.py`), which is another agent's file in this session. The
engine answers the question; the tool cannot yet ask it.

---

## 4. Cost, measured

| operation | before | after |
|---|---|---|
| `mode=community` end to end, warm | 3.3 s, 117 tokens, `omitted_fields: ["communities"]` | 4.9 s, 13 479 tokens, 30 named communities |
| naming 300 community members | 1.45 s (one `occurrence` scan per node) | 0.09 s (`EvidenceGraph.node_locations`, one anchored scan) |
| `find_callers` on a rare name | 1.9 s | 4.2 s (adds one exact count and one bounded join) |
| `find_callers` on `execute` (1 395 unresolved sites) | — | +1.35 s for the bounded 200-row sample |

`occurrence` is indexed by `(source_id, byte_start, byte_end)`, not by
`node_id`, which is why the per-node form costs a full table scan each time and
the bulk form does not. No index was added: an index is part of the generation
schema, and changing it would invalidate every published generation.

---

## 5. Sources

- Call Graph Soundness in Android Static Analysis, ISSTA 2024 — arXiv:2407.07804
- On the Recall of Static Call Graph Construction in Practice
- Do Code LLMs Do Static Analysis? — arXiv:2505.12118 (fabrication as the
  dominant error mode when call graphs are guessed)
- Neo4j Graph Data Science, Louvain — community id per node, joined to names
- The Power of Communities (arXiv:1909.11706) — community labelling, and size
  thresholds for what counts as a valid community
