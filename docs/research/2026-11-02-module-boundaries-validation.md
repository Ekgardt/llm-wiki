# Checking the modularity draft against laws 4, 6 and 7

2026-11-02. Research and a pilot draft; no architectural decision has been made.
It supplements and refines [the initial scheme](2026-11-02-module-boundaries-and-token-budget.md).
Code, structure, env, runtime contracts and the installed version were not changed.

## Outcome of the review

The fixed number 74 is withdrawn as a target result. There are
measured files and boundary candidates, but no basis to declare this specific
layout optimal. The user's requirement of ≤100,000 tokens stays.
It does not allow hiding dependencies, tests or needed data.

One product should be kept, and boundaries developed for confirmed reasons.
The first proposed pilot is now the contract of receipt and source formats.
Input/output and ACL are not proposed as the first pilot: the area touches critical
publication and Windows permissions; getting a comparable check of all these paths here
is harder than checking deterministic formats. This is a choice by risk and observed
coupling, not a statement that file security matters less.

The pilot is not implemented yet. Below are its concrete source definitions,
dependencies, consumers, test base, forbidden simplifications and admission criteria.
Neither a token gain nor meeting the final limit is declared.

## 1. Real spending: what file sizes do not explain

The local journal of a previously restored project session was read. Only
aggregates and episode identifiers were kept; the source transcript was not copied into documentation.
Two finished work episodes of 2026-10-20 with explicit outcomes were taken: the fix
`3e1def7e` and the next diagnosis of keeping the claims tree in memory. This is not
a random sample and not the whole multi-day item 7; the results must not be extended to
all tasks. The choice allows linking spending to observed actions.

Usage is summed over unique `(turn_id, response_id)` from `token_usage_record`.
Cached input tokens are already included in input. Reasoning is already included in output;
these fields are not added again. Total tokens were not converted to money.
The visible tool output was additionally measured with `o200k_base`; it is not equal to
the provider's accounting of the full request. Both episodes were recorded with the model `gpt-6-astra`;
this is not proof of the cost of the planned cycle on another model.

| Metric | Citation binding fix | Memory diagnosis |
|---|---:|---:|
| Recorded model responses | 24 | 11 |
| All input tokens | 1 284 200 | 699 656 |
| Of them cached | 1 238 656 | 682 240 |
| Uncached input | 45 544 | 17 416 |
| Output | 11 082 | 2 065 |
| Total input + output | 1 295 282 | 701 721 |
| Input range of a single response | 32 919–200 445 | 55 181–71 019 |
| Visible tool results, o200k | 16 493 | 15 553 |
| Fully identical repeated tool results | 0 | 0 |
| Episode duration from the journal | 334.834 s | 104.081 s |

**Conclusion:** large files were not read whole on each request. Most of the
total input of these episodes was cached. Renaming files alone
does not remove the accumulated history and the repeated sending of context. Analysing the journals
does not prove that all these tokens were superfluous, and does not establish causation
between a specific import link and spending. A comparison on identical
tasks with preserved quality is needed, including checks and fixes.

The largest single output in the first episode is 4 642 tokens; the request concerned
`compile_memory.py` and `test_compile_source_line_choices.py`. In the second, 3 412
tokens over several sources. A path name in a command does not prove how much
of that file's content entered the response; such data are kept as
reference points, not as an exact attribution of cost to each module.

Evidence: `logs/modularity-2026-11-02-real-turn-costs.json`,
`logs/modularity-2026-11-02-real-tool-costs.json`. The first contains the SHA-256
of the transcript read. The spending of child agents and separate pipeline calls,
if they are not in these records of the parent session, is not measured here.

## 2. Change history and the choice of boundaries

All available history of the ancestors of `3e1def7e` was analysed, without merge commits,
with changes to `scripts/` or `tests/`: 2 169 commits. Pairs are counted for
the product Python files that exist now; removed paths and history before
a rename were not merged heuristically. There is no cut-off of "last N" or
a date. Commits with mass changes are kept. The additional weight of each pair
in a commit with n sources is `1/(n−1)`: the contribution of one file over all neighbours equals
one. The raw count is also kept; this weight is not used as an automatic
boundary criterion.

