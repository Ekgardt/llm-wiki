---
type: raw-source
status: active
confidence: high
source_authority: web
created: 2026-08-29
---

# What an invalidation fingerprint can mean

One-sentence summary: four of the five `invalidation_fingerprints` name nothing
any dependent can read and cannot be given a definition at all; the fifth,
`exports`, has an exact one — the set of definitions a source contributes to the
extractor's shared resolution indexes — and making it real narrows a code edit's
rebuild set from 423 sources to 1 while saving no measurable time, because
extraction is batched per universe and the edited source always forces its own
batch, so the right outcome is to name the choice in the code rather than ship
the narrowing.

Dated research for the change to `doctor._source_extraction`, which today
computes all five keys as `sha256(f"{key}:{sha256(content)}")`.

---

## 1. The defect, restated

`doctor._source_extraction` builds every fingerprint from the content digest:

```python
digest = hashlib.sha256(content).hexdigest()
fingerprints = {
    key: hashlib.sha256(f"{key}:{digest}".encode("ascii")).hexdigest()
    for key in ("exports", "imports", "signatures", "aliases", "project_metadata")
}
```

Verified against the vault's live generation
(`generation-18d04225ed539d72-265c0570`, 910 sources — active when measured;
the nightly pass advanced the pointer during this work): for **910 of 910**
sources the stored fingerprints equal that function of the stored content
digest. So `_semantic_changes` — which is defined as "changed sources whose
fingerprints moved" — is exactly `changed`, and the five keys buy nothing.

The machinery is not idle decoration. `_semantic_changes` feeds two things in
`evidence_graph_builder._expanded_rebuild`: the seed of the dependent closure,
and the trigger for workspace invalidation. Both are the "should this change be
propagated?" question, and both currently answer "always".

## 2. What each of the five keys can mean

This is the part that has to be settled before anything is computed. A
fingerprint whose meaning nobody can state is the defect one layer up.

The question has an exact answer here, because the extractor's cross-source
channels are enumerable. `code_extractor._Collector` builds exactly three shared
indexes, and one shared attribution map:

| index | key | what it holds |
|---|---|---|
| `definitions` | `(module, name)` | node ids of module-level defs |
| `python_scopes` | `(module, scope, name)` | node ids of defs in a scope |
| `modules` | `module_name` | module node ids |
| `node_sources` | `node_id` | the sources that **define** that node |

`_resolve_expression` reads the first three and nothing else.
`source_dependencies` — the edge the dependent closure walks — is computed in
`add_observation` as the sources holding the candidate nodes, i.e. through
`node_sources`.

Two facts make the surface exact:

1. **Every `add_occurrence` call in the file passes role `"definition"`.** There
   is no reference-role occurrence, so `node_sources[n]` is the set of sources
   that *define* `n`, never the set that mentions it. A call added or removed
   inside a function body moves nothing in any of the four.

2. **Node identity carries no body and no span.** `symbol_identity` returns
   `("code-symbol/v1", "repo \x1f language \x1f path \x1f owner \x1f name \x1f signature")`.
   The SCIP branch, which *would* key identity on the definition's byte offsets,
   is unreachable in this deployment: `scip_symbols` defaults to `()` and no
   production caller supplies it.

So a source's readable interface is exactly **the set of definitions it
contributes**, and the five keys resolve as follows.

| key | can it be defined? | why |
|---|---|---|
| `exports` | **yes** | the definition entries this source contributes to `definitions`, `python_scopes`, `modules` — the only channel a dependent reads |
| `signatures` | **no, subsumed** | `signature` is *inside* the node identity key, so a signature change necessarily changes `exports`; it has no separate content |
| `imports` | **no** | `aliases` is a local dict built per-source inside the analysis method; it never enters a shared index, so no dependent can read it |
| `aliases` | **no** | the same local dict; the key is a second name for `imports` |
| `project_metadata` | **no** | project journals are extracted one at a time (`extract_projects((one_source,), …)`), so a journal has no shared universe to be visible in |

The honest count is **one of five**. That is a finding, not a shortfall: a key
that carries no dependent-visible information must not pretend to vary, or it
re-creates exactly today's defect in a longer form.

## 3. Current practice

### 3.1 Bazel and Gradle — the interface *is* the compiled artifact minus bodies

