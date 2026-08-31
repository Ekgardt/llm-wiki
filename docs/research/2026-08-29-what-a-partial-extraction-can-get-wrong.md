---
type: raw-source
status: active
confidence: high
source_authority: web
created: 2026-08-29
---

# What a partial extraction can and cannot get wrong

One-sentence summary: `extract_code` is already two passes and the second one is
safely per-source — proved by building a whole incremental generation that way
and finding it identical to a full build across all seven tables, added source
included — but the first pass must see the whole universe and is 43% of the
extractor, so the split is worth 3.0 CPU s on the 93.8% of deltas that rebuild
widely and 9.8 s on the 6.2% that do not, which measures end to end as +0.98 s
and −4.30 s and comes to a net loss of about 0.65 s a pass.

Dated research for the proposed change to `doctor._SourceExtractionAdapter`,
which today batches `extract_code` over every code source the moment one source
is rebuilt. It continues
[what an invalidation fingerprint can mean](2026-08-29-what-an-invalidation-fingerprint-can-mean.md),
whose §6 named this exact change as the trigger for revisiting its conclusion.

---

## 1. The defect, restated

`doctor._SourceExtractionAdapter._code_result` memoizes one call to
`extract_code(self.code_sources, …)`, and `_code_extraction_sources` is every
non-`knowledge/` source in the snapshot — not the rebuild set. So a one-line edit
to one Python file re-extracts all 678 code sources, and the 487 sources whose
records the builder is about to discard in favour of the parent's are extracted
anyway.

In Gradle's vocabulary the system does *incremental compilation* and never
*compilation avoidance*. The sibling note measured the consequence: making the
invalidation fingerprints real collapses the rebuild set 423-fold and saves
nothing, because the batch is per universe and the edited source always forces
it.

## 2. What `extract_code` actually builds

`_Collector.extract` is already two passes, and the split is exact rather than
approximate.

**Phase A — the universe.** `structural_nodes()`, then per source a parse and
`collect_python_definitions` / `collect_syntax_definitions`. Every write to a
shared resolution index happens here: `definitions`, `python_scopes`, `modules`,
`module_name_index`, `source_modules`, `files`, `tables`, `syntax_definitions`,
`syntax_functions`, the three scope maps, and `node_sources`. `_table`,
`_entry_point` and `_routes` are reached only from inside
`collect_python_definitions`'s `walk`, so they belong to phase A too — which
closes the §4.2 hole the sibling note left open, since an interface derived from
*this* boundary covers table and route nodes by construction.

**Phase B — one source at a time.** `collect_python_edges` /
`collect_syntax_edges`. Grepping every write in the file confirms these two
methods write no shared index at all: they only read them, and append to
`nodes` (via `setdefault`), `assertions`, `observations`, `evidence`,
`observation_source_dependencies` and the two dedupe id sets. All eleven
`add_occurrence` call sites are in phase A, so `node_sources` — the map
`source_dependencies` is computed from — is complete before phase B starts and
never moves during it.

Three facts make phase B order-independent and source-independent:

1. Every record id is `_identifier(...)` — a SHA-256 over content parts — never
   a counter. Node identity is `symbol_identity`, which carries no body and no
   span in this deployment.
2. The dedupe sets `assertion_ids` / `observation_ids` are keyed on ids that
   embed the analysing source's own `logical_id` and byte offsets, and
   `extract_code` already rejects duplicate logical ids. So one source's records
   can never suppress another's.
3. `add_node` is `setdefault` on a content-derived id, so a node two sources
   both create is byte-identical either way.

Therefore running phase B for a subset yields, for every source in the subset,
exactly the records a full run yields for it. §5 checks that against the product
instead of asserting it.

## 3. Current practice: this split has a name

The shape is not invented here; it is the standard answer, and every system that
has it draws the line in the same place — at the boundary between what a
dependent can read and what it cannot.