| File pair | Together | Changes of the first / second |
|---|---:|---:|
| doctor / markdown_transaction | 32 | 200 / 140 |
| doctor / scheduled_nightly | 26 | 200 / 73 |
| compile_memory / memory_queue | 25 | 190 / 191 |
| retrieval / search_memory | 25 | 80 / 127 |
| markdown_transaction / memory_queue | 23 | 140 / 191 |
| compile_memory / contradiction_pipeline | 19 | 190 / 24 |

Joint changes of compilation and the contradiction pipeline are frequent from the side of
the latter (19 of 24). So it must not be declared a fully independent
service by the file name alone. At the same time, the queue and transactions changed separately in most
commits: merging them into one large component is not justified by
history. Shared publication invariants are kept regardless of location.

For doctor/nightly, part of the shared commits is directly about the report format, freshness
and completion: for example `fc6d554d`, `ea3aea69`, `8fac61e3`. This supports
extracting a shared contract of maintenance results, but not merging diagnostics
with the maintenance executor.

In the initial map of 33 areas, 759 of 902 explicit import links cross
area boundaries. These are not 759 errors: shared base functions are expected to have many
consumers. However, a map of names does not by itself reduce coupling.
The strongly connected group of 91 files stays a signal, not a diagnosis of all files.

Evidence: `logs/modularity-2026-11-02-cochange.json`, `*-imports.json`.
The current built-in index of the working branch was rechecked: `stale=false`, no changes
in the covered roots. The previously described limits of the static graph remain.

## 3. All test files are counted; ownership and dependencies are separated

For all 1 203 tracked files of `tests/`, the path, SHA-256 and tokens are saved:
the sum is 2 440 164, exactly the source measurement. The new map accounts for explicit
imports, string references to sources, imports from other test files and
benchmark modules. A small remainder was assigned by docstring and the subject of the checks
to install, quality or test harness; the basis is marked in each record.

426 files got one area or a data/infrastructure role, 777 have
several areas. This gave 371 distinct combinations of dependencies.
**This is not a proposal to create 371 modules or scenarios.** The map shows
the check context and the ownership candidate; it does not prove the final semantic
belonging of each check. All files are counted; the ambiguity did not disappear
by assigning a label. Imports of helper functions may widen the context without
a real dependency of the tested behaviour on all neighbouring components.

A concrete correction follows from this: test data and reusable fixtures
must be extracted by the contract owner. A test of one module must not import
a whole neighbouring test file for one builder. But shared fixtures are not free:
their needed text enters the package of each consumer. Integration checks
stay integration checks; they must not be weakened to mocks to shrink the package.

Example: the source 14 test files that refer directly to receipt definitions
take 62 375 tokens. Another 14 helper/bootstrap files take 39 069 tokens.
This is a conservative measurement of whole files, not proof that each of their lines
must be read. Together with code/schemas/instructions this gives 121 942 tokens,
**still without fixtures, documentation and serialization**. Such an assembly must not be accepted.

The large fixture `compile-receipt-v3-foreign-body.json` belongs to the scenario
doctor/transaction/receipt authority; it contains 22 base64 images of files, not
one long placeholder string. It must not be replaced by a short invented stub.
If the representation has to change, only exact restoration
of each source file, a hash check and preservation of the source refusal scenario are acceptable.
This is separate work, not compression declared here.

Evidence: `logs/modularity-2026-11-02-test-map-v3.json`,
`*-receipt-tests.json`, `*-receipt-packet.json`.

## 4. Comparison of options after measurement

