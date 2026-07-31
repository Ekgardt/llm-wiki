---
title: XDG OpenCode Plugin Destination
type: decision
status: current
confidence: high
source_authority: user
date: 2026-07-26
---

# XDG OpenCode Plugin Destination

One-sentence summary: OpenCode plugins install to the effective XDG config directory, with one distinct Windows compatibility destination and normalized path deduplication.

## Decision

Install `llm-wiki-memory.js` under `$XDG_CONFIG_HOME/opencode/plugins/` only
when `XDG_CONFIG_HOME` is absolute. If it is unset, empty, or relative, ignore
it and use the absolute `~/.config/opencode/plugins/` fallback.

On Windows, also install to `~/.config/opencode/plugins/` when that location
is distinct from the effective XDG destination. Normalize destinations and
compare them case-insensitively before copying.

## Rationale

OpenCode documents its global configuration and plugin directories beneath
`~/.config/opencode`, while the XDG Base Directory Specification makes
`XDG_CONFIG_HOME` the effective user configuration root and defaults it to
`~/.config`. The Windows fallback preserves compatibility without producing
duplicate copies when both paths resolve to the same directory.

## Alternatives Rejected

- Always installing only to `~/.config/opencode` ignores an explicit XDG root.
- Installing unnormalized candidate paths can copy the same plugin more than once.
- Hardcoding a machine-specific directory makes the installer non-portable.

## Evidence

- Approved installer hardening plan: `docs/superpowers/plans/2026-07-25-installed-reliability-repair.md`, Task 8.
- OpenCode configuration documentation: https://opencode.ai/docs/config/
- XDG Base Directory Specification 0.8: https://specifications.freedesktop.org/basedir-spec/latest/

## Related

- [[knowledge/notes/centralized-memory-subsystem]]
- [[docs/STRUCTURE]]
