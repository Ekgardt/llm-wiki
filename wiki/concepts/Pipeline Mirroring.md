---
type: concept
title: "Pipeline Mirroring"
description: "A vault convention — when adding a new durable-knowledge subsystem, reuse the shape of an existing pipeline in this repo rather than inventing a parallel structure."
timestamp: 2026-07-03T05:41:37
---
# Pipeline Mirroring

One-sentence summary: A vault convention — when adding a new durable-knowledge subsystem, reuse the shape of an existing pipeline in this repo rather than inventing a parallel structure.

## What it is
This vault's core pipeline is three-layered:

```
raw/   →  inbox/       →  wiki/
immutable  staging       compiled pages + index + log
```

Pipeline mirroring is the rule that any new subsystem meant to accumulate knowledge over time should copy the same shape (immutable capture → optional staging → compiled pages with editorial index and log). The canonical application is the `memory/` subsystem: `memory/daily/` plays the raw role, `memory/knowledge/<type>/` plays the compiled role, `memory/index.md` + `memory/log.md` mirror the wiki's editorial pair.

## Why it matters
- Conventions transfer unchanged: provenance rules, editorial notes, index+log registration, compile cadence.
- One mental model covers every layer — readers don't learn a second structure.
- Cross-layer promotion (e.g. `bridge-promote-insight`) becomes obvious when source and target shapes match.

## When to apply
Adding any new layer intended to hold durable content that grows over time.

## When *not* to apply
Scratch workspaces, exports (`outputs/`), build artifacts, and tooling directories — these aren't knowledge subsystems and don't need the three-layer shape.

## Source
- Origin: session memory — [[memory/knowledge/concepts/pipeline-mirroring]] (noun-form definition) and [[memory/knowledge/patterns/mirror-existing-pipelines]] (imperative pattern). Distilled from `memory/daily/2026-04-13.md` [01:04:32].
- Canonical pipeline it mirrors: [[Karpathy LLM Wiki Workflow]].

This page is a **promotion from session memory**, not a compilation of external source material. It was lifted here because the rule applies to any author extending the vault, not only to the session in which it was articulated.

## Related
- [[Karpathy LLM Wiki Workflow]] — the pipeline whose shape is being mirrored.
- [[Memory Subsystem Action Plan]] — the first application of the rule.
- [[Preliminary Flagging]] — sibling concept; another vault convention surfaced by the same session.
- [[memory/knowledge/patterns/mirror-existing-pipelines]] — imperative-form pattern with apply-when heuristic.
- [[memory/knowledge/concepts/pipeline-mirroring]] — noun-form definition with fuller discussion.
- [[Editorial Notes Pattern]] — sibling convention surfaced by the same 2026-04-13 session.
- [[2026-04-13 Three Conventions One Root|Three Conventions, One Root]] — connection page placing this convention against the two siblings.
- [[Global Multi-Project Migration Plan]] — applies this convention again: `wiki/projects/` as a new mirrored axis.
