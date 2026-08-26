---
type: decision
title: "Managed IDE Hooks Use Install V2 Fragment Ownership"
description: "Cursor and Antigravity user hooks use canonical lifecycle adapters and bounded structural fragments owned by one resumable install v2 update, rollback, and uninstall control plane."
date: 2026-08-16
confidence: high
source_authority: user
status: superseded
superseded_by: [[knowledge/notes/retire-cursor-and-antigravity-decision]]
---
# Managed IDE Hooks Use Install V2 Fragment Ownership

One-sentence summary: Cursor and Antigravity user hooks use canonical lifecycle adapters and bounded structural fragments owned by one resumable install v2 update, rollback, and uninstall control plane.

## Decision

Date: 2026-08-16.

LLM Wiki will manage the official local user hook configurations at
`~/.cursor/hooks.json` and `~/.gemini/config/hooks.json`. Host events pass through
`scripts/integration_adapter.py`, the existing canonical `EventEnvelope`, and the
existing transactional capture/occurrence-receipt path. The integrations do not
implement another memory API and no longer instruct agents to append daily Markdown
directly.

The installer owns structural fragments, never whole user configuration files:

- Cursor ownership is limited to exact LLM-Wiki handler objects inside supported
  `version: 1` hook arrays. Unrelated keys, events, handlers, and ordering remain user
  content.
- Antigravity ownership is the exact top-level `llm-wiki` object in
  `~/.gemini/config/hooks.json`. An existing unrecognized object with that name is a
  conflict and is never overwritten.
- Missing files may be created with private permissions. Existing files keep
  unrelated bytes semantically, preserve supported mode/owner metadata, and receive a
  verified bounded sibling preimage before replacement.
- `run/install/preimages/` contains only exact owned projections and manifest state;
  whole user hook files remain outside runtime state because they may contain
  unrelated commands or credentials.

## Install V2 Update Contract

`install-manifest/v2` and `install-transaction/v2` extend the existing
`run/install/` control plane without another directory, database, daemon, MCP tool, or
environment variable. Version 1 records remain readable and may be adopted only after
their schema, preimages, manifest linkage, and real owned resources validate without
ambiguity.

- Install and update persist all required owned projections before the first external
  mutation. Update keeps the prior manifest active until every target projection is
  verified and the new generation is durably published.
- The original pre-first-install projection remains the uninstall baseline. The exact
  projection before the current update is a separate rollback baseline.
- One latest committed update may be rolled back explicitly. A later committed update
  replaces that bounded rollback point; unbounded release history is not retained.
- Rollback and uninstall restore a projection only when the current owned projection
  still equals the expected installed projection. User drift blocks mutation rather
  than being overwritten.
- Recovery reconstructs historical resources from authenticated records and persisted
  owned projections, not from whichever templates happen to exist in a newer checkout.

## Hook Protocol Boundary

Cursor local user hooks map `sessionStart`, `beforeSubmitPrompt`, `postToolUse`,
`preCompact`, `stop`, and `sessionEnd` to canonical lifecycle events. Stable host IDs
or bounded deterministic identities become replay-stable occurrence IDs. User-level
Cursor hooks do not run in Cursor cloud agents, so cloud capture is outside this
claim.

Antigravity maps initial `PreInvocation` to session start, significant
`PostToolUse` events to tool capture, and idle `Stop` to session end. Hooks without a
truthful canonical lifecycle meaning remain protocol no-ops. Every invocation emits
the host-required neutral JSON object, including fail-open capture errors, without
printing source payloads or secrets.

Both adapters accept bounded JSON, project only allowlisted fields, omit tool output,
attachments, artifact directories, and full errors, and compute identity only after
redaction. Cursor transcript classification remains disabled until an exact documented
trusted subtree exists. Antigravity transcript access is limited to its documented
conversation log shape and existing regular-file/link/content bounds.

## Consequences

- Fresh installs and existing validated v1 installations can reach the same managed
  Cursor/Antigravity state through a resumable v2 operation.
- Installer rollback and uninstall preserve unrelated hook configuration.
- Doctor reports host detection, manifest ownership, active projection, malformed
  config, conflict, and drift separately; it does not repair configuration implicitly.
- Live five-agent behavior, clean-machine update/rollback, and Cursor cloud-agent
  coverage remain external evidence rather than code-level claims.

## Source / Evidence

- Explicit user approval of user-level managed hooks and latest-committed-update
  rollback in the 2026-08-16 OpenCode session.
- Cursor Hooks documentation: https://cursor.com/docs/hooks
- Google Antigravity Hooks documentation: https://antigravity.google/docs/ide/hooks/
- Python `os.replace()` and `os.fsync()` documentation: https://docs.python.org/3/library/os.html
- [[knowledge/notes/install-ownership-control-plane-decision]].
- [[knowledge/notes/integration-config-backup-retention-decision]].
- [[knowledge/notes/agent-native-mcp-foundation]].

## Related

- [[knowledge/notes/install-ownership-control-plane-decision]]
- [[knowledge/notes/integration-config-backup-retention-decision]]
- [[knowledge/notes/audit-closure-security-recovery-control-plane-decision]]
- [[knowledge/notes/agent-native-mcp-foundation]]
- [[knowledge/notes/retire-cursor-and-antigravity-decision]]
