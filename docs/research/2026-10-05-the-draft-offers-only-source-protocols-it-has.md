# The draft offers only source protocols it has

Research date: 2026-10-05.

A real Codex draft returned five native-event selectors for a selected legacy
daily source containing no verified native projection. The physical evidence
binder correctly refused them. The request had offered both evidence shapes
and described the native protocol regardless of its selected source material.

The draft request now offers legacy evidence alone when its selected daily
sources contain no verified rendered native projection. A native or mixed
selection retains both existing shapes. Optional page context cannot enable
the native source protocol. The semantic validation schema, physical source
checks, citation binding and publication preconditions remain unchanged.
The prompt explains that ordinary Markdown and tool examples do not establish
a native-event container. This wording is guidance; the local source verifier
remains the enforcement boundary.

Packing, final input fit and dispatch use the same selected request schema.
The additive byte estimate includes the exact schema-envelope difference for
native selections. The draft program becomes v8 so previous draft-cache
identities cannot silently reuse the former presentation. Existing cached
plans remain disposable derived data; there is one active draft implementation.
No persisted source, claim or receipt format changes, and no window, retry,
output budget, provider or runtime path is added or changed.

Blind retries were rejected because they leave the unsupported output shape
available. Reconstructing invented selectors would bypass physical proof.
Prompt wording alone does not constrain the request shape. Disabling native
evidence universally would remove a supported source protocol.

Two regressions fail against the original request. Controls preserve native
input support and the global semantic validation schema. Existing physical
native-container and exact provider-payload measurement tests exercise the
related paths. Real model quality, retries and full-cycle cost need separate
qualification; passing these tests does not close the overall audit.

Primary sources checked on the research date:

- [OpenAI agent safety](https://developers.openai.com/api/docs/guides/agent-builder-safety):
  constrain data flow with structured outputs and evaluate actual traces.
- [Anthropic prompting practices](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices):
  distinguish instructions, source content and examples, and ground conclusions
  in source quotations. This is supporting research, not a provider change.
- [OWASP prompt injection prevention](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html):
  labels do not enforce authority; validate model proposals outside the model.

Evidence: `tests/test_compile_draft_offers_only_present_evidence_styles.py`;
`tests/test_compile_packing_reuses_only_selected_projection.py`;
`tests/test_compile_plans_the_local_provider_layout.py`;
`scripts/compile_memory.py::_draft_schema`.


## Protected source quotations (2026-10-06)

The common DLP boundary can redact a source line before the model reads it.
An exact quotation of that protected line consequently differs from the
original physical bytes. The binder now verifies the protected representation
against the immutable selected source packet and the current authenticated
DLP policy. It maps only a complete, uniquely identified logical line back
to its original physical container and retains the original byte spans and
hashes. The semantic quotation remains redacted. No secret is reconstructed
for a model request.

Native quotations additionally require a rendered, verified native container,
its original selector and its line index. Optional context cannot confer this
authority. Partial protected quotations, ambiguous aliases, unmatched policy
transformations and transformations that prevent physical line alignment fail
closed. Unicode line separators inside a native JSON frame do not split its
physical source line. The normalization identity becomes normalize-v4;
no durable source, receipt, schema or runtime location changes.

Regressions reproduce both original quotation failures and preserve negative
controls. Revalidation of retained model output is artifact evidence, not a
fresh model cycle, a publication or an efficiency claim. Full-cycle model
quality and cost remain separate qualification requirements.

Additional independent primary source checked on 2026-10-06:
[W3C PROV-DM](https://www.w3.org/TR/prov-dm/) describes derivation between
entities; here the transformation and original physical provenance are
verified locally. This source does not establish automatic trust in model
output. OpenAI and OWASP sources above were checked again on the same date.

Evidence: `tests/test_compile_binds_the_protected_source_view.py`;
`scripts/compile_memory.py::_protected_source_rows`;
`scripts/compile_memory.py::_bound_protected_legacy_evidence`.

### 2026-10-06: repeated address metadata must not strand a legal source part

The unchanged long-entry regression exposed a new display cost. Its 83,020-byte
ordinary source already becomes seven legal physical parts. A 16,367-byte part
has 402 eligible addresses. Repeating its eight-character entry timestamp on
all rows makes the address table 6,732 bytes and the actual local planning
estimate 30,344 bytes, above the unchanged available input of 27,744 bytes.
This is display redundancy, not an oversized native atomic unit.

The derived table now groups consecutive rows under `ENTRY <timestamp>` inside
their `FILE` block. Every ordered integer ID and visible LF-line address remains
present. A timestamp appearing again later starts another group; no sorting or
merging of physical source spans occurs. The selected raw source, physical part
boundaries, receipt identities, evidence schema, binding and DLP checks, and
input budget are unchanged. The draft identity becomes compile-draft/v11;
normalization remains normalize-v6 because normalized evidence is unchanged.
The same part has a 3,151-byte table and a 26,761-byte local estimate. This is an
estimated local serialized-input measurement, not a model wire-token guarantee.

Research read on 2026-10-06: Python 3.10's official
[itertools.groupby contract](https://docs.python.org/3.10/library/itertools.html#itertools.groupby)
groups consecutive equal keys without sorting; the W3C's stable 2013
[PROV-DM Recommendation](https://www.w3.org/TR/prov-dm/) separates derived views
from identified source versions; the official
[JSON Schema object reference](https://json-schema.org/understanding-json-schema/reference/object)
clarifies that existing required fields and additional-property restrictions
remain the validation boundary. These sources justify lossless display grouping,
not new source authority or inference quality. Raising the budget, changing
physical partitions, omitting eligible addresses, and caching successful DLP or
binding verdicts were rejected.

The original isolated run retained three failures and three passing controls:
the old long-entry assertion and two lossless display regressions fail on the
old representation. The first measurement operator had an incorrect
ContextBudget constructor and was retained as a failed qualification attempt;
its corrected run uses the real `_compile_budget(None)`. An initial related-test
command named a nonexistent file and collected zero tests; the corrected focused
run passed 74 tests. No model calls or knowledge changes were made. Fewer display
bytes do not establish full-cycle model usefulness, and other genuinely
oversized requests must still be refused by the existing fit checks.

The final identity string is two characters shorter than the interim display
prototype. Thus internal model_start character offsets shift by two, while all
integer IDs, FILE line ordinals, quoted text, timestamps and source paths remain
identical. Matched metadata hashes exclude only model_start; they do not claim
identical whole derived dictionaries. Extending the measurement initially
exceeded CCN 5 and was refactored before execution; attempting to pass a tuple
to the strict canonical-JSON helper failed and was retained before using an
explicit deterministic diagnostic JSON serialization. No production checks were
relaxed. Related final-v11 checks passed 338 tests in 116.85 seconds.

### 2026-10-06: select context once at the point where a batch will execute

The normal run eagerly packed every group with optional context, then called
`_refresh_compile_batch` immediately before executing each group. Refresh takes a
new context/target snapshot and performs the complete ranking and fitting again.
The initial optional selection is therefore unused model-input work. Removing
refresh would instead leave later groups bound to context from before earlier
publications, so refresh remains mandatory.

Public `pack_compile_batches` continues to return ready batches. Its one shared
packing pipeline also serves internal mandatory-only run planning. A provisional
batch retains the exact complete source parts, manifest, selected provider/model
and budget, and sets the transient `context_pending` field. This field defaults
to false for existing callers, is excluded from comparison, and never enters the
explicit persisted packing dictionary. It is misuse prevention, not authority.
Resolution, cache lookup, low-level fit/dispatch and publication reject such a
batch. Normal refresh performs the one full fresh selection, verifies unchanged
manifest/model/candidate ownership/budget, and emits a ready batch with the final
measured packing. Complete context remains available for selection; no offer cap,
source omission, DLP verdict cache, budget change or durable format is added.

Design sources read on 2026-10-06: Python 3.10's official
[dataclass field/replace documentation](https://docs.python.org/3.10/library/dataclasses.html)
explains transient fields and comparison (frozen objects are not a security
boundary); official [SQLite isolation documentation](https://www.sqlite.org/isolation.html)
does not grant filesystem snapshot authority; MITRE's
[CWE-367](https://cwe.mitre.org/data/definitions/367.html)
explains why earlier checks cannot replace checks at use. Existing physical
source, native/tool companion, target, writer and CAS guards remain outside the
provisional state. Alternatives rejected were removing refresh, caching stale
initial optional selections, disabling context, or making public ready packing
implicitly lazy. This is internal ephemeral orchestration, not a new setting,
path, environment variable, authority or receipt contract.

The first original regression run retained six failures and one passing control;
the restored original with expanded guards retained eleven failures and three
passing controls. The original unused-selection test observes a real selection
call, rather than relying only on a missing new field. The candidate's 308 related
tests passed. Existing test files and assertions remain unchanged. Pure paired
measurement uses one complete genuine physical unit and one captured full context
for both processes. Its context callback intentionally holds that snapshot for a
matched packing comparison; this measures packing, not fresh collection or model
quality. Live physical/frame checks are still performed normally. Full-cycle speed,
provider quality and overall audit completion require separate evidence.

The matched sample has 1,734 available context sources. Old initial selection
used 106.886 seconds and old refresh 107.854 seconds; mandatory-only planning
used 0.148 seconds and candidate refresh 106.280 seconds. Observed selection
passes changed from two to one. CPU totals were 210.179 and 105.363 seconds;
peak RSS was 89,604 and 89,692 KiB respectively. Every final prompt/schema,
protected Codex-local stdin, physical descriptor, packing field, selected source
and captured target matched exactly; the final local stdin was 27,678 bytes.
This is a shared-host, one-unit observation with retained context, not a measured
32-unit improvement, full-wire token guarantee or useful-model cycle. It also
shows that the remaining necessary final selection is still expensive.
The measurement operator initially exceeded CCN 5 and was refactored before
execution. No model/bootstrap/protocol calls, SQL changes or Root edits occurred.


## 2026-10-06 — Requalify the same provider after local environment changes

The real local reranker-load diagnostic took 15.774 seconds, with peak RSS
2,341,972 KiB and zero model turns. The existing environment digest guard passed
before loading and refused afterward. Only the observed names are retained here:
KMP_DUPLICATE_LIB_OK, KMP_INIT_AT_FORK and TORCHINDUCTOR_CACHE_DIR. A fresh native
qualification passed afterward with the same client and reranker source bytes.
This proves a possible shared cause; the earlier production error recorded only
RuntimeError, so its exact message is not retrospectively established.

Before a refreshed batch, compare its retained basis with the current provider
environment. If changed, perform normal qualification under the original caller
deadline and require the same provider, model, inference settings, fallback and
backend identity. Unknown candidates keep their existing behavior. The original
strict dispatch guard remains. Ignoring selected environment names, freezing old
environment values and preloading the model were rejected because they obscure
the actual executable environment or move the cost without proving equivalence.

Primary sources read on 2026-10-06:

- [Python 3.10 os.environ](https://docs.python.org/3.10/library/os.html#os.environ):
  process environment is observable and mutable through the mapping.
- [Linux execve](https://man7.org/linux/man-pages/man2/execve.2.html): the invoked
  program receives its supplied environment.
- [SQLite isolation](https://www.sqlite.org/isolation.html): database snapshots
  do not authenticate a later provider environment or filesystem state.

The isolated provider candidate passed 38 related tests. Its native observation
was initialization and local loading, not inference or a successful compile.

## 2026-10-06 — Combined qualification scope

The address display, environment requalification and single final context
selection share one compiler. Refresh verifies the newly qualified candidate
tuple, rather than demanding the stale tuple object after legitimate environment
change. Unchanged candidate identities retain their original tuple. Model and
budget guards, mandatory source identities and readiness checks remain. No
provider choice, budget, persistent format or source partition changes.

## 2026-10-06: one protected base per draft layout

The observed refresh spends 2642 of 3967 nonempty sampling observations in
`_redact_patterns` and `_redact_named_values`. Each optional-context offer builds
one complete layout. Its exact base is protected once for choice binding and
again for display; the appended prompt receives a third complete scan. These
are repeated CPU work, not model wait. Sampling counts are not invocation counts
or an isolated CPU benchmark.

The candidate retains one protected base only for the lifetime of one
`_draft_layout`, with an exact full-text comparison and a fresh policy-value
comparison before reuse. Exceptions reset that scope. Standalone choice/prompt
calls keep their existing fresh scans. The complete appended prompt and final
dispatch still receive fresh full scans. No successful alias, physical-source,
filesystem, native companion, or CAS verdict is cached across contexts/batches.

The original production layout scans the same exact base twice: the regression
fails with actual count 2 where 1 is required. Drift and unavailable policy,
redaction of appended addresses, and a secret assembled across the base/address
boundary remain refusals. Existing source choices, physical evidence bindings,
whole-source transport, and no-choice behavior retain their contracts.

Primary research checked 2026-10-06:

- [Python 3.10 regular expressions](https://docs.python.org/3.10/library/re.html):
  substitutions depend on the complete string and matching flags. Fragment scans
  cannot be assumed equivalent to the current whole-prompt pipeline.
- [Git racy-file identity](https://git-scm.com/docs/racy-git): metadata alone is
  insufficient identity. The candidate compares exact retained text and freshly
  loaded policy values, not path/stat identity.
- [OWASP prompt-injection prevention](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html):
  model and input data remain untrusted; output/source authorization still needs
  its independent checks.

Fragment redaction and cross-context success caches were rejected: whole-string
allow fingerprints, configured literals, cross-boundary matches, and protected
alias ambiguity make them different security operations. This removes one
redundant base scan, not the required whole appended/dispatch checks. It neither
qualifies the untouched planning constants nor proves full-cycle model
usefulness or closes the remaining problem-day backlog.

The matched no-inference sample reuses one retained genuine complete unit and
all 1734 optional offers in two independent processes. Final prompt, schema,
protected Codex stdin, physical descriptors, selected context, packing metadata
and target snapshots are identical. Baseline selection used 106.060 CPU seconds
and 139.367 wall seconds; the candidate used 64.527 CPU seconds and 72.020 wall
seconds. Peak RSS was 89324 versus 89892 KiB (+568 KiB). The host concurrently ran
normal SourceWork, so wall times are not isolated benchmarks. This is not a
whole-cycle token, publication, semantic-quality, or backlog-completion proof.
The retained original RED and an initially misbound new drift-test fixture remain
recorded; the fixture was corrected to observe both actual loader call sites.

### 2026-10-06 — Preserve claims while canonicalizing receipt evidence

The retained accepted plan failed publication because multiple semantic evidence
items projected to the same receipt record. The current canonical cache entry
SHA-256 is `15e4912ffa91cfdbf266afe47ba843eafa913ce8a25dc9bc6b754a5f75177f73`.
Normal materialization independently reproduced 17 operations, 31 receipt records,
28 distinct records and three duplicate groups. A single captured immutable input
was shared by the original and candidate validators, using exact dataclass-field
conversion between independently loaded compiler modules. All 17 rendered output
hashes and every distinct receipt record matched. The candidate emitted 28 records.
No model, transaction replay or live-vault mutation was performed.

Receipt evidence is a projection, not the ordered semantic assertion list. The
common `_bound_evidence` boundary now validates every binding and required source
part first, then retains one record for each exact canonical JSON representation.
Every record field participates, including any retained legacy extra field. The
first occurrence determines order. Different operation paths, source paths,
source digests and quote digests remain distinct. Semantic evidence, its indices,
claims, rendered text and claim ledger are unchanged. The receipt schemas and
`uniqueItems` validation remain strict. This does not grant new source authority.

Primary sources checked on 2026-10-06:

- [JSON Schema validation, section 6.4.3](https://json-schema.org/draft/2020-12/json-schema-validation):
  `uniqueItems` requires distinct array values. The published draft describes the
  assertion; the product keeps its existing local schema and validator.
- [Python 3.10 dictionary contract](https://docs.python.org/3.10/library/stdtypes.html#mapping-types-dict):
  dictionaries preserve insertion order. Exact canonical record bytes are keys;
  no digest-only identity or partial-field deduplication is introduced.
- [W3C PROV data model](https://www.w3.org/TR/prov-dm/): provenance entities and their
  uses are distinct from semantic statements about those entities. A repeated
  provenance projection does not justify discarding a claim.

Rejected alternatives: dropping duplicate semantic evidence would risk evidence
indices and claims; deduplicating only quote hashes would merge distinct source
or operation provenance; relaxing the receipt schema would conceal malformed
receipts; changing persisted formats is unnecessary. Exact canonical record
keys add temporary storage proportional to validated receipt evidence, without a
new cap or persistent cache.

Regression evidence includes original duplicate-materialization failures and an
original normal temporary-vault v4 publication failure at `$.evidence uniqueItems`.
The corrected publication retains both assertions. Negative controls preserve
source-tamper refusal, distinct record fields, legacy extra fields and strict
schema uniqueness. Initial test/operator mistakes (module import, missing model
argument, schema-object API, and independent-module snapshot class mismatch) are
retained in private logs; they are not product failures or successful proof.
The live SourceWork failure and its 20 model calls remain counted. This candidate
has not rerun that useful model cycle, settled historical SQL records, or closed
point 7. The prior D matched CPU evidence concerns one layout scope only; it is
not a complete-cycle speed claim.
