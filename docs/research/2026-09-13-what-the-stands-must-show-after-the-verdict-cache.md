# What the stands must show after the verdict cache

Dated 2026-09-13. Yesterday's change put the chunk walk back on the read path
and made its verdict remembered by the artifact's content digest
(`docs/research/2026-09-12-the-rows-are-checked-once-per-distinct-bytes.md`).
The owner then asked for every stand to be run once the tests are green. This
note settles *how* those numbers must be taken, before they are taken, because a
cache changes what a benchmark measures.

## The facts

- The cache file on the installed vault holds 10 stat entries and **0 verdicts**
  right now (`cache/evidence-graph/verified-artifacts.json`, 1 733 bytes,
  2026-09-12 23:44) — the new code has not yet answered a question here.
- `benchmark/run_code_parity.py` starts a **fresh child process per call**, so
  every one of its calls is a cold open. What it cannot do is make the *bytes*
  cold: the first call of the first run fills the verdict, and every later call
  in every later run reads it.
- The stand therefore measures two different things before and after its own
  first call, and the difference is exactly the 0.38 s walk.

## Practice on this date

1. **Publish cold and warm separately; do not let a warm number stand in for a
   cold one.** Benchmarks should include both a cold start with an empty cache
   and a warm-cache phase, because the first shows the baseline overhead and the
   second the optimised path
   ([why benchmarks need cold-start and warm-cache scenarios](https://milvus.io/ai-quick-reference/why-should-benchmark-tests-include-both-coldstart-scenarios-first-query-empty-cache-and-warm-cache-scenarios-especially-for-measuring-latency-in-vector-searches)).
2. **Burn the cold cost outside the measurement window, or report it as its own
   number.** Define the warm-up phase, gate the measurement, discard early
   samples, and publish cold and warm results separately; a cold first run is
   skewed by page-cache warming and connection fill
   ([instance warm-up phases and benchmark results](https://medium.com/@daya-shankar/instance-warm-up-phases-and-their-impact-on-benchmark-results-b0934b9e256f)).
3. **Space the cold phase so no cache entry survives it.** The recommended cold
   phase fires each distinct input exactly once, spaced so nothing is reused,
   which gives the worst-case floor; the warm phase then repeats it
   ([cold cache, hot cache](https://tianpan.co/blog/2026-04-10-cold-cache-hot-cache-llm-latency-staging-lies)).

## The decision for this run

- **The parity numbers are reported as warm-bytes numbers**, which is what the
  installed vault will actually serve from tomorrow morning: the generation does
  not change between questions, so the second question of the day onwards is the
  normal case. The three runs per side stay as they were, so the comparison with
  the other tool is still like-for-like.
- **The cold number is taken separately and named as its own measurement**: the
  verdict entry for the vault's generation is removed from the cache file, one
  `get_architecture mode=query` is timed, and that is the cold-open figure. It is
  not averaged into the parity numbers, because averaging a once-per-generation
  cost into per-question latency would overstate every question but the first.
- **Every other stand is run at its documented defaults**, sequentially, one
  process at a time — six parity runs sharing this machine with an index build
  is what produced the blackboard flake on 2026-09-12
  (`docs/research/2026-09-12-a-lock-error-then-a-fence-error-is-a-question-not-a-retry.md`).
- Nothing derived from the private vault is committed: the stands that read
  `knowledge/notes/` (selective forgetting) report counts only.

## The gold was pinned to line numbers, and that is the defect

The clean full run of `9e680df` (8 411 passed, 1 failed, 16 min 40 s) failed
`tests/test_parity_gold_resolves.py::test_every_gold_citation_resolves_in_the_tree[T07]`
for the second time in a day: `scripts/search_memory.py:5038 does not name
_legacy_vector_source_membership`. The gold was true both times — the function is
still there, now at 5085 — and both times my own edits above it moved the line.
Fixing the number is fixing the instance; the class is that **a definition's line
number is not a fact worth storing**.

- Golden sets earn their keep by staying small and maintainable, so what they
  store should be the property under test and nothing more
  ([golden sets](https://synthmetric.com/golden-sets-create-small-mighty-test-suites/)).
- The point of a golden test is that "the output cannot change by accident"
  ([golden tests](https://medium.com/casperblockchain/golden-tests-e521077ae235)).
  A line number changes by accident on every edit above it, which is the
  opposite: it makes the guard cry wolf and trains its reader to re-stamp it.

**Decision.** A citation may name a file and a definition and leave the line to
be resolved: `scripts/search_memory.py (def _legacy_vector_source_membership)`.
Eleven anchors of that shape lost their numbers. The line is resolved from the
tree by the guard, which still fails loudly when the definition leaves the file.
Every number that is *graded* keeps its numbered anchor and its exact check —
those numbers are the measurement, and a call site's line is what the question
asks about. A numberless anchor is allowed only when its note starts with `def `,
so prose inside a citation ("grep -rn x scripts/ (on this date)") is not mistaken
for one.

## Open, and honest

I have not yet measured the cold open with the verdict present; the 0.38 s is
yesterday's measurement of the walk it replaces, and the cold figure in the
report will be a fresh measurement, not that one. If the cold open turns out to
be slower than yesterday's 1.23 s by more than the walk, the cache is not doing
what this note assumes and I will say so with the numbers.

Files: `scripts/search_memory.py`, `scripts/verified_artifacts.py`,
`benchmark/run_code_parity.py`, `benchmark/code-parity-v2.json`,
`tests/test_parity_gold_resolves.py`,
`docs/research/2026-09-13-what-the-stands-must-show-after-the-verdict-cache.md`.
