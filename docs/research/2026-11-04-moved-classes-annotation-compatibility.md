# Moved classes retain annotation ownership

Status: user approved and implemented in candidate 5bc08163 on 2026-11-04; native CI pending. Research date: 2026-11-04.

## Evidence

On candidate 293339ab / Python 3.14.7, fresh-process imports followed by ordinary
`typing.get_type_hints(Class)` fail for 16 of 25 annotated classes whose
`__module__` was set to a historical facade for pickle compatibility. Stringified
annotations are evaluated in that facade's namespace, which may not be loaded
or may no longer import the annotation names. Explicitly supplying the defining
globals succeeds in some existing tests, but that does not cover the standard
no-namespace call. EventEnvelope fails on datetime; other affected names include
Callable, Mapping, ProviderDescriptor, TokenUsage and OwnerRole.

Affected classes:

- `scripts/llm_wiki/sqlite_contracts.py::MigrationStatement`
- `scripts/llm_wiki/model_records.py::ProviderDescriptor`
- `scripts/llm_wiki/model_records.py::LLMResult`
- `scripts/llm_wiki/model_records.py::BackendResponse`
- `scripts/llm_wiki/model_records.py::CodexPlanningBasis`
- `scripts/llm_wiki/ownership_contracts.py::OwnerLease`
- `scripts/llm_wiki/ownership_contracts.py::MarkerIdentity`
- `scripts/llm_wiki/compile_event_records.py::CompileEvent`
- `scripts/llm_wiki/ownership_rows.py::_AcquireRequest`
- `scripts/llm_wiki/markdown_records.py::MarkdownChange`
- `scripts/llm_wiki/markdown_records.py::MarkdownOperation`
- `scripts/llm_wiki/markdown_records.py::CommittedWriteAuthority`
- `scripts/llm_wiki/markdown_records.py::TransactionRecord`
- `scripts/llm_wiki/markdown_records.py::IntentFence`
- `scripts/llm_wiki/queue_task_records.py::_ExportedRows`
- `scripts/llm_wiki/event_envelope_values.py::EventEnvelope`

A diagnostic-only execution of source without `from __future__ import annotations`
resolves 24 of 25 inspected classes. IntentFence also needs its already-declared
OwnerLease value dependency at runtime: today it is TYPE_CHECKING-only. No
production files were changed by this probe. This is not a completed repair.

## Proposed compatibility choice

Use Python 3.14's native deferred annotations in the affected value owners and
make that existing OwnerLease dependency available at runtime. Keep nominal
classes, fields, defaults, behavior, old facade aliases and serialization paths.
Native annotation values and signature rendering differ from the former string
representation; the user explicitly accepted that compatibility clarification on 2026-11-04. Recheck constructor parameters/defaults semantically, historical pickle
roundtrips, isolated imports, no-facade annotation resolution, boundary closure,
actual complexity, measured test weights and every full packet. Do not replace
failures with Any, silence NameError or preload heavy facades.

Alternatives: requiring all callers to provide defining globals shifts the defect
to consumers; changing __module__ loses historical serialization identity;
resolving each class dictionary manually adds mutation logic and can disagree with
dataclass field metadata; fake facade modules create import-order hazards. The
native mechanism fits the already-approved Python 3.14 runtime, with the visible
representation tradeoff stated above. No additional library is proposed.

## Sources

Checked primary sources: [Python 3.14 annotationlib](https://docs.python.org/3.14/library/annotationlib.html),
[Larry Hastings's PEP 649](https://peps.python.org/pep-0649/),
[Jelle Zijlstra's PEP 749](https://peps.python.org/pep-0749/), and independent
[attrs runtime type resolution API](https://www.attrs.org/en/stable/api.html#attrs.resolve_types).
The runtime documentation distinguishes stringified and deferred annotations;
the two PEPs specify namespace capture and dataclass handling. attrs demonstrates
that runtime consumers need actual type resolution and sometimes explicit namespaces;
it is evidence for the compatibility concern, not a new dependency or a proposed
replacement for dataclasses.

Private evidence: logs/moved-class-annotations-reproduction.json,
logs/moved-class-native-annotations-probe.json,
logs/class-annotations-and-ci-graph.json. Fresh graph: generation-18e4b727fda2f155-75b523bd,
2439 sources, candidate293339ab; callers and actual sources/tests checked.


## Candidate qualification (2026-11-04)

Removed stringified annotations in eight owners and imported the existing OwnerLease
at runtime. Production function/class ASTs are unchanged; facade aliases and pickle
identities remain. Immutable fixtures retain original fields, defaults and annotation
labels; additional assertions require actual native types and ordinary get_type_hints.
Cold-process regressions fail on old source (16 failures, 5 controls pass) and pass
after repair without importing historical facades.

194 focused checks, 63 weighed checks and 3700 expanded checks passed (3 skips and
one pre-existing warning in the expanded run). Additional transaction, ownership,
SQLite and event consumer checks: 280 passed, 5 platform skips. The first consumer
command named a nonexistent test file and ran no tests; the corrected selection
produced the result above. All 169 measured functions satisfy CCN/if/depth limits;
Ruff clean. All159 complete packets reconstructed exact paths and bytes, largest
99806tokens. This approval report is currently outside those packets. No live
installation or full-current/native CI success is claimed. Evidence: logs/native-
annotations-{red,consumers,bundles}.txt and approved-contracts-annotations-graph.json.
