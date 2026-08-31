---
type: raw-source
status: active
confidence: high
source_authority: web
created: 2026-08-29
---

# What a changed source may invalidate

One-sentence summary: an invalidation rule is sound only when it follows the
structure the analysis actually reads, so a source whose unresolved reference
cannot be attributed to named candidates must be re-run when *its own*
resolution universe moves — and not when some other universe moves, because
nothing it read can have changed.

Dated research for the change to
`evidence_graph_builder._workspace_invalidated`, which today re-extracts every
workspace-sensitive source in the generation whenever *any* source in the
generation changes.

---

## 1. The question

The incremental generation builder reuses a parent generation's records for
sources whose bytes did not move. On top of the exact dependency closure it
carries one blunt rule (`scripts/evidence_graph_builder.py`, `_workspace_invalidated`):

```python
if not semantic_changes or membership_changed:
    return set()
return (_workspace_sensitive_source_ids(parent_entries) & current_ids) - rebuild
```

A source is `workspace_sensitive` when its extraction emitted a
`missing_dependency` or `unresolved_reference` observation that the extractor
could *not* attribute to candidate sources (`doctor._workspace_sensitive_observation`).
That is an open question asked of the whole workspace rather than of a named
neighbour, so the answer may change when the workspace changes — hence: rebuild
it.

The rule as written asks nothing about *which* workspace changed. Measured on
this vault's live active generation
(`generation-18d015d5499fc78e-7d4cc1d8`, 860 sources): 431 sources are
workspace-sensitive, 399 of them code and 32 of them knowledge notes. So
appending one line to one knowledge page invalidates 399 workspace-sensitive
*code* sources, and the dependency closure then drags in their dependents —
488 of 860 sources rebuilt for a one-page edit, 400 of them code. The same
happens in the other direction: editing one code file rebuilds 88 knowledge
sources.

The question this note has to settle before the rule is narrowed: **what
correctness property does the blunt rule protect, and is a per-universe rule
still sound?**

## 2. What the product actually reads

Extraction is not one analysis. `doctor._SourceExtractionAdapter._result_for`
routes each source into one of three disjoint batches by path prefix:

| universe | membership | batched by |
|---|---|---|
| code | `not relative_path.startswith("knowledge/")` | `extract_code(code_sources, …)` |
| knowledge | `knowledge/` but not `knowledge/projects/` | `extract_knowledge(knowledge_sources, …)` |
| project journal | `knowledge/projects/` | `extract_projects((one_source,), …)` — one source, alone |

`extract_code` is handed `_code_extraction_sources(snapshot)` and nothing else.
It never sees a knowledge page's bytes. Therefore no observation it emits — and
no reference it fails to resolve — can be answered by a knowledge page. The
converse holds for `extract_knowledge`, which is handed only `knowledge/`
sources that are not project journals. A project journal is extracted on its
own, so it has no universe to be sensitive to at all; measured on the live
manifest, 0 of 132 project journals are workspace-sensitive.

Two further measurements on the same live manifest, as a cross-check that the
static split is the split the data has: **0** code sources declare a
`source_dependency` on a knowledge source, and **0** knowledge sources declare
one on a code source.

The builder already knows this boundary. `_workspace_source_ids` — the
predicate `_workspace_membership_changed` uses to decide whether the *workspace
surface* moved — is exactly "not under `knowledge/`", i.e. universe 1. So the
boundary is declared once already; the invalidation rule simply does not
consult it.

## 3. Current practice

### 3.1 Salsa / rust-analyzer — durability, and why it is sound