| Option | What is confirmed | Decision |
|---|---|---|
| Only folders by the initial 74 names | Boundaries inside large files and tests are not checked; existing imports remain | Do not accept as an architecture |
| Large merged subsystems | Part of the joint changes is expected; the size already exceeds the limit | Use as navigation areas, not final modules |
| Only extracting the needed symbols into context | Already applied: tool responses are much smaller than the sources | Keep and measure; by itself it does not fix reverse dependencies |
| Step-by-step extraction of contracts with their own tests | There are concrete dependencies that can be removed without changing formats and runtime | The recommended path, starting with a bounded pilot |

Do not add an API and Protocol to each file automatically. If a plain function
and an immutable data structure express the contract, that is enough. Do not introduce
a new bus, a dependency container or dynamic registration to reduce the
visible number of imports.

## 5. Concrete pilot: receipt formats and source identity

**Observed boundary defect:** diagnostics, to check a receipt format,
has to depend on the compile executor. Even with a mechanical move of the
parser, a chain remains through `SourceDescriptor` from compile_cache to
markdown_transaction. The cause is that format definitions are placed with executing
components and used through their internal functions.

This is a proven structural cause of a superfluous dependency. It is not claimed that
it is already proven as the cause of the nightly failure or of growing LLM payment.

Proposed future module: `scripts/llm_wiki/formats/`, one responsibility:
canonical formats, source identity and receipt checking. Internal
files `canonical.py`, `schema.py`, `source.py`, `receipts.py`; this is one budget
module, not four mandatory standalone packages. The public interface
is defined by the operations used, without a new object hierarchy.

| Source location | Concrete content | Measured text |
|---|---|---:|
| compile_memory.py | v2/v3/v4 parsers, dispatcher, source fields, source/operation identity and the closure of helper functions | 3 751 tokens |
| compile_cache.py | SourceDescriptor, SourceOccurrenceBounds and their local validators/constants | 540 |
| reliable_memory.py | canonical JSON, SHA-256, validate_schema_object, SchemaValidationError and the closure of schema helpers | 2 839 |
| scripts/schemas | existing receipt schemas v2/v3/v4 | 3 553 |
| Mandatory instructions | one copy of AGENTS and the nine laws | 9 815 |

The known part before tests and new documentation totals 20 498 tokens.
This is not the final cost of the pilot. The slices are AST closures of the source definitions,
without a new implementation; exact names, lines and sizes are recorded in
`*-receipt-slice.json` and `*-format-primitives.json`. Private imports and decorators
are checked separately; a conservative AST slice does not replace checking the executed path.

Public operations: `parse_compile_receipt_v2`, `parse_compile_receipt_v3`,
`parse_compile_receipt_v4`, `parse_compile_receipt_version`, extracting the declared
source identity, building the source identity and the compile operation identity.
`UnsupportedCompileReceiptVersion` stays a separate refusal type. A format
check does not confirm that an operation was really committed: that check
stays with the transaction/doctor reader. Do not raise historical v2 receipts
to the authority of modern receipts.

Formats do not import compile_memory, compile_cache, queue, doctor, transaction
or the model client. Schemas stay at the supported paths and are passed/read as
immutable resources; module import does not open runtime databases and does not start
a model. The generic `validate_schema(path)` stays an IO wrapper over the shared validator;
do not write a second validator with different rules. Exceptions have one
identity under old and new imports.

**Consumers that are checked together:** compile_memory, compile_cache,
doctor, evidence_resolver, archive_daily and their test builders; other
uses of canonical/schema helpers are found by a full search and the graph.
Old modules may temporarily re-export the same object for step-by-step
migration of consumers, but contain no second implementation. The places of such exports
are listed and removed after the last consumer is migrated. This is not a reason
to keep the old path forever.

## 6. Gates of the pilot and of law 7

Before the change: add a boundary check that detects the current imports
of formats through the compiler/cache. Show its failure on the source version. Then,
after extraction, show it passing without ignore rules. One green import test
is not enough: a load check in a new process is needed, with forbidden imports
of executing components and no runtime writes.

Properties checked:

- Existing v2/v3/v4 samples give the same records and refusals; canonical bytes,
  source hashes and operation IDs do not change.
