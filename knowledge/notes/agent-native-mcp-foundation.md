---
type: decision
title: "MCP Is The Common Agent Interface, Native Adapters Supply Lifecycle Events"
description: "LLM Wiki uses MCP as the common read/action interface for every agent while host-specific hooks remain thin lifecycle-event adapters."
timestamp: 2026-07-13T00:00:00
confidence: high
source_authority: user
status: active
---
# MCP Is The Common Agent Interface, Native Adapters Supply Lifecycle Events

One-sentence summary: LLM Wiki uses MCP as the common read/action interface for every agent while host-specific hooks remain thin lifecycle-event adapters.

## Decision

Date: 2026-07-13.

Chose: one versioned event envelope for capture, MCP tools and resources for all
agent-facing reads and actions, and agent-readable health instead of a web UI.
Routine validation, repair, classification, and promotion remain automatic.

Kept: minimal native hooks and plugins for lifecycle events. MCP servers are
intentionally isolated from the host's full conversation, so MCP cannot replace
capture adapters.

Rejected: a human dashboard, mandatory user confirmation for normal memory
maintenance, cloud synchronization, and duplicated memory logic in every agent
integration.

Why: the product serves one owner with many concurrent agents. MCP provides the
portable protocol boundary, while thin native adapters preserve reliable event
capture on hosts with different lifecycle APIs.

## Evidence

- User approval in the 2026-07-13 architecture review.
- MCP architecture: https://modelcontextprotocol.io/specification/2025-06-18/architecture
- MCP tools: https://modelcontextprotocol.io/specification/2025-06-18/server/tools
- MCP resources: https://modelcontextprotocol.io/specification/2025-06-18/server/resources
- Design specification: `docs/superpowers/specs/2026-07-13-agent-native-foundation-design.md`.

## Related

- [[knowledge/notes/reliable-memory-stage-2]]
- [[knowledge/notes/centralized-memory-subsystem]]
- [[knowledge/notes/mirror-existing-pipelines]]
- [[solo-operator-superset-product-decision]]
