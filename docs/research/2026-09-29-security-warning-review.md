# Security warning review

Current status, 2026-09-30: the qualified source changes described here are installed. See [the installation evidence](2026-09-30-durable-capture-installation.md) for the final regression, live capture and nightly results. The checkpoints below retain their original dates and describe the state at that checkpoint; their pending-installation statements are historical. Installation does not close the remaining warning review, every native-event qualification, or the wider limit audit.

Review date: 2026-09-29. This is an incomplete audit ledger, not a security
clearance or a list of confirmed vulnerabilities.

## Checkpoint recovery validation, 2026-09-30

Review of the eight `project_journal.py` assertion sites found a real validation
gap; those sites are not collectively cleared. `committed_events` decoded stored
JSON without validating it, and `_project_reserved` did the same on replay.
The renderer was then called with `_validated=True`. A valid database row could
therefore hold an event naming another project. A regression proved that rebuild
accepted that event; schema/type and event-sequence mismatches were also tested.
Six pre-change checks failed. Some malformed inputs already failed later, but
that late exception was not the required validation boundary.

The isolated candidate now reuses `_validated_journal_event` for stored checkpoint
payloads. `_validated_checkpoint_record` additionally binds the event sequence
to the actual database row. Both committed reads and pending replay use it before
writing Markdown. Existing schema, canonical JSON and project-name validation
are shared with journal reads. No new schema, path, dependency, cap or assertion
suppression was added. The previous unvalidated decoding was replaced directly.
A structurally valid event altered consistently with all bindings is outside
this correction: it adds no cryptographic authenticity guarantee and does not
resolve the broader recovery authority/migration audit.

Sources checked on 2026-09-30:

- [OWASP Input Validation](https://cheatsheetseries.owasp.org/cheatsheets/Input_Validation_Cheat_Sheet.html)
  distinguishes syntactic checks from semantic binding and recommends validating
  input before downstream processing.
- [MITRE CWE-20](https://cwe.mitre.org/data/definitions/20.html)
  describes the consequences of processing data without required validation.
- [SQLite integrity_check](https://www.sqlite.org/pragma.html#pragma_integrity_check)
  documents database structural/constraint checks. Application-level agreement
  between JSON fields and their enclosing row still requires application checks.

Alternatives: replacing only the assertions would leave project/sequence binding
unchecked; duplicating the event schema would create divergent validators;
changing event storage is unnecessary for this defect. Reusing the existing
validator adds schema checking when checkpoint rows are read. That cost is
accepted for correctness; no speedup is claimed. Python 3.12.3 and SQLite 3.45.1
were used for verification; the correction uses APIs already supported by the
project's Python 3.10 contract.

The native graph was fresh at `generation-18da1066738743fe-1f2eb295`; incomplete
caller coverage was supplemented with the actual reserve, replay, render and
rebuild paths. The first related regression/complexity batch passed 119 tests in
51.67 seconds. Nine corruption scenarios then passed in 1.92 seconds, including
preservation of existing Markdown and refusal to create files from pending bad
records. Ruff passed. Optimized-interpreter, final complexity and index results
are retained under `logs/audit-2026-09-30-checkpoint-validation-*`.

This candidate is not installed. Private progress/log publication remains
blocked on ownership-safe production mutation. The latest preceding Bandit
snapshot has 346 warnings and no parse errors (`audit-2026-09-30-maintenance-errors-bandit-current.json`);
the 350-warning checkpoint below is historical. A changed warning count is not
a measure of security clearance.

## Follow-up source review, 2026-09-30

The isolated candidate scan has 350 findings and no parse errors (snapshot:
`audit-2026-09-30-lsp-fixture-bandit-current.json`). The counts in earlier
paragraphs below are historical checkpoints. Eight additional B101 sites have
been reviewed; together with the earlier nine, this gives 17 specific assertion
dispositions out of 115. The other 98 assertion findings remain open.

| Additional site | Boundary independent of assert |
| --- | --- |
| `compile_cache._validate_normalized_plan` | `_validate_schema_value` checks the complete plan against the committed schema before the operations-list assertion. |
| `compile_cache._validate_operation_path` (two assertions) | Its only caller iterates operations after that schema check; dictionary and path-string types were already validated. Explicit path normalization and duplicate detection remain active. |
| `compile_cache._checked_schema_properties` | The asserted dictionary belongs to the repository's committed JSON schema, not to the caller's plan. Value type and unknown fields are checked explicitly. |
| `compile_cache._require_schema_fields` | The asserted list is the committed schema's required-field list. Missing plan fields cause an explicit ValueError. |
| `reliable_memory._validate_object`, `_validate_array`, `_validate_string` | `_expected_types` rejects mismatches before dispatch; each `_TYPE_VALIDATORS` selector independently checks the corresponding Python type. The validators explicitly check fields, bounds and content. |

The native graph was queried from fresh generation
`generation-18da0df77d732f30-d7dce7c1`; incomplete coverage was supplemented with
the actual validators and their call sites. The schema/cache tests also ran
under `PYTHONOPTIMIZE=1`: 105 passed in 1.27 seconds. Pytest emitted its expected
warning that ordinary non-test assertions are disabled; that was the condition
being checked, not a suppressed warning. This is additional qualification and
does not replace normal regression or complexity checks. No assertion,
validation, test expectation or analyzer setting was changed for this review.

Seventeen B110 sites were also traced to their callers. This does not clear
them collectively: optional telemetry, cleanup, and maintenance failures have
different consequences. In particular the silent nightly-claim/recovery paths
and generation-handle cleanup still need fault-path qualification. The review
does not establish successful recovery merely because an exception was caught.
Evidence prefixes: `logs/audit-2026-09-30-schema-assert-review-` and
`logs/audit-2026-09-30-exception-review-`.

## Earlier checkpoints, 2026-09-29

Latest assertion review: nine B101 sites below are internal narrowing/invariant
assertions supported by independent checks or construction, rather than the sole
validation of external input. This is a source/data-flow disposition, not a
blanket clearance for assertions or a full optimized-interpreter test run.
The scan remains 351 warnings; 249 remain open after these nine dispositions.

Latest SQL review: all 80 current B608 findings now have specific dispositions.
Of the final 60 reviewed sites, two population probes had a confirmed identifier
encoding defect; their correction also covers the related wrong-object drop and
schema-column paths. The remaining 58 construct fixed syntax or generated
placeholders and bind caller values separately. The full scan still has 351
findings, zero parse errors; 258 warnings in other categories remain open. This
does not certify SQL authorization, availability, limits or every query outside
the analyzer's findings. Details and evidence follow below.

The subsequent bootstrap diagnostic correction removes one B110 warning: the
full scan is now 351 warnings, zero parse errors, with 318 warnings still open.
`2026-09-29-bootstrap-failure-keeps-its-cause.md` records the failing real-child
probe and successful corrected checks. The separate sink-redaction correction
(`2026-09-29-hook-errors-protect-their-text.md`) has regressions for all three
affected writers but does not change Bandit's warning count.

Latest follow-up: the JUnit parser correction removes B314; the full scan now
reports 352 warnings and zero parse errors. B405 remains visible for the import
used only in Element annotations and ParseError handling; actual parsing uses
defusedxml with DTD, entities and external references forbidden. This additional
source-specific disposition leaves 319 warnings open (321 minus the removed
parser finding and the reviewed import). See
`2026-09-29-junit-refuses-dtd-and-entities.md` for reproduction, research, regressions,
dependency validation and limits. No rule or source exclusion was added.

Follow-up after the nightly-trigger correction: the same Bandit invocation
reports 353 warnings and no parse errors. B110 decreased from 20 to 19;
the other rule counts are unchanged. The removed warning was the nightly
catch-up exception that now reaches the redacted, single-line hook log; its
actual regression evidence is recorded in
`2026-09-29-nightly-trigger-errors-remain-visible.md`. Together with the twelve
specific dispositions initially recorded below, that left 341 warnings unreviewed.
The subsequent twenty SQL dispositions reduce the unreviewed remainder to 321.
It does not clear the remaining exception handlers or turn those warnings into
confirmed vulnerabilities. The original snapshot below remains historical
evidence. Follow-up scan:
`logs/audit-2026-09-29-completed-repair-security-bandit-after-trigger.json`.

Bandit 1.9.4, running on its isolated Python 3.14.7 tool environment, scanned
`scripts/` and `benchmark/` again. The product/test interpreter remains Python
3.12.3. The scan covered 231 files and 184,860 lines, with no parsing errors:
354 findings, 268 low and 86 medium, none high. The earlier snapshot covered
225 files and reported 361 findings. Differences in count are not a measure of
fixed defects: in particular, the shared HTTP opener no longer matches the
analyzer's `urlopen` pattern. Its redirect/proxy correction has separate actual
network regression evidence in `2026-09-29-provider-transport-keeps-the-approved-destination.md`.
No Bandit rule, source inclusion, or `nosec` suppression was changed.

## Reviewed findings

| Reviewed B101 site | Independent boundary checked in source |
| --- | --- |
| `lsp_identity.validate_install_manifest` | `_require_manifest_shape` calls `_manifest_shape_ok`, which explicitly checks dictionary type, exact keys, string values and digest shape and raises ManifestError before the assertion. Field/pin checks also raise independently. |
| `lsp_process._start_heartbeat_worker` | Under the lifecycle lock, a missing heartbeat is assigned a newly constructed Thread; construction failure propagates. The non-None assertion narrows the retained local reference, not a caller-controlled object. |
| `lsp_protocol._raise_terminal_outcome` | `_become_fatal` constructs the protocol exception and propagates it through `_terminate_pending_locked` and `_mark_terminal` before completing pending requests. The fatal path still raises its exception when assertions are disabled. |
| `private_vault_backup._output_readers` | The sole production caller gets its process from `_start_command`, which explicitly sets both stdout and stderr to PIPE. Process creation failure raises BackupError. |
| `private_vault_backup._Entry.manifest` and `_copy_entry` | Production symlink entries are created by `_stable_symlink_entry` with a string from os.readlink after file-identity verification. `_leaf_entry` explicitly rejects unsupported file types. The assertions do not authorize source paths or replace those checks. |
| `repository_scope._read_probe_output` | `_git_output` gets its process from `_git_probe_process`, which always sets stdout=PIPE. Bounds and process-outcome checks are separate explicit code. |
| `workspace_revision._GitRun._read_output` | The production constructor call receives the Popen created with `_git_popen_options`, whose stdout is PIPE. Reader exceptions are retained and re-raised by the owner. |
| `workspace_revision._stage_prepared_file` | Its caller `_prepare_file` returns before staging if prepared_files is None. The Unicode-collision rejection uses an explicit ValueError, not the assertion. |

These nine functions and their connected graph, plus source hashes, are retained
under `logs/audit-2026-09-29-completed-repair-assert-review-`. No product code,
assertion, analyzer setting or test was changed to clear them. They do not clear
the other 106 B101 findings, subprocess trust, backup recovery or LSP correctness
as a whole.

Final SQL group, reviewed against source and connected calls on 2026-09-29:

| File / current B608 sites | Specific boundary and disposition |
| --- | --- |
| `markdown_transaction.py`, 14 | Five module expressions compose literal retention predicates and `?` placeholders. `_any_live_expiry` and `_v2_table_populated` receive the literal v2 ownership table tuple. `_coordinator_v2_transaction_rows` selects only `_COORDINATOR_V2_COLUMNS`, substituting literal NULL aliases for absent fields. The migration `completed` closure receives fixed table/column/key definitions and binds key values. Row counts, reconciliation counts, empty-table checks and new-field checks take source-defined table/field sets, not manifest-provided identifiers. `_table_has_rows` was the exception: names came from sqlite_schema and were not escaped; now corrected and regression-tested. |
| `memory_queue.py`, 24 | `_table_is_populated` had the same confirmed defect, now corrected. `_queue_v3_row_matches` receives literal task/history tables and schema constants from its two migration builders, binding the row key. `_still_held` has only the literal source-fence/queue-owner call sites and fixed predicates. Reconciliation and row counts use the fixed migrated/v3-only table sets. Histories, source references, selection/purge authorizations, capture handler versions and task lists generate only `?` placeholders; values are passed separately. The delete helper's placeholder string is generated by its sole caller. Age filtering adds a fixed `created_at >= ?` clause. v2/v3 claimability predicates are source literals with bound time/attempt values. Owned-evidence deletion iterates a literal table tuple. |
| `evidence_graph.py`, 20 | Validation row counts run only after the exact closed schema signature check. Query builders use `_kind_clause`, `_metadata_clause`, `_metadata_values_clause`, `_in_clause`, `_edges_filter`, `_edge_filter`, `_path_prefix_clause`, `_name_prefix_exclusions` and `_reason_filter`: caller values enter parameter lists; metadata keys/column names enter from fixed callers. Traversal direction selects one of two literal column pairs. Evidence selectors choose one of two literal identifier columns and bind the selected value. Search degree/rank/location and unresolved-call fragments are source constants; patterns, names, node IDs and limits are bound. No raw caller text becomes an SQL identifier. |
| `benchmark/run_retrieval_v2.py`, 2 | FTS table names/tokenizer specifications come from the closed source-defined `LEXICAL_CONFIGURATIONS`; the requested configuration is checked before use. `_build` binds candidate contents. `_bm25_rows` generates placeholders and binds the FTS query, eligible IDs and limit. This is not permission to interpolate a future externally supplied tokenizer definition. |

The reviewed source snapshots, connected graph and per-site dispositions are
preserved under `logs/audit-2026-09-29-completed-repair-remaining-sql-`.
Existing graph and retrieval benchmark suites passed 205 tests in 20.07 seconds.
The identifier correction has separate failing reproductions and a corrected
183-pass, five-skip migration/complexity run in
`2026-09-29-schema-names-remain-identifiers.md`. Those skips remain unqualified.
No new injection test is claimed for every source-reviewed constant expression.

| Rule and locations | Finding-specific conclusion and evidence |
| --- | --- |
| B105, four expressions in `benchmark/run_comparative.py` | False positive for hardcoded credentials. The expressions name the token-cost ratio metric, its confidence-bound field, the comparison expression and a missing-result value. They are benchmark report schema/threshold data, not authentication values. This does not justify the numeric benchmark thresholds under law 9. |
| B105, `scripts/onnx_encoder.py::PAD_TOKEN` | False positive for a password. `<pad>` is passed to `tokenizer.token_to_id` to identify padding. It is not an authentication token. |
| B311, `benchmark/longmemeval_data.py::stratified_sample` | Seeded pseudorandomness chooses reproducible benchmark rows within strata. There is no security identifier or credential generated at this call. Cryptographic randomness would break the reproducibility requirement. |
| B311, `benchmark/run_durability.py::assigned_specs` | Seeded shuffling orders predetermined failure-injection trials. The random result is not an ownership token or security decision. |
| B615, two calls in `scripts/reranker.py::_loaded_bundle` | The revision is passed through `**common`, together with `local_files_only=True` and `trust_remote_code=False`. The normal entry obtains the default pinned commit or validates an explicit 40-hex revision in `configured_reranker_identity` / `_explicit_identity`. The analyzer does not follow that dictionary. This resolves the alleged missing Hub revision for this call path, not all model-loading risks. |
| B615, three calls in `benchmark/run_retrieval_v2.py` | `_transformers_model` and `_load_transformer_reranker` pass the selected revision through `**common`; `load_model_selection` validates every candidate through `_require_pinned_local_code` before selecting it. Remote-code loading is refused. Prefetch and offline execution remain distinct existing modes. The finding that these calls omit the revision is not supported by the data flow. |

| B608, `scripts/blackboard.py::_busy_claim_rows` | The interpolated text consists only of generated `?` placeholders. Project and resource values are passed separately to `execute`. Literal-value regression checks both inputs, unrelated-row exclusion and table preservation. |
| B608, two expressions in `scripts/code_hints.py::_lookup_rows` | `_LOOKUP_WHERE` is a source constant containing placeholders. Symbol, suffix and limit use bound parameters. Literal-value regression exercises the actual count and row queries. |
| B608, `scripts/fact_keys.py::_span_rows` | The dynamic fragment contains only comma-separated `?` placeholders, generated from batch length; span values are separate parameters. Literal-value regression checks matching and unrelated spans. |
| B608, `scripts/ledger.py::_SELECT` | Table `ledger` and every selected column come from source-defined constants. No caller-controlled identifier enters this statement. This is a source/data-flow disposition, not an injection test of all ledger operations. |
| B608, `scripts/operational_ownership.py::_OWNER_PROJECTION_DELETES` | Every table and column comes from the literal `_OWNER_PROJECTIONS` tuple; owner identities use placeholders. This clears the flagged statement construction, not all ownership invariants. |
| B608, `scripts/retrieval_telemetry.py::read_events` | `_present_filters` supplies only the two fixed column names; filter values and the limit are bound parameters. The public write/read regression verifies that SQL-looking candidate text selects only its own event. |
| B608, `scripts/trace_ingest.py::_caller_totals` | Only generated `?` placeholders enter the SQL text. Target IDs are passed as the execute parameter sequence. This disposition follows source data flow; the new literal-value tests do not exercise trace ingestion. |

| B608, `scripts/doctor.py::_count_observations` | The only dynamic fragment is a sequence of `?` placeholders. Reasons and the row limit are separate parameters. |
| B608, `scripts/evidence_graph_builder.py::_load_collection_records` | `_RECORD_KEYS[collection]` first requires a source-defined collection key; the corresponding table spelling is derived from that accepted key. Unsupported keys fail before SQL execution. |
| B608, `scripts/generation_catalog.py::_require_capacity` | All three production calls pass literal table names: `generations` or `activation_history`. The limit is bound. No externally supplied table name reaches these call sites. |
| B608, four expressions in `scripts/installed_memory_repair.py` | `_first_held_table` and `_live_owner` receive the literal `_STRAY_SPECS`; `_holds_a_row` callers use literal names or the source-defined queue/coordinator table maps. `_recorded_transactions` generates only `?` placeholders and binds IDs. |
| B608, `scripts/retrieval.py::_neighbour_rows` | Direction selects one of two fixed column pairs; edge types become bound values behind generated placeholders. The seed path is bound too. This does not validate query limits or graph semantics. |
| B608, four expressions in `scripts/search_memory.py` | Generation insertion interpolates a fixed-length placeholder list. The three search statements use `_GENERATION_CHUNK_COLUMNS` and clauses produced by `_generation_filters`; its validity values, query and project are bound. `current_status_sql()` is called with its fixed default column and source-defined status set. No user SQL fragment enters these production callers. |

These further twelve dispositions are source/data-flow reviews. They were not
covered by the twelve new literal-value cases and must not be represented as
such. Their graph evidence is retained as
`logs/audit-2026-09-29-completed-repair-sql-second-review-graph.json`.

Thirty-two current warnings have the above specific dispositions. The remaining 321
warnings still require review; they are not 321 established bugs. The other 60
SQL warnings, subprocess construction, assertion use, swallowed exceptions,
XML parsing and executable resolution have not been cleared by this ledger.

Within that open remainder, all 52 B404 locations were mapped to actual
`import subprocess` AST nodes in the current hashed source files. They are
informational import markers, not 52 demonstrated command-execution flaws.
[Bandit's B404 definition](https://bandit.readthedocs.io/en/latest/blacklists/blacklist_imports.html)
confirms this scope (checked 2026-09-29). Their downstream argument, executable,
environment and shell-boundary review is still open; the 52 remain in the open
count rather than being declared secure just because the imports are ordinary.
The per-file map is
`logs/audit-2026-09-29-completed-repair-security-subprocess-import-review.json`.

SQL review used the connected Codebase Memory graph and the concrete producers
of each interpolated fragment. No production SQL or analyzer suppression changed.
The existing blackboard, code-hint, ledger, ownership, telemetry and trace tests
passed 133 checks in 21.58 seconds. Six Python 3.12 warnings about forking a
multithreaded test process remain visible; these checks do not resolve that
separate compatibility concern. Twelve new actual-SQLite checks passed in
0.23 seconds with SQL-looking literal values, rather than mocked execution.
The first Ruff check found an import-order error in the new test; it was corrected.
The final run of these literal-value tests together with the actual Lizard/AST
complexity gate passed 23 checks in 27.62 seconds; corrected Ruff passed. All
1102 source files in the isolated test copy match the working tree.
Private progress/log updates remain deferred during the active compile snapshot.

Follow-up: the six fork warnings above were subsequently addressed in the two
blackboard process tests; see
`2026-09-29-blackboard-tests-start-fresh-processes.md` for the reproduced context,
spawn/barrier change and successful checks. The original warning-bearing run is
retained. This test-harness correction does not change the Bandit disposition
count or certify other process launch paths.

The interpretation of bound parameters agrees with the current
[Python sqlite3 documentation](https://docs.python.org/3/library/sqlite3.html)
and [OWASP SQL injection prevention guidance](https://cheatsheetseries.owasp.org/cheatsheets/SQL_Injection_Prevention_Cheat_Sheet.html),
checked 2026-09-29. It does not establish authorization, resource bounds or
whole-application security. Evidence prefix:
`logs/audit-2026-09-29-completed-repair-sql-review-`.

## Verification and limits

The existing reranker and selected model-matrix loader tests passed 28 tests in
10.98 seconds in the isolated source copy. They check revision propagation,
local-only behavior, remote-code refusal and sparse-asset requirements. All
1099 code files in that copy had already been compared with the working tree;
no code changed during this review. Transformers is installed at 5.13.0.

Three warnings remain visible: deprecated `torch.ao.quantization` in this
project and its library, plus deprecated quantized tensor creation. Those are
compatibility work, not suppressed warnings or evidence of a failed pin check.
No dependency upgrade or alternative quantization implementation was attempted.

Primary references checked today:
[Bandit B615](https://bandit.readthedocs.io/en/latest/plugins/b615_huggingface_unsafe_download.html)
defines the immutable-revision concern;
[Transformers model loading](https://huggingface.co/docs/transformers/main_classes/model#transformers.PreTrainedModel.from_pretrained)
documents revision and local-file arguments. The call-path conclusions above
come from repository code and tests, not from assuming current online docs prove
installed behavior. Pinned selection does not prove a model benign, protect
against a malicious same-user cache editor, or make a local directory immutable.

Evidence: `logs/audit-2026-09-29-completed-repair-security-bandit-current.json`,
`logs/audit-2026-09-29-completed-repair-security-bandit-current.out`, and
`logs/audit-2026-09-29-completed-repair-security-model-pins-check.txt`.