This is the closest analogue and it is the same design. Bazel's `ijar` "processes
jar files to remove everything except call signatures", producing *header jars*
used "to improve the compilation incrementality by only recompiling downstream
dependents when the body of a function changes"
([bazel/third_party/ijar](https://github.com/bazelbuild/bazel/tree/master/third_party/ijar)).
Gradle states the rule as a property: "if project A depends on project B and a
class in B is changed in an ABI-compatible way (typically, changing only the body
of a method), then Gradle won't recompile A"
([Compilation Avoidance](https://blog.gradle.org/compilation-avoidance)).

The `exports` key above is an ABI in exactly that sense — names, owners and
signatures, no bodies — and it is derived the same way, from the parsed source
rather than from its bytes.

Gradle also draws the distinction that turns out to decide this whole question:
*compilation avoidance* means "avoiding calling the compiler altogether for a
given project", while *incremental compilation* "does mean calling the compiler,
but makes an attempt to reduce the amount of code needed to be recompiled"
([An In-depth Look at Gradle's Approach to Faster
Compilation](https://blog.gradle.org/our-approach-to-faster-compilation)).
Section 5 is about which of the two this vault can actually get.

One qualification carries over as a warning rather than a rule: Gradle
*deactivates* compile avoidance when annotation processors are on the compile
classpath, "because for annotation processors the implementation details
matter". The equivalent hazard here is any extractor that reads more of a
dependency than its definitions. Today none does — §2 fact 1 is what proves it —
so the assumption is load-bearing and belongs in a test rather than in folklore.

### 3.2 Salsa / rust-analyzer — early cutoff, and what licenses it

Salsa's red-green algorithm floods the dependency graph backwards from a changed
input and stops "when we hit a query whose result is unchanged despite a changed
input (early cutoff)". The canonical example is precisely the one measured
below: "changing the input source code to include an extra whitespace does not
change the AST structure", so results that depend on the AST but not on the byte
stream are reused
([The red-green algorithm](https://salsa-rs.github.io/salsa/reference/algorithm.html),
[Durable Incrementality](https://rust-analyzer.github.io/blog/2023/07/24/durable-incrementality.html)).

The structural lesson is that early cutoff needs a *derived* value to compare —
a parse, not a hash of the file. A fingerprint computed from the content digest
can never cut anything off, because it is a bijection with the bytes. That is
this defect stated in one sentence.

### 3.3 ccache — the same idea at the other end of the stack

ccache's default `direct` mode hashes the source and the headers it read;
`preprocessor` mode hashes preprocessor *output* instead, and by default
**discards comments before hashing**, exposed as `keep_comments_cpp`, whose
default is false ([ccache manual](https://ccache.dev/manual/4.13.3.html)). So a
comment-only edit is a cache hit by default in a tool whose whole job is not to
be wrong. It is worth noting the direction of the trade: ccache's faster mode is
the *less* precise one, and the manual keeps the preprocessor fallback for the
cases direct mode cannot answer.

### 3.4 Zinc / sbt — name hashing, and the exemption

Zinc narrows a conservative over-approximation by asking whether dependent files
*mention* the changed names, and exempts dependencies introduced by inheritance
from name hashing precisely because the compiler reads more than a name there
([Understanding Recompilation](https://www.scala-sbt.org/1.x/docs/Understanding-Recompilation.html)).
The exemption is the same shape as the Gradle annotation-processor carve-out and
points at the same invariant: narrow only as far as what the analysis actually
reads.

## 4. Measured: how many real edits are invisible?

The model of the interface is not asserted, it is checked against the product.
`interface_fingerprint.definition_entries` predicts the definitions a source
contributes; `code_extractor` emits one node per definition carrying the exact
`identity_key`. On **all 143 `scripts/*.py` files** the predicted identity keys
equal the emitted ones exactly — 143/143, no source-only and no product-only
entries. (An earlier draft descended into `if`/`try`/`with` bodies and was a
strict superset on 7 files; the product's `walk` descends only into class and
function bodies, so a conditionally-defined class enters no index and is
genuinely unreadable. Matching the product made it exact.)

Applied to this repository's own history — every `(commit, .py file)` pair in
the last 1500 commits, comparing the interface before and after:

| outcome | count | share |
|---|---|---|
| bytes unchanged in that commit | 5272 | 59.2% |
| **visible** — interface moved | 2249 | 25.2% |
| **invisible** — interface identical | 871 | 9.8% |
| file added or deleted | 520 | 5.8% |
| total file-edits examined | 8912 | |

**Of the 3120 content edits to existing Python files, 871 are
dependent-invisible — 27.9%.** So the premise "almost every real edit changes an
export or a signature" is false for this repository: better than one edit in
four is a comment, a docstring, a local rename, a changed default, or a body
rewrite that no other source can see.

### 4.1 But a file is the wrong unit

A delta pays only if **every** changed code source in it is invisible: one
visible edit anywhere in the code universe re-arms workspace invalidation and
the batch. The realistic unit is therefore the delta, and the smallest
realistic delta is one commit — a nightly pass usually accumulates a day of
them. Over the same 1500 commits, counting any non-Python code source and any
unreadable pair as visible:

| commit | count | share |
|---|---|---|
| touches a code source, **some edit visible** | 990 | 90.8% |
| touches a code source, **all edits invisible** | 65 | 6.0% |
| touches no code source | 35 | 3.2% |

**Of the 1055 commits that touch a code source, 65 — 6.2% — are entirely
dependent-invisible.** The per-file 27.9% does not survive aggregation, and a
nightly delta spanning many commits would drive it lower still.

### 4.2 One hole in the model, invisible today

The interface in §2 is "the definitions a source contributes", and §2 fact 1
says every `add_occurrence` uses role `"definition"`. Three of those calls are
not function or class definitions:

- `_entry_point` adds an `entry-point` node when a function named `main` exists
  **and** `main()` is called inside `if __name__ == "__main__":`.
- `_routes` adds a `route` node for a recognised web framework decorator.
- `_table` adds a `table` node for a class body containing
  `__tablename__ = "<literal>"`.

All three therefore enter `node_sources`, and `node_sources` is what
`source_dependencies` is computed from. An exports fingerprint that tracks only
`def` and `class` misses them.

Entry-points turn out to be harmless, and the reason is worth stating precisely:
`candidate_node_ids` is only ever `_resolve_expression(...)`,
`_candidate_modules(...)` or `tables`, and none of those can return an
entry-point id — an entry-point is reachable only through the `EXPOSES`
assertion from its own function. So `node_sources[entry_point]` is never
consulted, and the 97 entry-point nodes in the live generation cannot move any
dependent.

**Tables are not harmless.** `add_observation(..., candidate_node_ids=tables)`
means a table id *can* become a `source_dependency`, and a `__tablename__`
literal lives inside a class body — exactly the region an ABI is supposed to
ignore. Today this cannot bite: the live generation contains **0** table nodes
and **0** route nodes, because this vault uses raw `sqlite3` rather than an ORM
(the only `__tablename__` occurrences in the tree are string literals inside
`code_extractor.py` and two test files, which parse as constants, not as class
bodies). But the extraction path is live and tested, so the moment anyone adds
an ORM model, an exports fingerprint of the shape built here would silently
produce an incremental build that differs from a full build.

That is a latent correctness hole with, as §5 shows, no measured benefit
purchased against it.

## 5. Measured: and it is worth nothing

The rebuild set does collapse. On the live manifest (910 sources, 671 code, 422
workspace-sensitive code):

| delta | rebuilt sources |
|---|---|
| any *semantic* code edit (today's rule, post-`e05c79a`) | **424 of 910** |
| an *invisible* code edit under real fingerprints | **1 of 910** |

The pure dependency closure is not what makes 424. It is tiny: **497 of 671**
code sources have a closure of exactly one — themselves — and the median closure
is 1. The 424 is almost entirely the workspace-sensitive set, invalidated
because a semantic change occurred anywhere in the code universe.

A 424-to-1 collapse ought to be worth a great deal. It is worth nothing, for a
reason that is structural and not a tuning problem:

1. `_initial_rebuild` returns `added | changed`, so **the edited source is always
   in `rebuild`**, invisible or not.
2. `doctor._SourceExtractionAdapter._code_result` memoizes one call to
   `extract_code(self.code_sources, …)`, and `_code_extraction_sources` is
   **every** non-`knowledge/` source in the snapshot — not the rebuild set.
3. Therefore any code edit puts at least one code source in `rebuild`, which
   forces the single batched `extract_code` over all 671 code sources, at full
   price.

In Gradle's vocabulary: real fingerprints can buy *incremental compilation*
here, never *compilation avoidance*. The extractor is invoked either way; only
the number of sources whose records are merged fresh rather than read from the
parent changes. And that trade is close to zero-sum — the parent's records have
to be read and re-inserted instead.

### 5.1 The paired arms

Both arms run the product's own maintenance pass
(`doctor._build_or_refresh_generation`) against a **frozen copy** of this
vault's real sources (910 sources, 671 of them code) into a throwaway state
root. The corpus copy is byte-identical between arms and never edited except by
the delta stage itself, which reverts in a `finally`. The product code is a
**pinned copy** of `scripts/` taken at commit `098f480`, because this checkout
is shared and other agents commit to it mid-run — HEAD did in fact move from
`e05c79a` to `098f480` while these builds were running. The arms differ by
exactly one thing: arm B's `doctor._source_extraction` calls
`interface_fingerprint.fingerprints`, which derives `exports` from the parsed
source and pins the other four keys.

Vectors are switched off on both sides via
`search_memory.build_generation_vectors_if_available`, so the comparison is of
records. The machine carried a load average of 12–14 throughout (other agents),
so **CPU seconds are the comparable figure**; wall seconds are reported only for
scale.

Arm B's parent generation confirms the wiring: of 910 sources, 429 of the 430
`.py` sources carry interface-derived fingerprints (one is deliberately
unparsable and falls back), and the other 481 non-Python sources fall back to
the content digest. Arm A's parent confirms the control: **910 of 910**
fingerprints equal `sha256(f"{key}:{sha256(content)}")`.

The delta in both arms is the same invisible edit — one comment line appended to
`scripts/reliable_memory.py`.

| arm | rebuilt / reused | CPU s | wall s | `extract_code` calls |
|---|---|---|---|---|
| A — content-derived (shipped) | 423 / 487 | **155.09** | 385.7 | one call, **671 sources** |
| B — real `exports` | **1 / 909** | **155.98** | 349.9 | one call, **671 sources** |

**The rebuild set collapses by a factor of 423 and the pass does not get faster.**

The last column is the whole explanation, and it was instrumented rather than
inferred: `extract_code` is called exactly once in both arms, with all 671 code
sources, whether the builder asked for 423 of them or for one. Arm B reuses 909
sources' records instead of 487, and what it saves on merging it pays back on
reading the parent's records — the two are close to zero-sum, which is why 423
fewer rebuilds is worth nothing.

### 5.2 Repeats, and the run-to-run spread

One pair proves nothing about a difference this small, so both arms were
repeated from a restored copy of their own parent state, arms interleaved:

| run | arm | rebuilt / reused | CPU s | wall s |
|---|---|---|---|---|
| 1 | A — content-derived | 423 / 487 | 155.09 | 385.7 |
| 2 | A — content-derived | 423 / 487 | 153.40 | 481.0 |
| 1 | B — real `exports` | 1 / 909 | 155.98 | 349.9 |
| 2 | B — real `exports` | 1 / 909 | 156.33 | 385.8 |

Means: **A 154.25 s, B 156.16 s — arm B is 1.9 s (1.2%) slower.** Arm A alone
varies by 1.69 s between two runs of identical code on an identical corpus, so
the effect sits right at the edge of what this stand can resolve. What the four
runs do settle is the direction: both B runs are slower than both A runs, and
none of them is faster. Reusing 909 sources' records instead of 487 means
reading more rows out of the parent database than the 422 avoided merges save,
which is the zero-sum trade named above, tipped very slightly the wrong way.

Wall seconds vary far more (385.7 against 481.0 for the *same* arm) because the
machine carried other agents' work throughout; that is why CPU seconds are the
reported figure.

The `extract_code` batch size was `[671]` in all four runs — the finding the
whole note rests on, and the one number that never moved.

## 6. What this supports

**Do not make the fingerprints real. Say in the code that they are not, and
why.**

The recommendation is not the one the defect report expected, and neither is the
reason. The expected reason was "almost every real edit changes an export, so
there is nothing to cut off". That is false here: 27.9% of file edits and 6.2%
of code-touching commits are genuinely invisible to every dependent. There is
plenty to cut off. The reason the cut is worthless is that **this vault has no
place to put the saving**:

- Extraction is the only reuse-gated phase, and it is batched per universe. One
  rebuilt code source costs the same `extract_code(671 sources)` as 423 do, so
  the edited source — always in `rebuild` by `_initial_rebuild` — always buys
  the whole batch. Gradle would call this a system that can do incremental
  compilation but not compilation avoidance; the avoidance case, a delta with
  *no* code source in it, is exactly what `e05c79a` already captured by scoping
  invalidation to universes.
- Everything else in a delta pass — writing the whole graph database,
  re-validating it, collecting the corpus, publishing — scales with corpus size,
  not with the delta, because a generation is immutable by contract. Measured in
  the sibling note: about 100 s of a ~110 s pass.

So the honest ledger is a 423x smaller rebuild set, **about 1.9 s slower**, against a
latent correctness hole (§4.2) that would surface the first time an ORM model
enters the tree. Rule 4 asks for reliable before fast; this trade is unpaid risk,
and taking it would be shipping machinery that pays for itself only on comment
edits — and not even then.

What *is* worth doing, and is done: stop the fiction from reading as an
accident. `doctor._source_extraction` now says in a docstring that the five keys
are one value under five names on purpose, that only `exports` could ever be
defined, and what the measurement was;
`tests/test_invalidation_fingerprints.py` pins the behaviour and tells the next
author to delete the test and bring a new measurement if they change it. The
keys cannot be removed — `evidence_graph_builder._require_invalidation_fingerprints`
requires exactly those five, as lowercase SHA-256 digests, and that module is
out of scope here — so naming the choice is the available form of deleting the
fiction.

### When this should be revisited

The conclusion is contingent on one fact, and it is worth stating the trigger
precisely. If extraction ever stops being batched over the whole universe — a
per-source extractor, or a batch built from the rebuild set rather than from the
snapshot — then the 423× collapse becomes a real 24.5 s of `extract_code` saved
on 6.2% of deltas, and the arithmetic changes. The table hole in §4.2 would have
to be closed first: an `exports` fingerprint must cover every node the source
*defines*, and `_table` and `_routes` define nodes from inside a class or
function body.

## 7. What was measured and what was not

Measured, on this repository and on a frozen copy of it: the fingerprints are
content-derived (910/910); the interface model matches the product exactly
(343/343 files across `scripts/` and `tests/`); the invisible-edit share
(27.9% of file edits, 6.2% of code-touching commits); the dependent closure
(median 1, 497 of 671 code sources have none); the rebuild sets (423 against 1);
the delta CPU (mean 154.25 against 156.16 over two runs each); and the
extraction batch size (671 in
both arms).

Not measured, and not claimed: that arm B produces a generation byte-identical
to a full build. The full-versus-incremental comparison across all seven tables
was not run, because the decision does not turn on it — a change that is not
being shipped does not need its equality proof, and §4.2 already names a
construction under which arm B *would* diverge. Anyone reviving this must run
that comparison first; the comparator used by the sibling note is the technique.

## Sources

- [bazel/third_party/ijar](https://github.com/bazelbuild/bazel/tree/master/third_party/ijar)
- [Compilation Avoidance — Gradle blog](https://blog.gradle.org/compilation-avoidance)
- [An In-depth Look at Gradle's Approach to Faster Compilation — Gradle blog](https://blog.gradle.org/our-approach-to-faster-compilation)
- [The "red-green" algorithm — Salsa](https://salsa-rs.github.io/salsa/reference/algorithm.html)
- [Durable Incrementality — rust-analyzer blog, 2023](https://rust-analyzer.github.io/blog/2023/07/24/durable-incrementality.html)
- [ccache manual 4.13.3](https://ccache.dev/manual/4.13.3.html)
- [Understanding Incremental Recompilation — sbt reference manual](https://www.scala-sbt.org/1.x/docs/Understanding-Recompilation.html)
- [What a changed source may invalidate](2026-08-29-what-a-changed-source-may-invalidate.md) — the sibling note this one continues
