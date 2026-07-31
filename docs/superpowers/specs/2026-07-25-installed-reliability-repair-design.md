# Installed LLM Wiki Reliability Repair Design

## Goal

Restore the installed v3 pipeline so capture remains compact, all SDK work uses
`openai/gpt-5.6-luna`, compilation and deferred work complete reliably, and
retrieval exposes one current canonical knowledge corpus.

## Constraints

- Repair the installed vault at `$LLM_WIKI_ROOT` in place; do not migrate it to
  the incomplete public v4 architecture.
- Preserve user knowledge and runtime state before cleanup.
- Never replace `knowledge/`, `run/`, `logs/`, or `cache/` wholesale.
- All production-data cleanup must support dry-run, backup, and an audit report.
- Do not commit without explicit user permission.

## Design

### Capture

OpenCode forwards user prompts, meaningful tool targets, idle transcripts, and
pre-compact transcripts to focused Python helpers. Empty targets, high-frequency
shell probes, tier-only classifications, and AI-generated feedback candidates
are discarded. Project state is resolved from the active directory and included
in session context.

### SDK Service Work

Classifier, compile, and deferred queue prompts explicitly select
`openai/gpt-5.6-luna`. Every ephemeral session is owned by one operation and is
cleaned with `abort` followed by `delete` in `finally`. Failures are persisted in
vault runtime state and never represented as successful maintenance.

### Compilation

Compiler input is normalized before model submission. Empty tool breadcrumbs and
empty idle summaries are excluded. Meaningful timestamp blocks are grouped into
bounded batches. A daily hash is published only after all batches for that daily
have committed successfully. Oversized individual blocks fail visibly rather
than overflowing the provider context.

### Context And Retrieval

The context budget is allocated per section. Guardrails, health, current project
state, latest daily excerpt, recent editorial log, and a bounded index excerpt
each receive reserved space. Duplicate-note candidates are classified for
manual review only; cleanup never removes them automatically.

### Maintenance And Installation

Nightly exits nonzero while queue or compile work remains failed. Weekly holds a
single lease for the full mutation sequence and rebuilds indexes after its final
write. Installers honor `XDG_CONFIG_HOME` and stop on dependency or test failure.

### Cleanup

A migration command inventories candidate changes and can write a timestamped
backup. Its automatic actions are limited to verified empty breadcrumbs/idle
summaries and quarantine of known false feedback candidates. Only backup-only
creates a manifest; mutating apply requires that explicit reviewed manifest. Duplicate notes
remain report-only for manual review. Orphan `memory-*` sessions are report-only
until a separately reviewed safe API deletion path exists. Cleanup does not
modify ordinary sessions, raw sources, inbox material, or unique personal notes.

## Verification

Each behavior is introduced through a failing regression test. Completion
requires the full suite with no thread warnings, Ruff, PowerShell and Bash syntax
checks, memory lint, a controlled OpenCode restart, and live end-to-end evidence
for capture, precompact, Luna metadata, cleanup, queue drain, bounded compile,
context completeness, and canonical retrieval.
