# Agent-Native Foundation Design

## Status

Approved by the user on 2026-07-13. This design defines the first implementation
stage of the broader agent-native roadmap.

## Goal

Make MCP the common external interface for every supported agent while keeping
native hooks and plugins as thin lifecycle-event adapters. Routine operation is
automatic; agents consume health, context, and actions without a human UI.

## Architecture

Native integrations emit one versioned event envelope containing event identity,
type, timestamps, agent, session, project, worktree, severity, schema version,
content hash, parent event, and a typed payload. Missing source fields remain null;
adapters must not invent values. Existing capture scripts retain their current
input formats as adapter boundaries.

The MCP server exposes a small task-shaped tool set and application-driven
resources. Every tool returns the same structured envelope with schema version,
generation and index timestamps, source commit, freshness, coverage, confidence,
fallback and partial-result indicators, warnings, and data. Text JSON remains for
clients that do not support structured content.

Health is agent-readable rather than visual. `doctor` performs read-only checks;
`doctor --repair` applies only idempotent, non-destructive repairs. `sync` aligns
dependencies, integration configuration, and regenerable indexes without touching
personal knowledge. SessionStart injects health only when degraded.

## Boundaries

- MCP does not replace lifecycle capture because MCP servers do not receive the
  host application's full conversation.
- Native adapters contain no classification or knowledge logic.
- Markdown remains the durable source of truth.
- Runtime health and event state remain under gitignored `cache/`, `logs/`, and
  `run/`.
- No web UI, cloud synchronization, or mandatory human confirmation is added.
- Destructive repair and remote network operations are outside this stage.

## Data Flow

1. A host-specific hook observes a lifecycle event.
2. The adapter normalizes it to the common event envelope.
3. The capture pipeline validates, redacts, deduplicates, and persists the event.
4. Agents discover knowledge resources and invoke task-shaped actions through MCP.
5. MCP responses report evidence quality and freshness in a uniform envelope.
6. Doctor checks the same contracts used by installers and integrations.

## Failure Handling

Invalid events are rejected without breaking the host session. Capture errors are
recorded locally without the original secret-bearing payload. MCP returns explicit
partial and fallback metadata. Doctor repairs are atomic and idempotent; failed
repairs leave the previous valid configuration intact.

## Testing

- Contract tests feed equivalent events from Python and OpenCode-shaped adapters.
- Schema tests cover missing optional fields, hashes, timestamps, and redaction.
- MCP tests validate input/output schemas and backward-compatible text output.
- Doctor tests run against isolated fake homes and state roots.
- Installer tests verify repeated repair produces no additional changes.

## Research Basis

- MCP architecture and capability negotiation:
  https://modelcontextprotocol.io/specification/2025-06-18/architecture
- MCP tools, structured content, and output schemas:
  https://modelcontextprotocol.io/specification/2025-06-18/server/tools
- MCP resources and subscriptions:
  https://modelcontextprotocol.io/specification/2025-06-18/server/resources
- Anthropic context engineering and minimal task-shaped tools:
  https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- OpenTelemetry log/event data model:
  https://opentelemetry.io/docs/specs/otel/logs/data-model/