Salsa assigns each input a *durability* and keeps a version vector rather than a
single version counter; a change to a volatile input bumps only its own
component and the less durable ones, so a query that reads only durable inputs
can skip validation entirely
([Durable Incrementality](https://rust-analyzer.github.io/blog/2023/07/24/durable-incrementality.html)).
The sentence that matters here is the justification, not the mechanism: the
optimisation is "sound rather than speculative" because "durability assignments
respect actual dependency structure — stdlib truly cannot depend on user code."

That is the same argument in the same shape. A knowledge page truly cannot
resolve a code reference, because `extract_code` is never handed one. The
narrowing is sound for the same reason Salsa's is, and it fails for the same
reason Salsa's would: if the assignment ever stopped matching what the analysis
reads.

Salsa also records dependencies *per field*, so "if a query reads only certain
fields, changing another field does not invalidate that query"
([salsa overview](https://salsa-rs.github.io/salsa/overview.html)). Read-set
fidelity, not change magnitude, is what licenses reuse.

### 3.2 Zinc / sbt — conservative approximation, stated as such

Zinc tracks dependencies at source-file granularity and is explicit that "sbt
cannot determine precisely which dependencies have to be recompiled; the goal is
to compute a conservative approximation"
([Understanding Recompilation](https://www.scala-sbt.org/1.x/docs/Understanding-Recompilation.html)).
Name hashing narrows that approximation by asking whether dependent files
*mention* the changed names; dependencies introduced by inheritance are exempted
from name hashing and invalidate more broadly, precisely because the analysis
there reads more than a name.

Two things carry over. First, a conservative over-approximation is legitimate —
this vault's `workspace_sensitive` flag is one, and stays one. Second, Zinc
narrows its over-approximation by consulting what the compiler actually read,
and exempts the case where it read more. Narrowing by *what the extractor was
handed* is the same move.

### 3.3 Bazel / Buck2 — the cost of an over-wide edge

Bazel invalidates bottom-up through the graph and then prunes: a node whose
recomputed value equals its old value "resurrects" the nodes invalidated
because of it ([Skyframe](https://bazel.build/reference/skyframe)). Buck2's DICE
engine tracks dependencies more finely than Bazel with the same aim, and
separates the core from language-specific rules
([Why Buck2](https://buck2.build/docs/about/why/)).

The literature also names the failure mode directly: "underutilized
dependencies reduce the effectiveness of incremental builds, as a compilation
unit that depends on a very small part of a dependency might need to be rebuilt
due to changes in other parts of the dependency, even if the changes are
unrelated" ([The Cost of Downgrading Build
Systems](https://arxiv.org/pdf/2510.20041)). An edge from "any source changed"
to "every workspace-sensitive source" is the extreme case: the dependency is
not merely underutilized, it is absent.

Change pruning is worth noting as the alternative this vault does **not** have.
Bazel can afford a wide invalidation edge because recomputing a node cheaply and
finding the value unchanged costs only the recomputation. Here, re-extraction is
the expensive step *and* its result is not compared against the parent's — a
rebuilt source's records simply replace the reused ones. So a wide edge costs
full price every time.

## 4. What the blunt rule gets wrong in the other direction

Reading the rule against the three universes turns up two places where it is not
conservative but simply blind. Both are consequences of having one global switch
instead of one per universe.

1. `membership_changed` suppresses **all** workspace invalidation
   (`if not semantic_changes or membership_changed: return set()`). It is safe
   for the code universe, because a membership change there already puts every
   current workspace source into `rebuild` (`_incremental_delta`). It is not
   safe for the knowledge universe, which that rebuild does not cover: add one
   code file in the same pass as a knowledge edit, and the 32 workspace-sensitive
   knowledge notes are skipped.

2. `_semantic_changes` iterates `changed` only, and `changed` is
   `current_ids & previous_ids` — an **added** source can never be a semantic
   change. For the code universe `_workspace_membership_changed` covers adds and
   deletes. For the knowledge universe nothing does: adding a knowledge page,
   which is exactly the event that can resolve a note's dangling wikilink,
   invalidates nothing. This is the "should an added source invalidate its
   dependents?" half of the question, and the honest answer is that an added
   source has no dependents yet — what it can change is the *resolution
   universe*, so it belongs with membership, not with content.

A second, smaller finding worth recording because it explains why the rule is so
wide in practice: `invalidation_fingerprints` carries no semantic information at
all. `doctor._source_extraction` computes every one of its five keys as
`sha256(f"{key}:{sha256(content)}")`. Verified against the live manifest: for
every source, the stored fingerprints equal that function of the stored content
digest. So `_semantic_changes` is, today, exactly `changed` — every byte-level
change is a "semantic" change, and the extra machinery buys nothing until an
extractor computes the five keys from real exports/imports/signatures.

## 5. The rule this supports

Invalidate a workspace-sensitive source only when *its own* resolution universe
moved. Membership of a universe is the boundary the builder already declares in
`_workspace_source_ids`, so there is one definition and two readers rather than
two rules that disagree. A universe "moves" when a source inside it changed
content, was added, or was deleted.

Two buckets, not three. The builder splits workspace from non-workspace, so
knowledge notes and project journals share the second bucket: editing a project
journal still invalidates every workspace-sensitive knowledge note. That is
over-broad and deliberately so — a third bucket would buy the 0.09 s that
`extract_knowledge` costs, and a project journal is never workspace-sensitive
anyway (0 of 132 on the live manifest), so it can only ever be the trigger, not
the victim.

Membership is read from **both** sides, current and parent. A deleted source's
universe is named only by its parent entry, and a source moved across the
boundary belongs to both universes until the move has been accounted for. The
alternative — deriving the second universe as the complement of the first —
would silently file a `knowledge/ → scripts/` rename as a workspace event only.

What is preserved: the conservative flag itself. A source that could not
attribute its unresolved reference is still re-extracted on *any* change inside
its universe, however unrelated — this note does not claim to know which page
would have resolved the link, only which pages could possibly have.

What is not claimed: that the three universes are a property of the builder. They
are a property of `doctor._SourceExtractionAdapter`, the only extractor any
production or benchmark caller passes
(`doctor`, `repository_index`, and the three `benchmark/*_vault.py` entry points
all call `doctor._generation_source_extractor`). The builder's assumption is not
new — `_workspace_membership_changed` has always relied on it — but the change
makes the assumption load-bearing in a second place, so it is pinned by a test
rather than left as folklore.

## 6. Measured

Every number below comes from `doctor._build_or_refresh_generation` — the
product's own maintenance pass — run against a frozen copy of this vault's real
sources (884 sources) into a throwaway state root, with a parent generation
seeded by one full build. The corpus is frozen because a full build on the live
vault defers with `corpus_changed` (2026-08-24).

Two things had to be pinned before the arms could be compared, and the first
attempt was thrown away because they were not. The product code is a **copy** of
`scripts/` taken once, because this checkout is shared and two other agents
committed to it during the first matrix. The arms then differ by exactly one
thing: the control arm restores the pre-change `_expanded_rebuild` verbatim by
monkey-patch — the `NEW-138` technique — and the control reproduces the shipped
pre-change counts (503 rebuilt / 381 reused) exactly, which is what makes it a
control. The machine carried a load average of 4–9 throughout, so **CPU seconds
are the comparable figure** and wall seconds are reported only for scale. Two
runs per arm, arms interleaved.

### 6.1 Where a delta pass goes

One knowledge page edited, one line appended; control arm; 503 of 884 sources
rebuilt, 381 reused; **136.4 and 134.0 s CPU**:

| phase | CPU s | share |
|---|---|---|
| write the whole graph database | 60.6, 60.6 | 45% |
| re-validate the finished generation (`catalog._validate_candidate`) | 27.7, 28.6 | 21% |
| `extract_code` over all 623 code sources | 24.5, 23.0 | 18% |
| vectors, total | 9.2, 8.6 | 7% |
| — of which asking the model for new rows | 1.7, 1.1 | 1% |
| collect the corpus | 2.5 | 2% |
| merge records | 2.5, 1.9 | 1.6% |
| activate and publish | 1.7, 1.8 | 1.3% |
| build the record-dependency rows | 1.5, 1.2 | 1% |
| write the manifests | 1.0 | 0.7% |
| read the parent's reused records | 0.9, 0.8 | 0.6% |
| build the FTS index | 0.7 | 0.5% |
| `extract_knowledge` over all 105 notes | 0.09 | 0.06% |

A full build for scale: 884 rebuilt, 737.6 s CPU, of which 602.9 s is embedding
3401 fresh chunks. An idle pass — corpus unchanged — returns `status: current`
in 6.7 s wall / 4.2 s CPU and builds nothing.

**This corrects the register.** `NEW-138` concluded that restored record reuse
bought no time because "the expensive half of `283eb3a` is embedding reuse, and
it is not scope-gated". Embedding reuse works and is nearly free on a delta:
3400 of 3401 chunks come from the parent and the model is asked for one, costing
1.1–1.7 s of 135. Record reuse bought no time for two different reasons. The two
phases that dominate — writing the whole graph database and re-validating it —
are functions of *corpus size*, not of the delta, and are not reuse-gated at
all. And the one phase that is reuse-gated, extraction, was still being paid in
full: a one-line edit to a wiki page invalidated the entire code workspace, and
`extract_code` batches all 623 code sources the moment one of them is rebuilt.

### 6.2 What a knowledge-page edit costs, and what a code edit costs

They are different questions, and the vault's own writers touch knowledge pages
constantly — every compile rewrites `knowledge/index.md` and `knowledge/log.md`,
every session writes an evidence record, every consolidation appends to a daily.

| delta | arm | rebuilt / reused | CPU s | wall s |
|---|---|---|---|---|
| one knowledge page | control | 503 / 381 | 136.4, 134.0 | 142.9, 156.7 |
| one knowledge page | scoped | **89 / 795** | **108.0, 111.5** | 117.0, 115.2 |
| one code file | control | 503 / 381 | 135.3, 133.8 | 166.3, 135.8 |
| one code file | scoped | **414 / 470** | **132.6, 134.3** | 165.2, 161.4 |

A **knowledge-page edit** loses `extract_code` entirely (24.5 → 0.0) and its
record merge falls from 2.5 to 0.7 s; reading the parent's records rises from
0.9 to 1.6 s, which is what reuse costs. Net **−25 s CPU, −19%**.

A **code edit** gains nothing measurable: −1.1 s against a run-to-run spread of
1.5 s on the control side and 1.7 s on the scoped side. That is the honest shape
of it. The code universe still rebuilds, and the 88 knowledge sources it stops
dragging along cost 0.09 s to extract. The change is worth having for a code
edit only because it stops rewriting 88 sources' records for no reason — not
because the clock notices.

The same closure computed against the live vault's own active generation
(`generation-18d015d5499fc78e-7d4cc1d8`, 860 sources) gives 488 → **88** for a
knowledge page, 489 → **401** for a code file, and 489 → **89** for a project
journal.

### 6.3 What is left, and why no reuse policy can take it

After the change a knowledge-page delta costs 108–111 s CPU against a floor of
about 100 s that reuse cannot touch: ~60 s to write every merged record into a
fresh database, ~28 s to re-validate that database, ~9 s of vector assembly and
model load, ~2.5 s to collect the corpus, ~2 s to activate and publish. Those
scale with the corpus, not with the delta, because a generation is immutable by
contract — it is rewritten whole or not at all.

So the honest answer to "why does a delta pass still cost 100 s" is that **the
remaining cost is irreducible without giving up generation immutability**, and
nobody should trade that for a minute a night on a pass that runs at 03:00.
What was reducible was the 25 s of extraction that a wiki-page edit had no
business paying, and that is now gone.

### 6.4 The old rule was also wrong, not only wide

Proved by full-versus-incremental comparison of **every table** of
`evidence.sqlite3` — `source`, `node`, `occurrence`, `assertion`, `evidence`,
`observation`, `dependency` — both generations built from the same frozen corpus
with the same pinned code, rows ordered by every column and hashed. Vectors are
switched off on both sides so the comparison is of records: a re-batched
embedding differs in float32 by design
(`docs/research/2026-08-28-what-a-rebuild-may-reuse.md`).

| delta | arm | incremental rebuilt | all 7 tables equal to a full build? |
|---|---|---|---|
| knowledge page edited | scoped | 89 / 884 | **yes** |
| code file edited | scoped | 414 / 884 | **yes** |
| knowledge page **added** | scoped | 90 / 885 | **yes** |
| knowledge page **added** | control | 1 / 885 | **no** — 3 tables differ |

Row counts and per-table SHA-256 for the added-page pair, which is the one that
separates the two rules:

| table | scoped inc | scoped full | equal | control inc | control full | equal |
|---|---|---|---|---|---|---|
| `source` | 885 | 885 | yes | 885 | 885 | yes |
| `node` | 29 813 | 29 813 | yes | 29 813 | 29 813 | yes |
| `occurrence` | 26 318 | 26 318 | yes | 26 318 | 26 318 | yes |
| `assertion` | 73 210 | 73 210 | yes | **73 207** | 73 210 | **no** |
| `evidence` | 153 466 | 153 466 | yes | 153 466 | 153 466 | **no** (content) |
| `observation` | 80 256 | 80 256 | yes | **80 259** | 80 256 | **no** |
| `dependency` | 0 | 0 | yes | 0 | 0 | yes |

The scoped incremental generation hashes to `170562cc1332e3b5…` on `assertion`
and `ddb6b3c922877afa…` on `observation`, byte for byte what the full build
produced.

The last two rows are the finding. Under the old rule an added source was never
a semantic change, and `_workspace_membership_changed` does not watch the
knowledge universe, so adding one page rebuilt only itself — and the resulting
generation differed from a full build on three tables: `assertion` 73 207 rows
against 73 210, `observation` 80 259 against 80 256, and `evidence` equal in
count but not in content. The added page satisfies three wikilinks that were
dangling; the incremental build kept all three recorded as unresolved, and
nothing would ever have revisited those notes. An incremental build that differs
from a full build is wrong, and that one did. Under the scoped rule the same
delta comes out byte-identical to a full build.

## Sources

- [Durable Incrementality — rust-analyzer blog, 2023](https://rust-analyzer.github.io/blog/2023/07/24/durable-incrementality.html)
- [Salsa — Overview](https://salsa-rs.github.io/salsa/overview.html)
- [Understanding Incremental Recompilation — sbt reference manual](https://www.scala-sbt.org/1.x/docs/Understanding-Recompilation.html)
- [Skyframe — Bazel documentation](https://bazel.build/reference/skyframe)
- [Why Buck2 — Buck2 documentation](https://buck2.build/docs/about/why/)
- [The Cost of Downgrading Build Systems: A Case Study of Kubernetes (arXiv 2510.20041)](https://arxiv.org/pdf/2510.20041)
