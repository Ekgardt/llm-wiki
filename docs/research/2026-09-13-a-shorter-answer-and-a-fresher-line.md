# A shorter answer, and a fresher line

Dated 2026-09-13. The owner read the full comparison
(`docs/COMPARISON-2026-09-13.md`) and asked for the two places where we lose to
be fixed: our answers cost more tokens than the other tool's, and after an edit
our answer names the line the symbol used to sit on. This note settles both
before either is touched.

## What is actually being paid, measured

One question, `get_architecture mode=callers symbol=fuse_rrf`, on the installed
vault, 9 rows:

| shape | tokens |
|---|---|
| as it answers today (`indent=2`) | 836 |
| the same JSON with no indentation | 608 |
| and without the dotted module name the path already carries | 558 |
| and with the rows grouped under their file | 520 |

The other tool answers the same question in **44 tokens** — it returns one row,
production callers only, under a shared prefix printed once. Half of its saving is
a judgement we deliberately do not share (it drops test callers, and that is why
it answers T16 wrongly while we answer it correctly). The other half is shape, and
that half is ours to fix: **316 of our 836 tokens are whitespace, a repeated
module path and a repeated file path.**

For the second defect the measurement is simpler. After `_page_diverse` moved from
line 3030 to 3054, the other tool's graph answered 3054 and ours answered 3030,
because their index watches the tree and ours is rebuilt by the nightly pass. A
forced rebuild inside a live session is refused by the maintenance fence, twice
measured, `TimeoutError: Evidence Graph build cancelled` after ~46 s.

## Practice on this date

1. **Whitespace is billed like content.** A recorded benchmark: 11 842 tokens
   pretty-printed against 4 617 minified for the same payload, and "a 5 KB
   pretty-printed JSON object might compress to 2 KB minified, cutting the token
   cost of that context by 40% with zero loss of information"
   ([TOON vs JSON](https://www.tensorlake.ai/blog/toon-vs-json),
   [why optimize JSON for LLMs](https://dev.to/del_rosario/why-should-we-optimize-json-for-llms-hep)).
   "Pretty-printing is for humans — AI doesn't need it"
   ([LLM token optimization](https://redis.io/blog/llm-token-optimization-speed-up-apps/)).
2. **The current answer to index freshness is a check at query time, with a
   watcher as an optional hint.** A hybrid design "combines a file watcher for
   proactive recursive filesystem invalidation with on-demand freshness
   verification, since requests can arrive before filesystem events and the
   watcher serves as a hint rather than a correctness oracle"; on-demand probing
   "costs one stat per unchanged file and never writes to the index"
   ([demand-driven index](https://github.com/jballo/camino/issues/44)).
3. **Invalidate per file, not per repository.** A content-diff over the working
   copy lets "source edits invalidate and re-index only affected chunks rather
   than the whole repository", and incremental indexing re-derives only the
   symbols whose content changed
   ([CocoIndex review](https://dev.to/andrew-ooo/cocoindex-review-incremental-rag-engine-for-ai-agents-248b)).
4. **A language server pays about 11 ms per update against ~419 ms for a fresh
   compiler invocation** ([structural codebase index](https://arxiv.org/pdf/2606.22417)),
   which is the order of magnitude a per-file re-extraction has to stay inside to
   be worth doing on the answer path.

## Decision one: the answer stops paying for whitespace and repetition

- **Serialize a tool answer compactly.** `separators=(",", ":")`, no `indent`.
  Nothing is dropped and no field changes name. 836 → 608 tokens on the measured
  question, 27%.
- **Drop the dotted module prefix from a row that already carries its file
  path.** `["scripts/retrieval.py", 3113, "scripts.retrieval._fused_candidates"]`
  becomes `["scripts/retrieval.py", 3113, "_fused_candidates"]`: the module is the
  path, spelled twice. 608 → 558.
- **Group the rows of one file under that file.** The file path is printed once
  instead of once per row. 558 → 520, and the answer reads as what it is — a few
  places in a few files.

Together 836 → 520, **0.62×**, with every fact kept. What this does *not* do is
copy the other tool's filtering: we keep test callers, because a question about
tests is a question we answer and it does not.

## Decision two: freshness is checked on the answer path, per file, and never writes

- Before a code answer is shaped, every file it names is checked against the
  digest the generation recorded for it. That is one hash of one file, not a walk
  of the tree — `detect_repository_changes` already does the whole-tree version
  and is far too expensive to run per question.
- A file whose digest moved is re-extracted **alone**, with the extractor the
  generation itself uses, and the rows that name symbols in it get the line those
  symbols occupy now. A symbol that no longer exists is marked, not renumbered.
- Nothing is written: no generation is published, no catalog row changes, no
  active pointer moves. The nightly pass still owns the rebuild. This is exactly
  the "on-demand verification, watcher as a hint" shape, minus the watcher — and
  the watcher stays unbuilt, because the contract forbids a persistent daemon and
  the check above is what makes it optional rather than necessary.
- Bounds: at most the files the answer names, a cap of 20 of them, inside the
  caller's remaining deadline, and any failure leaves the stored line untouched
  rather than failing the answer.

Why not the alternatives:

- **Rebuild the generation when the tree moves.** Measured at 46 s under the
  fence, and the fence refuses it during a live session for good reasons. It also
  writes, which a read path must not.
- **Mark the answer stale and stop there.** Honest but useless: the agent asked
  where a symbol is, and "somewhere, my index is old" is not an answer when the
  right line costs one file's worth of parsing.
- **Build the watcher.** It is in the superset contract as *optional bounded
  watching*, and the source above says plainly why it cannot be the correctness
  oracle on its own. The per-file check has to exist either way, so it is first.

## What must be true after the change

- The measured question drops to about 520 tokens, and the parity gold still
  grades every task correct — the names and numbers it matches on are all kept.
- Immediately after an edit that moves a definition, `mode=symbol` answers the new
  line without any rebuild, and the stand's T04 grades correct rather than partial.
- A repository whose files are unchanged pays one stat and one hash per file named
  in the answer, and no answer becomes slower than its deadline allows.

## A bound with no headroom, measured

The Windows shards carried `timeout-minutes: 40`. Measured on run 34753755498
(with `-v`) and run 34725227244 (without it), the slowest Windows shards take
22-26 and 23-30 minutes respectively — so `-v` costs about nothing, and the cap
sat a few minutes above the normal run. One shard, `windows_full py3.12-s1`,
took 45 minutes on a slow runner and was cancelled at the cap; GitHub reports
that as `cancelled`, not `failure`, and refuses to re-run it, so the branch could
not go green without a new commit.

The cap is now **60 minutes** for the Windows class only. That is the same defect
class as commit `9c88bbf`: a bound has to measure the hang it was written for, not
the runner's speed. Linux and macOS keep 20 minutes, where the measured range is
6-12.

Files: `.github/workflows/tests.yml`, `scripts/mcp_server.py`, `scripts/answer_budget.py`,
`scripts/fresh_positions.py` (new), `scripts/code_graph.py`,
`tests/test_answer_budget.py`, `tests/test_fresh_positions.py` (new),
`docs/research/2026-09-13-a-shorter-answer-and-a-fresher-line.md`.
