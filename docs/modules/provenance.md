# Provenance ranking policy

This owner retains the complete existing `scripts/provenance.py` without changing
its bytes from the shared corpus-policy correction. The full immutable corpus
membership implementation and guide are dependencies in this maintenance packet.
There is no collector, database, model or runtime-state dependency in the policy.

Existing public weight tables remain mutable dictionaries. Authority and page
type weights, unknown defaults, path handling and normalization are unchanged.
An unstated claim-page authority remains inferred; other sources keep their
existing default. The conditional curated-pages prior is disabled only by a
positive code-shaped intent. Under that prior, code-root commentary cannot be
lifted above its existing type weight. `trust_weight` retains its authority/type
product; this module does not add the conditional prior to that formula.

Substance weighting preserves the existing link-density and link-led-list rules,
bare-heading behavior, navigation weight and neutral unknown/empty inputs. No
threshold or interpretation is revised. Full source and historical rationale
remain available; this guide is not a substitute interface for a source cut.

## Test ownership

Five original pure test definitions move intact into this owner: unstated-page
authority, session authority ordering, positive intent detection, code-root
demotion, and the parameterized link-density cases. Their function/decorator ASTs
and all assertions remain identical. RELATED, ANNOTATED and PROSE fixtures move
once into `tests/modules/provenance/fixtures.py`; remaining integration tests
import the same immutable values. No original scenario is discarded.

Other functions in the four existing mixed test files remain unchanged. Search
fallback, fusion, generation and session-capture tests still run through their
actual entry points. Those integration files have separate future ownership;
their broad execution dependencies are not smuggled into this pure owner or
counted as already decomposed. Static and subprocess checks establish the local
policy dependency, including an actual weighted read without corpus collection.

## Qualification — 2026-11-03

The approved whole-project plan permits this responsibility boundary. The
preceding corpus-policy transfer removed the reverse collector import; this step
keeps algorithms unchanged and makes existing tests independently maintainable.
Alternatives were duplicating tests/data or carrying the whole retrieval/capture
implementation in every policy context. Exact test transfer and a single shared
fixture owner preserve evidence with less coupling.

Current architectural research is the same three independent primary sources
recorded in the complete [corpus membership guide](corpus-membership-policy.md):
Fowler bounded contexts, Import Linter dependency contracts and PEP 544. No new
runtime interface or dependency is introduced. The four historical ranking notes
are included whole, preserving old observations as dated evidence.

Original mixed-file baseline: 37 tests passed. Graph/source/fixture consumers were
checked before transfer. Run this owner, the four original mixed test files,
retrieval/search integration, and the complete ownership/source/budget gates.
Every full packet still must fit 100000 o200k_base tokens. No full-cycle LLM cost
saving, product completion, release or live installation is implied.
