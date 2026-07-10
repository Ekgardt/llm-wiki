---
type: skill
name: knowledge-review
argument-hint: "[optional area or folder]"
description: Review the health of the wiki for gaps, contradictions, weak linking, stale pages, and missing source provenance.
disable-model-invocation: true
context: fork
agent: Explore
allowed-tools: Read Glob Grep LS Bash(uv run python scripts/lint_memory.py *)
title: "SKILL"
timestamp: 2026-07-03T05:41:37
---
Review `$ARGUMENTS` if provided, otherwise review the whole wiki.

Checklist:
1. Identify orphan pages or pages with weak inbound/outbound linking.
2. Identify pages missing `Source:` references.
3. Identify duplicate or near-duplicate concepts.
4. Identify pages that should be split because they are too broad.
5. Identify contradictions and superseded claims.
6. Suggest the top 5 highest-value cleanup actions.

Return:
- a concise health report
- exact files involved
- the most important fixes first.
