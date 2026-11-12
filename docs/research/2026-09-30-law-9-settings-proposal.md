# Law 9: explicit operational budgets — proposal

Date: 2026-09-30. Status: **not selected; no settings added**.

The owner asked the agent to choose a solution that satisfies the development
laws rather than seek approval for this table. That instruction does not establish
the listed numbers as owner-selected budgets. The table remains a historical
proposal, not the operating contract. The continuation removes redundant bounds
and measures or establishes the basis of necessary bounds; introducing 81 knobs
alone would not satisfy law 9.

## Proposed contract

Extend the existing `scripts/settings.py` registry and existing optional
`llm-wiki.toml`, without adding a file location, runtime root, service or tool.
Add the exact settings listed below under `[limits]`. Their existing environment
form follows the existing registry rule: `LLM_WIKI_LIMITS_<UPPERCASE_KEY>`.
Allow finite positive fractional values for time budgets; count and byte settings
remain positive integers, and booleans and unknown keys remain errors. Per-setting
validation retains any independent protocol/platform bounds. Caller deadlines
still win over a configured time allowance. DLP, identity, path containment,
canonical bytes, admission fencing and evidence validation are not weakened.

The table specifies existing values for compatibility, **not measured optima**.
Approval would explicitly select them as the owner's initial resource budgets:
how much work, memory, waiting and diagnostic output one operation may spend.
Each declaration will state units, this provenance, the consequence of reaching
it and when to reconsider it. Tunability alone does not prove a numeric basis.
Further measurement can revise these initial budgets; no value is silently raised.
Settings that truncate derived presentation will report truncation. Data-bearing
operations must refuse or resume durably instead of silently dropping content.

This batch excludes semantic relevance weights, secret detection thresholds,
protocol constants and several values that need a separate root-cause repair.
The heuristic inventory is not a count of independent audit findings or proof
that all limits have been classified. Remaining values will be measured, removed
when redundant, or covered by a later explicit proposal if they change contracts.

## Research and choice