**rust-analyzer** is the closest analogue because it is also a graph over source
rather than a compiler with an artifact. Its `ItemTree` "condenses a single
`SyntaxTree` into a 'summary' data structure, which is stable over modifications
to function bodies", and exists to be "an invalidation barrier"; `DefMap`
"contains the module tree of a crate and stores module scopes" while `Body`
"stores information about expressions". The invariant is stated outright:
"typing inside a function's body never invalidates global derived data"
([architecture](https://rust-analyzer.github.io/book/contributing/architecture.html)).
Phase A is the ItemTree and DefMap; phase B is the Body.

**Bazel** builds the interface as a *separate pass over source*, not as a
by-product of compilation: `ijar` "processes jar files to remove everything
except call signatures", producing header jars "to improve the compilation
incrementality by only recompiling downstream dependents when the body of a
function changes", and `turbine` generates them from source through
`createHeaderCompilationAction`
([third_party/ijar](https://github.com/bazelbuild/bazel/tree/master/third_party/ijar),
[Java and Bazel](https://bazel.build/docs/bazel-and-java)).

**Roslyn** splits the same way into four phases, of which the middle two are the
relevant pair: a declaration phase producing a hierarchical symbol table, and a
binding phase that walks the syntax trees and resolves names into a semantic
model.

**Gradle** and **Zinc** supply the two warnings that carry over. Gradle
deactivates compile avoidance when annotation processors are on the classpath
"because for annotation processors the implementation details matter"
([Compilation Avoidance](https://blog.gradle.org/compilation-avoidance)); Zinc
exempts inheritance-induced dependencies from name hashing
([Understanding Recompilation](https://www.scala-sbt.org/1.x/docs/Understanding-Recompilation.html)).
Both say: narrow only as far as what the analysis actually reads. Here the
grep in §2 is what discharges that obligation — phase B reads the shared indexes
and nothing else.

So the design question is settled by practice: the split is right, and the
boundary this extractor already has is the right boundary. What is not settled
by practice is whether it is worth anything in a system this shape. That is §5
and §6.

## 4. What a partial batch gets wrong

One construction diverges, and it was found by looking for it rather than by
luck.

`_partition_nodes` attributes a node to its occurrence sources; a node with no
occurrence goes to whichever sources reference it in an assertion or
observation; a node with neither goes to a fallback owner, `min(source_ids)`.
The first two rules are per-source exact. The third is not: "referenced by
nobody" is global knowledge, and a partial run has less of it, so the fallback
bucket is larger and the fallback owner is handed nodes that a full run gives to
somebody else.

Measured on the frozen corpus, this is not hypothetical. `add_occurrence` and
`add_assertion` both return early on an empty span, so a **zero-byte source**
produces a file node and a module node with no occurrence and no structural
assertion. `tests/__init__.py` is such a file. Its module node is then claimed
in a full run by whichever source imports `tests`; in a partial run that does
not analyse that importer, nobody claims it and it falls to the fallback owner.
Running the fallback owner (`benchmark/COMPARATIVE.md`, the alphabetically first
source id) alone against a full run:

```
nodes full=5 partial=6 only-in-partial=1 only-in-full=0
  extra: module code-module/v1 'measure\x1fpython\x1ftests\x1ftests/__init__.py'
```

The fix that keeps the result exact is to refuse the shortcut rather than
approximate it: when the fallback owner is in the rebuild set, analyse the whole
universe. That degrades to today's cost and never to a different answer. It is
in the measured arm below.

## 5. Measured: the phase split, and the ceiling it sets

`extract_code` over this vault's 678 code sources, instrumented per phase
(CPU seconds, `time.process_time`):

| part of `extract_code` | CPU s | share |
|---|---|---|
| `ast.parse` | 4.18 | 20% |
| `collect_python_definitions` | 4.87 | 23% |
| `structural_nodes` | 0.11 | 1% |
| **phase A subtotal — must see the universe** | **9.16** | **43%** |
| `collect_python_edges` | 9.54 | 45% |
| `collect_syntax_edges` | 0.01 | 0% |
| **phase B subtotal — narrowable** | **9.55** | **45%** |
| validation, freezing, unaccounted | 2.35 | 11% |
| total | 21.06 | |

**Phase A is 43% of the extractor and no rebuild set can shrink it**, because
every source's definitions must be in the shared indexes before any source's
references can resolve. That is the ceiling, and it is the number the whole
question turns on.

Driving the two phases directly, and comparing each analysed source's partition
against the full run's (`freeze` and `partition` are the `_frozen` and
`_partition_code_extraction` steps):

| phase B run for | A (s) | B (s) | freeze | partition | total | per-source equality |
|---|---|---|---|---|---|---|
| all 678 (today) | 10.59 | 9.75 | 2.03 | 0.46 | **22.83** | — |
| 1 source | 10.28 | 0.01 | 2.61 | 0.11 | **13.02** | OK |
| 10 sources | 12.21 | 0.04 | 0.68 | 0.15 | **13.09** | OK |
| 100 sources | 11.43 | 0.96 | 0.85 | 0.19 | **13.43** | OK |
| 423 sources (today's rebuild set) | 12.45 | 5.63 | 1.47 | 0.28 | **19.83** | OK |

Every analysed source's `SourceExtraction` — nodes, occurrences, assertions,
evidence, observations, dependencies, source_dependencies, workspace_sensitive —
was equal to the full run's, for all four subset sizes. So §2's argument holds
against the product.

The ceiling in one line: **at today's rebuild set the whole idea is worth 3.0
CPU s; only a rebuild set near 1 is worth 9.8.**

## 6. Measured: the delta pass, and where the ceiling goes

The stand runs the product's own maintenance pass
(`doctor._build_or_refresh_generation`) against a frozen copy of this vault --
917 sources, 678 of them code, 107 knowledge pages, 132 project journals -- into
a throwaway state root, with a **pinned copy of `scripts/`** taken at commit
`75f842d` because this checkout is shared. Vectors are switched off on both
sides via `search_memory.build_generation_vectors_if_available`. The machine
carried a load average of 14-18 throughout (other agents), so **CPU seconds are
the comparable figure**; wall seconds swung from 146 s to 826 s for the same
work and are not reported per run.

Arm A is the pinned code unchanged. Arm B splits the extractor: one definitions
pass over the universe, then the edges pass for the sources the builder asks
for, with the §4 escape hatch. A full build is identical in both arms by
construction (the rebuild set is everything), and measured so: 166.12 s against
165.23 s.

### 6.1 What the rebuild set actually is

Instrumented, for a one-line comment appended to `scripts/reliable_memory.py`:

```
waves = [1, 425, 1]     rebuild_total = 427 code sources of 678
arm A: extract_code called once with 678 sources
arm B: the edges pass given to 1, then 425, then 1 -- 427 of 678
```

Three waves, not one: the builder discovers the rebuild set in stages
(`_initial_rebuild`, then `_workspace_invalidated`, then the dependency
closure). That matters, because a session has to survive all three, and each
wave that asks for a new source makes the partial arm re-freeze and re-partition
what it has accumulated. Arm B spent 10.0 s on the universe pass and 13.73 s on
`analyze`, of which 1.95 s was incremental freezing and 0.97 s three
partitions where arm A does one for 0.41 s.

### 6.2 The paired arms

Two windows, arms interleaved, each delta starting from a byte-identical restore
of that arm's parent state. "wide" is today's rule (427 of 678 rebuilt);
"narrow" simulates a real `exports` fingerprint by making `_semantic_changes`
return nothing for an interface-preserving edit, which is exactly the 1-of-910
rebuild set the sibling note measured.

| window | rebuild set | A -- today | B -- partial | B − A |
|---|---|---|---|---|
| first | wide (427) | 149.81, 148.80 → **149.31** | 153.79, 154.12 → **153.96** | **+4.65** |
| second | wide (427) | 153.43, 145.98 → **149.70** | 150.96, 150.41 → **150.68** | **+0.98** |
| first | narrow (1) | 137.41 | 129.39 | **−8.02** |
| second | narrow (1) | 146.67, 142.90 → **144.78** | 139.29, 141.67 → **140.48** | **−4.30** |

Read the two rows honestly and they say different things.

**Wide is not measurably different, and never faster.** The two windows disagree
in magnitude (+4.65 against +0.98) and arm A alone varies by 7.5 s between two
runs of identical code on an identical corpus (153.43 against 145.98). A 3.0 s
ceiling cannot be resolved by a stand whose noise floor is twice that. What the
four runs settle is the direction: no arm-B wide run beat the arm-A mean of its
own window.

**Narrow is a real effect.** Both windows agree in sign and order of magnitude,
−8.02 and −4.30, against the same within-arm spread. That is the 9.8 s ceiling
from §5, partly given back to per-wave bookkeeping.

### 6.3 And what a knowledge-page edit costs: nothing

The same stand, editing `knowledge/notes/single-directory-vault-decision.md`
instead:

| arm | CPU s | `extract_code` calls |
|---|---|---|
| A | 128.11 | **none** |
| B | 125.12 | **none** |

`extract_code` is never called at all, in either arm. Workspace invalidation is
already scoped to universes (that is what `e05c79a` fixed), so a knowledge edit
puts no code source in the rebuild set and the batch never fires. The knowledge
universe has its own two-phase extractor -- `_Extraction._index_pages` then
`add_source` per source -- and the whole thing costs **0.07 CPU s** for 107
sources, `_index_pages` 0.04 s and every `add_source` together 0.01 s. Project
journals are already extracted one at a time.

So the whole-universe defect is code-only, and the two questions have different
answers: a knowledge-page edit has nothing to win, a code edit has 3.0 s.

One incidental measurement worth recording: an incremental delta costs about
what a full build costs (arm A, 145.68 s delta against 145.94 s full on the same
corpus). Reuse is not where a pass spends its time -- writing and re-validating
the whole immutable generation is, as the sibling note's decomposition found.

## 7. Measured: incremental equals full

Correctness first, because a fast index that answers differently from a slow one
is not a win. Each pair applies the same edit to the same frozen corpus, builds
it once incrementally from the parent and once from scratch with
`force_rebuild`, and compares every table of `evidence.sqlite3` row by row after
ordering by every column.

| arm | edit | verdict |
|---|---|---|
| B — partial extractor | a code source **added** | **IDENTICAL** |
| B — partial extractor | a code source edited | **IDENTICAL** |
| A — today's extractor (control) | a code source edited | **IDENTICAL** |

The added-source case is the one that has bitten before — it is the defect
`e05c79a` fixed, where a source appearing must resolve the dangling links it
satisfies. Under the partial extractor it comes out row-for-row identical to a
full build:

```
tables A=7 B=7
  assertion    identical  rows=74210      node         identical  rows=30212
  dependency   identical  rows=0          observation  identical  rows=80877
  evidence     identical  rows=155087     occurrence   identical  rows=26717
  source       identical  rows=918
VERDICT: IDENTICAL
```

One qualification, and it is the reason §4 exists: this holds *because* the arm
takes the escape hatch. Without the fallback-owner rule the partial arm produces
a different node table, as §4 measured. The equality proof covers the arm as
built, not the idea as stated.

## 8. What this supports

**Do not split the extractor. Say in the code that it is whole-universe on
purpose, what the ceiling is, and what would have to change for the arithmetic
to turn.**

The recommendation is not the one the brief expected, and the reason is not the
one the sibling note's §6 predicted. That section said reviving this would be
worth "a real 24.5 s of `extract_code` saved on 6.2% of deltas". Two of those
three numbers are wrong:

- **It is not 24.5 s, it is 9.8 s at best and 3.0 s in the common case.** The
  edges pass is 45% of the extractor; the definitions pass that must see the
  whole universe is 43%, and no rebuild set can shrink it. Skipping extraction
  entirely was never on the table.
- **The 6.2% still holds, and it is what kills the trade.** Narrow rebuild sets
  are worth −4.30 CPU s, and they arrive on 6.2% of code-touching commits. Wide
  ones are worth +0.98 s, and they arrive on the other 93.8%. Expected value:
  `0.062 × (−4.30) + 0.938 × (+0.98) = +0.65 s` per pass. **The split is a net
  loss in expectation**, and it is a net loss even before the cost side.

The cost side is the part that makes it easy. Reaching the 3.0 s ceiling at all
requires one of two things, and both are expensive in the currency this vault
actually spends:

- A **resumable partitioner** that folds each newly analysed source's records
  into standing attribution maps instead of re-partitioning the accumulation.
  That means a second place that decides which source owns which record --
  duplicating `_partition_code_extraction`'s rules in the module that decides
  what every source in the graph owns. This vault has been bitten by two-writers
  divergence repeatedly (`_page_diverse` written and reached by nobody,
  `generation_embedder` with two entry points and one of them wired).
- Or `extract_code` taking the subset as a parameter, which means editing
  `scripts/code_extractor.py` -- **46 managed-gate findings**, 25 `[COMPLEXITY]`
  and 21 `[STRUCTURE]`, in the correctness core of the graph. The arm measured
  here dodged that with a module-global collector factory, which is fine for a
  stand and not shippable: it is a mutable module global on the hottest write
  path.

Half a second, in the wrong direction, for either of those.

**What is worth doing, and is done:** stop the batch from reading as an
oversight. `doctor._SourceExtractionAdapter` now says in a docstring that the
whole-universe batch is deliberate, that phase A is 43% of the extractor and
cannot be narrowed, that the split was built and measured, and where the numbers
are. `tests/test_extraction_universe.py` pins the two structural facts the
conclusion rests on -- that the edges pass writes no shared resolution index and
records no occurrence -- so that if someone makes the edges pass write into
`definitions` or `node_sources`, the test fails and names this note, rather than
the assumption rotting in a paragraph nobody re-reads. That is the Gradle
annotation-processor warning and the Zinc inheritance exemption, expressed as a
gate instead of folklore.

### What was proved along the way, and is worth keeping

Two results survive the negative recommendation and should not have to be
re-derived:

1. **The split is safe.** Partial extraction is equal to full extraction, per
   source, at 1, 10, 100 and 423 analysed sources; and a whole incremental
   generation built that way is identical to a full build across all seven
   tables of `evidence.sqlite3`, including a source **added**.
2. **There is exactly one construction that breaks it**, and it is named in §4:
   the fallback owner. A zero-byte source contributes a node with no occurrence
   and no structural assertion, and "referenced by nobody" is global knowledge a
   partial run does not have.

### When this should be revisited

The arithmetic turns if either term moves, and both are measurable before any
code is written:

- **If the definitions pass stops being 43%.** It is 9.16 s of 21.06 s today,
  and 4.18 s of that is `ast.parse`. A persisted phase-A summary -- an ItemTree
  in rust-analyzer's sense, stored in the generation and reused for sources
  whose bytes did not move -- would attack the half no rebuild set can reach.
  That is a new artifact in an immutable generation, so it is an architecture
  decision, not a refactor.
- **If wide rebuild sets stop being 93.8% of deltas.** 427 of 678 code sources
  are workspace-sensitive, and they are re-extracted whenever anything in their
  universe changes semantically. Narrowing *that* -- Zinc's name hashing, asking
  whether a sensitive source's unresolved names intersect the names the delta
  moved -- would shrink the common case rather than the rare one. It needs the
  `exports` interface the sibling note declined to build, and §2 shows that
  interface can now be defined completely, because `_table` and `_routes` turn
  out to sit in phase A.

Either one alone changes the answer. Neither was attempted here, and no part of
this was shipped.

## Sources

- [Architecture — rust-analyzer](https://rust-analyzer.github.io/book/contributing/architecture.html)
- [bazel/third_party/ijar](https://github.com/bazelbuild/bazel/tree/master/third_party/ijar)
- [Java and Bazel](https://bazel.build/docs/bazel-and-java)
- [Compilation Avoidance — Gradle blog](https://blog.gradle.org/compilation-avoidance)
- [An In-depth Look at Gradle's Approach to Faster Compilation](https://blog.gradle.org/our-approach-to-faster-compilation)
- [Understanding Incremental Recompilation — sbt](https://www.scala-sbt.org/1.x/docs/Understanding-Recompilation.html)
- [The "red-green" algorithm — Salsa](https://salsa-rs.github.io/salsa/reference/algorithm.html)
- [What an invalidation fingerprint can mean](2026-08-29-what-an-invalidation-fingerprint-can-mean.md)