- Corrupt JSON, invalid UTF-8, an unknown version, a different body,
  a wrong path/digest, missing dispositions, a mismatching manifest,
  duplicate/gapped/overlapping source ranges keep being rejected.
- "The format is valid" never becomes "the operation is published".
- A parser call gets no access to the queue, the writer or the model.
- Tests of the isolated format use byte samples and small builders
  of its contract. Existing integration scenarios are kept whole and have
  their own counted package; they must not be quietly removed from qualification.
- Packages, including shared helpers, instructions and data, are really ≤100,000.
  The assembly known today, 121 942+, does not pass; until it is correctly split,
  the gate stays red. There is no permission to cut context for a green result.
- CCN of each moved/changed function is ≤5 and the form limits are met.
  A green result of the existing tests does not prove this item after the change.

If meeting these requirements would mean extending the pilot to runtime,
a DB migration or changing receipt semantics, the move stops and the draft
is revised. Do not replace the cause with an additional layer of adapters.

## 7. Check of the source version performed

On the unchanged working branch `3e1def7e`, Python 3.12.3, pytest was run for:

```text
tests/test_compile_transactions.py
tests/test_receipt_versions_preserve_historical_archive_authority.py
tests/test_secondary_receipt_consumers_keep_authority.py
tests/test_doctor_current_v4_exact_historical_work.py
tests/test_doctor_exact_legacy_source_outcome.py
tests/test_compile_hardening.py
```

Result: **224 passed in 57.69s**. Log:
`logs/modularity-2026-11-02-receipt-baseline.txt`. The existing
test isolation outside the vault was used; CI and workaround variables were not set. This is a baseline
of the related contracts, not a test of the new architecture, not the full suite, not Windows
and not a confirmation of installation. After implementation, a comparison with the baseline,
new boundary tests, the full set of related scenarios and the platform matrix are required.

## 8. Criteria of laws 4, 6 and 7

**4.** For the same task, compare correctness, all tokens with separate
accounting of cache, repeated requests, time and intervention. An old
reading of the whole file must not be compared with a new reading of a symbol if the source agent already read symbols.
Two pairs are needed: the current context selection against an improved selection without a move;
then the best source option against the extracted module. The model, tools,
test quality and available sources are the same. Keep the result with lower
complexity and justified full costs; if the gain is not confirmed, do not
claim it. Boundaries may be useful for maintenance even without API savings,
but that is a separate justification.

**6.** No wrapping of the old compiler inside the new parser, no second implementation
of the validator, no registry instead of visible imports, no hidden fallbacks to old code
and no excluded tests. The minimal needed shared logic has one owner.

**7.** The cause is stated as a concrete dependency of the format on the executor;
the check reproduces it on the baseline and forbids its return. Runtime errors
of nightly/capture are not declared fixed by this reorganization. Their causes and
protection need their own investigation.

## Sources and reproducibility

Rechecked on 2026-11-02:

- [Fowler, Bounded Context](https://martinfowler.com/bliki/BoundedContext.html):
  explicit relations of domain models, not a universal number of packages.
- [Import Linter](https://import-linter.readthedocs.io/en/stable/contract_types/):
  protected/forbidden/layers/independence are import checks, not a certificate
  of runtime isolation. The candidate version stays 2.15 from the first research;
  the dependency/lockfile was not changed.
- [PEP 544](https://peps.python.org/pep-0544/): structural interfaces when they
  are needed; for a pure parser, functions and stable result types are enough.
- [CodeScene, coupling health](https://helpcenter.codescene.com/help/articles/1844075-how-to-assess-coupling-health-in-codescene):
  joint changes complement imports; coupling by itself is not a defect.
  The comparison method was used; the CodeScene product was not installed.

The Git sample is reproduced with the command `git log --no-merges
--format='COMMIT %H %s' --name-only -- scripts tests` from the working branch.
The token counting method and the base hashes are saved in the first research.
New local results are named `logs/modularity-2026-11-02-*`;
the hash manifest is `logs/modularity-2026-11-02-validation-manifest.json`.
The private transcript and operational data are not published.