Primary sources rechecked 2026-09-30:
[12-factor configuration](https://12factor.net/config),
[uv configuration](https://docs.astral.sh/uv/concepts/configuration-files/),
[Python TOML parser](https://docs.python.org/3/library/tomllib.html).
They support separating deployment-dependent values from implementation,
validated file/environment precedence, typed parsing and operator overrides. The
systemd documentation could not be fetched and is not counted as a checked source. They do not justify
any particular number in this table.

Existing validated registry versus separate per-module readers: select the
registry to avoid divergent parsing and error handling. Environment-only settings
would be difficult to inspect consistently across hooks, MCP and maintenance;
retain the existing persistent TOML and one-run environment override. Removing
all input ceilings would remove resource protection without replacing it, so it
is not the chosen repair. This introduces many advanced settings; normal use
still requires no configuration and listing effective values retains provenance.

## Exact mapping

Historical table update, 2026-10-01: `archive_stale.MAX_ARCHIVE_PAGE_BYTES` was removed; archive/restore reuse the existing transaction target budget. Its proposed setting below was never introduced. See [the installed correction](2026-10-01-archive-pages-use-the-transaction-budget.md).

| Existing declaration | Proposed setting | Initial value to approve |
|---|---|---|
| `access_tracking.MAX_EVENTS_PER_PAGE_EXPORT` | `limits.access_tracking_max_events_per_page_export` | `1_000` |
| `archive_daily.MAX_ARCHIVE_ENTRIES` | `limits.archive_daily_max_archive_entries` | `10_000` |
| `archive_daily.ARCHIVE_WRITER_WAIT_SECONDS` | `limits.archive_daily_archive_writer_wait_seconds` | `0.25` |
| `archive_stale.MAX_ARCHIVE_PAGE_BYTES` | `limits.archive_stale_max_archive_page_bytes` | `16 * 1024 * 1024` |
| `blackboard._MAX_RESOURCE_BYTES` | `limits.blackboard_max_resource_bytes` | `512` |
| `blackboard._MAX_TASK_BYTES` | `limits.blackboard_max_task_bytes` | `4096` |
| `blackboard._MAX_AGENT_BYTES` | `limits.blackboard_max_agent_bytes` | `128` |
| `capture_diagnostics.MAX_FAILURE_LOG_BYTES` | `limits.capture_diagnostics_max_failure_log_bytes` | `256 * 1024` |
| `capture_diagnostics.MAX_REASON_CHARS` | `limits.capture_diagnostics_max_reason_chars` | `200` |
| `claim_tree_manifest.MAX_GUARDRAIL_SOURCE_DIRECTORIES` | `limits.claim_tree_manifest_max_guardrail_source_directories` | `5_000` |
| `code_extractor._MAX_OBSERVATION_TARGET_BYTES` | `limits.code_extractor_max_observation_target_bytes` | `4096` |
| `code_extractor.MAX_BINDINGS` | `limits.code_extractor_max_bindings` | `8` |
| `code_extractor.MAX_BINDING_BYTES` | `limits.code_extractor_max_binding_bytes` | `256` |
| `code_graph.CALL_WALK_MAX_DEPTH` | `limits.code_graph_call_walk_max_depth` | `8` |
| `code_graph.CALL_WALK_MAX_ROWS` | `limits.code_graph_call_walk_max_rows` | `10_000` |
| `code_graph.CALL_WALK_MAX_WORK` | `limits.code_graph_call_walk_max_work` | `100_000` |
| `code_graph.CALL_WALK_MAX_SEEDS` | `limits.code_graph_call_walk_max_seeds` | `20` |
| `code_graph.DEPENDENCY_SEED_LIMIT` | `limits.code_graph_dependency_seed_limit` | `20` |
| `code_graph.DEPENDENCY_MAX_DEPTH` | `limits.code_graph_dependency_max_depth` | `8` |
| `code_graph.DEPENDENCY_MAX_ROWS` | `limits.code_graph_dependency_max_rows` | `1000` |
| `code_graph.DEPENDENCY_MAX_WORK` | `limits.code_graph_dependency_max_work` | `100_000` |
| `code_navigation_renderer._HOVER_BYTE_CEILING` | `limits.code_navigation_renderer_hover_byte_ceiling` | `2048` |
| `code_navigation_renderer._SIGNATURE_BYTE_CEILING` | `limits.code_navigation_renderer_signature_byte_ceiling` | `1024` |
| `codex_memory.MAX_HOOK_CONFIG_BYTES` | `limits.codex_memory_max_hook_config_bytes` | `256 * 1024` |
| `corpus_snapshot.MAX_CORPUS_INSPECTED_ENTRIES` | `limits.corpus_snapshot_max_corpus_inspected_entries` | `50_000` |
| `corpus_snapshot.MAX_CORPUS_HEADINGS` | `limits.corpus_snapshot_max_corpus_headings` | `100_000` |
| `corpus_snapshot.MAX_CORPUS_CHUNKS` | `limits.corpus_snapshot_max_corpus_chunks` | `100_000` |
| `doctor.SUMMARY_LIMIT` | `limits.doctor_summary_limit` | `600` |
| `doctor.MAX_MANIFEST_BYTES` | `limits.doctor_max_manifest_bytes` | `256 * 1024` |
| `doctor.MAX_OPERATIONAL_ROWS` | `limits.doctor_max_operational_rows` | `10_000` |
| `doctor.DEFAULT_TIME_BUDGET_SECONDS` | `limits.doctor_default_time_budget_seconds` | `5.0` |
| `doctor.DEFAULT_GENERATION_TIME_BUDGET_SECONDS` | `limits.doctor_default_generation_time_budget_seconds` | `60.0` |
| `doctor.CODEX_HOOK_PROBE_SECONDS` | `limits.doctor_codex_hook_probe_seconds` | `2.0` |
| `doctor.CODEX_HOOK_PROBE_STARTUP_SECONDS` | `limits.doctor_codex_hook_probe_startup_seconds` | `0.25` |
| `doctor.MAX_CODEX_HOOK_PROBE_BYTES` | `limits.doctor_max_codex_hook_probe_bytes` | `256 * 1024` |
| `doctor.FILESYSTEM_PROBE_SECONDS` | `limits.doctor_filesystem_probe_seconds` | `1.0` |
| `doctor.BACKUP_GIT_TIMEOUT_SECONDS` | `limits.doctor_backup_git_timeout_seconds` | `5` |
| `evidence_resolver.MAX_GROUNDED_SOURCE_BYTES` | `limits.evidence_resolver_max_grounded_source_bytes` | `8 * 1024 * 1024` |
| `evidence_resolver.MAX_ARCHIVE_MANIFEST_BYTES` | `limits.evidence_resolver_max_archive_manifest_bytes` | `1024 * 1024` |
| `evidence_resolver.MAX_TAG_FILE_BYTES` | `limits.evidence_resolver_max_tag_file_bytes` | `1024 * 1024` |
| `generation_catalog.MAX_CATALOG_BYTES` | `limits.generation_catalog_max_catalog_bytes` | `256 * 1024 * 1024` |
| `generation_catalog.MAX_GENERATIONS` | `limits.generation_catalog_max_generations` | `1024` |
| `generation_catalog.MAX_ACTIVATION_HISTORY` | `limits.generation_catalog_max_activation_history` | `16384` |
| `graph_query.MAX_HOPS` | `limits.graph_query_max_hops` | `3` |
| `graph_query.HOP_ROW_CEILING` | `limits.graph_query_hop_row_ceiling` | `200` |
| `graph_query.HOP_WORK_CEILING` | `limits.graph_query_hop_work_ceiling` | `1000` |
| `graph_storable.MAX_IDENTITY_KEY_CHARS` | `limits.graph_storable_max_identity_key_chars` | `4096` |
| `installer_config.MAX_DEBUG_BYTES` | `limits.installer_config_max_debug_bytes` | `4 * 1024 * 1024` |
| `installer_config.DEBUG_TIMEOUT_SECONDS` | `limits.installer_config_debug_timeout_seconds` | `15.0` |
| `installer_config.CLEANUP_SECONDS` | `limits.installer_config_cleanup_seconds` | `2.0` |
| `integration_config_backup.MAX_BACKUPS` | `limits.integration_config_backup_max_backups` | `10` |
| `lsp_identity.MAX_REPOSITORY_CONFIG_BYTES` | `limits.lsp_identity_max_repository_config_bytes` | `256 * 1024` |
| `markdown_transaction._ADOPTION_VALIDATION_SECONDS` | `limits.markdown_transaction_adoption_validation_seconds` | `30.0` |
| `mcp_server.MAX_MCP_EVIDENCE_BYTES` | `limits.mcp_server_max_mcp_evidence_bytes` | `64 * 1024` |
| `mcp_server.MAX_MCP_TOTAL_EVIDENCE_BYTES` | `limits.mcp_server_max_mcp_total_evidence_bytes` | `256 * 1024` |
| `mcp_server.MAX_MCP_QUERY_LENGTH` | `limits.mcp_server_max_mcp_query_length` | `8_192` |
| `mcp_server.MCP_LSP_STARTUP_SECONDS` | `limits.mcp_server_mcp_lsp_startup_seconds` | `60.0` |
| `mcp_server.MAX_NAVIGATION_SOURCE_BYTES` | `limits.mcp_server_max_navigation_source_bytes` | `16 * 1024 * 1024` |
| `mcp_server.MAX_NAVIGATION_SOURCE_CACHE_BYTES` | `limits.mcp_server_max_navigation_source_cache_bytes` | `64 * 1024 * 1024` |
| `mcp_server.MCP_WORKER_SLOTS` | `limits.mcp_server_mcp_worker_slots` | `4` |
| `migrate_to_okf.MAX_MIGRATION_PAGE_BYTES` | `limits.migrate_to_okf_max_migration_page_bytes` | `16 * 1024 * 1024` |
| `path_coverage.NODE_CEILING` | `limits.path_coverage_node_ceiling` | `10_000` |
| `path_coverage.PARSE_ERROR_LIMIT` | `limits.path_coverage_parse_error_limit` | `20` |
| `path_coverage.PARSE_NODE_CEILING` | `limits.path_coverage_parse_node_ceiling` | `200_000` |
| `path_coverage.MAX_PARSE_BYTES` | `limits.path_coverage_max_parse_bytes` | `4 * 1024 * 1024` |
| `pinned_download.NETWORK_TIMEOUT_SECONDS` | `limits.pinned_download_network_timeout_seconds` | `30.0` |
| `private_vault_backup._MAX_ENTRIES` | `limits.private_vault_backup_max_entries` | `1_000_000` |
| `pyright_profile.NODE_PROBE_TIMEOUT_SECONDS` | `limits.pyright_profile_node_probe_timeout_seconds` | `2.0` |
| `pyright_session._OWNER_CLEANUP_SECONDS` | `limits.pyright_session_owner_cleanup_seconds` | `2.0` |
| `repository_retention.RETIRE_BUDGET_SECONDS` | `limits.repository_retention_retire_budget_seconds` | `5 * 60.0` |
| `retire_lsp_evidence.SCAN_BUDGET_SECONDS` | `limits.retire_lsp_evidence_scan_budget_seconds` | `20.0` |
| `retire_own_call_transcripts.MAX_TRANSCRIPTS_PER_PASS` | `limits.retire_own_call_transcripts_max_transcripts_per_pass` | `2000` |
| `retrieval.MAX_OPTIONAL_STRAGGLERS` | `limits.retrieval_max_optional_stragglers` | `2` |
| `search_memory.MAX_SEARCH_LIMIT` | `limits.search_memory_max_search_limit` | `1_000` |
| `search_memory.MAX_GENERATION_FTS_CHUNKS` | `limits.search_memory_max_generation_fts_chunks` | `100_000` |
| `self_update.FETCH_TIMEOUT_SECONDS` | `limits.self_update_fetch_timeout_seconds` | `120.0` |
| `symbol_snippet.MAX_SNIPPET_LINES` | `limits.symbol_snippet_max_snippet_lines` | `120` |
| `symbol_snippet.MAX_NAME_MATCHES` | `limits.symbol_snippet_max_name_matches` | `200` |
| `sync_memory.DEPENDENCY_TIMEOUT_SECONDS` | `limits.sync_memory_dependency_timeout_seconds` | `30.0` |
| `workspace_revision._MAX_INVENTORY_HINT_ENTRIES` | `limits.workspace_revision_max_inventory_hint_entries` | `4096` |
| `workspace_revision._MAX_INVENTORY_HINT_PATH_BYTES` | `limits.workspace_revision_max_inventory_hint_path_bytes` | `1024 * 1024` |

## Required qualification before installation

Regression coverage must cross each former hidden boundary, exercise an override,
reject invalid settings and preserve durable continuation or an explicit refusal.
Run the actual complexity and branch-shape checks, relevant integration tests,
refresh the native graph, and update the canonical contract after approval and
before implementation. Approval alone does not establish audit closure.
