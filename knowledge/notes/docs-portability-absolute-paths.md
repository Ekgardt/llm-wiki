---
type: pattern
title: "Docs Portability: Absolute Paths"
description: "Replace hardcoded absolute paths (`<absolute-path>...`) in canonical documentation with `$ENV_VAR (on this machine: <absolute-path>...)` to keep docs portable across machines while preserving a concrete sanity-check referenc"
timestamp: 2026-07-03T05:41:37
---
# Docs Portability: Absolute Paths

One-sentence summary: Replace hardcoded absolute paths (`<absolute-path>...`) in canonical documentation with `$ENV_VAR (on this machine: <absolute-path>...)` to keep docs portable across machines while preserving a concrete sanity-check reference.

## Lesson

Docs written with hardcoded Windows absolute paths break on any other machine or OS, and even on the same machine after a directory reorganization. The `$ENV_VAR` form solves portability. But removing the concrete path entirely makes docs useless to someone debugging a live system — they cannot tell at a glance whether the env var resolves to the right place.

**The pattern:** write both:

```
$LLM_WIKI_ROOT (on this machine: $LLM_WIKI_ROOT)
$LLM_WIKI_STATE_ROOT (on this machine: $LLM_WIKI_STATE_ROOT)
```

The env var is the portable reference; the parenthetical is the concrete sanity anchor for the person reading the doc while actually running the system.

### Where to apply
- READMEs and architecture docs that describe directory layouts.
- Setup and re-setup guides (e.g., `knowledge/projects/llm-knowledge/notes/re-setup.md`).
- Any canonical explanatory text that refers to paths that differ across machines.

### Where NOT to apply
- Machine-local config files (`settings.local.json`, `~/.claude/settings.json`) — those are explicitly machine-specific and should contain concrete paths.
- Code / scripts — use `os.environ.get("LLM_WIKI_ROOT")` directly; the parenthetical note is for human readers, not runtime code.
- `re-setup.md` concrete-path columns in setup tables — intentionally machine-specific; label them as such instead.
- One-off commands in commit messages or ephemeral notes — portability overhead outweighs the benefit.

## Evidence
- `knowledge/daily/2026-04-19.md` [23:13:01] — audit v7 session; pattern applied to `README.md` and synthesis pages that contained bare `$LLM_WIKI_STATE_ROOT/` references.

## Related
- [[knowledge/notes/hook-scripts-defense-in-depth]] — `_resolve_state_root()` fallback is the *runtime* version of this same portability concern: when the env var is missing, fall back rather than hardcode.
- [[knowledge/notes/hook-errors-silent-without-state-root]] — what happens when `LLM_WIKI_STATE_ROOT` is absent entirely, making even the fallback insufficient without the explicit env block.
