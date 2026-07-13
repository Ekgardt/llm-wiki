# Agent-Native Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every supported agent one event contract, one MCP contract, and one automatic health/repair path.

**Architecture:** Native hooks remain thin event producers. Shared Python modules validate and persist versioned events, build MCP response envelopes/resources, and run idempotent diagnostics. Existing hook and MCP entry points remain compatible while moving internal behavior to the shared contracts.

**Tech Stack:** Python 3.10 stdlib, MCP Python SDK, JSON Schema-shaped dictionaries, pytest, Git.

---

### Task 1: Versioned Event Envelope

**Files:** `scripts/event_envelope.py`, `scripts/user_prompt_capture.py`, `scripts/post_tool_capture.py`, `tests/test_event_envelope.py`.

- [ ] Write failing tests for canonical IDs, UTC timestamps, content hashes, optional source fields, and invalid event types.
- [ ] Run `uv run pytest tests/test_event_envelope.py -q`; expect import failure for the missing module.
- [ ] Implement immutable envelope construction and validation without persistence or network calls.
- [ ] Adapt prompt and tool capture to construct the envelope before their existing durable append path.
- [ ] Run focused tests, full pytest, and Ruff.

### Task 2: MCP Response Contract And Resources

**Files:** `scripts/mcp_contract.py`, `scripts/mcp_server.py`, `tests/test_mcp_server.py`.

- [ ] Write failing tests for a uniform response envelope and health/context resources.
- [ ] Run focused tests; expect failure from the missing contract.
- [ ] Implement freshness, coverage, confidence, fallback, partial state, warnings, and data fields.
- [ ] Add MCP resources when supported by the installed SDK, retaining text JSON compatibility.
- [ ] Run focused tests, full pytest, and Ruff.

### Task 3: Agent-Readable Doctor

**Files:** `scripts/doctor.py`, `scripts/mcp_server.py`, `scripts/session_start_context.py`, `tests/test_doctor.py`, `tests/test_mcp_server.py`.

- [ ] Write failing tests for environment, runtime, queue, index, scheduler, MCP, and integration checks.
- [ ] Run focused tests; expect import failure for the absent doctor.
- [ ] Implement read-only checks and idempotent non-destructive repairs behind `--repair`.
- [ ] Expose doctor through MCP and inject only degraded summaries at SessionStart.
- [ ] Run focused tests, full pytest, and Ruff.

### Task 4: Integration Contract Tests

**Files:** `scripts/llm-wiki-memory-opencode.js`, `scripts/codex_memory.py`, `integrations/claude-code/settings.json`, `tests/test_integration_injection.py`, `tests/test_plugin_helpers.py`.

- [ ] Write failing fixtures showing each integration maps source fields to the same envelope.
- [ ] Run focused tests and verify the contract mismatch.
- [ ] Reduce adapters to event normalization and shared pipeline entry points.
- [ ] Verify safe degradation when optional host fields are absent.
- [ ] Run focused tests, full pytest, and Ruff.

### Task 5: Product Cleanup And Documentation

**Files:** remove `integrations/obsidian/Article-to-Inbox.json`; update all README translations, architecture, structure, user guide, integration docs, agent contracts, and structural tests.

- [ ] Write failing structure/documentation tests for the agent-native integration model.
- [ ] Remove Web Clipper wiring and describe Obsidian only as an optional Markdown viewer.
- [ ] Remove QMD claims from the active architecture while preserving historical records.
- [ ] Keep `AGENTS.md` and `CLAUDE.md` byte-identical.
- [ ] Run README i18n, full pytest, Ruff, and the retrieval benchmark.

## Self-Review

The plan covers the first-stage design without introducing later transaction,
retrieval, semantic-code, or scale projects. Each task has a behavioral red-green
boundary. Runtime and path changes remain within the approved three-zone layout.
