---
type: qa
title: "Where does a source file live after it's been compiled — knowledge/inbox/ or knowledge/raw/?"
description: "Once a source file has been compiled into durable wiki pages, move it from knowledge/inbox/ to knowledge/raw/; knowledge/inbox/ is staging for *unprocessed* material only."
timestamp: 2026-07-03T05:41:37
confidence: medium
source_authority: user
---
# Where does a source file live after it's been compiled — `knowledge/inbox/` or `knowledge/raw/`?

One-sentence summary: Once a source file has been compiled into durable wiki pages, move it from `knowledge/inbox/` to `knowledge/raw/`; `knowledge/inbox/` is staging for *unprocessed* material only.

## Question
When a captured article (e.g. `knowledge/inbox/articles/Thread by @karpathy.md`) has already been compiled into wiki pages and cited as their source, should it stay in `knowledge/inbox/` or move to `knowledge/raw/`?

## Answer
**Move it to `knowledge/raw/`.** The project contract (CLAUDE.md + `knowledge/raw/README.md`) treats:
- `knowledge/raw/` as immutable **source-of-truth** inputs,
- `knowledge/inbox/` as a **staging area** for newly captured material *not yet* compiled.

A file that has already been lifted into the wiki is, by definition, no longer staged — it is the immutable source behind live `Source:` citations. Leaving it in `knowledge/inbox/` after compilation mislabels its status and, over time, turns `knowledge/inbox/` into an archive rather than a queue.

## Mechanics of the move
1. Move the file: `mv knowledge/inbox/<subpath>/<file> knowledge/raw/<subpath>/<file>` (preserve subpath).
2. Update every `Source:` line across `knowledge/notes/` that cites the old `knowledge/inbox/...` path to the new `knowledge/raw/...` path.
3. Update any raw-source record in `knowledge/notes/` if it lists `Local file:` — reflect the new path.
4. Append a `knowledge/log.md` entry describing the move and the path update.

## Why not keep it in `knowledge/inbox/` permanently?
- `knowledge/inbox/` should always answer the question *"what do I still need to process?"* at a glance. Compiled material poisons that signal.
- `knowledge/raw/` immutability rule is strongest when the directory contains only canonical sources — mixing staging with source-of-truth dilutes the rule.

## Evidence
- `knowledge/daily/2026-04-13.md` — hygiene pass that executed exactly this move for the Karpathy thread, updating seven wiki pages + one raw-source record.
- `knowledge/log.md` entry dated 2026-04-13 documenting the move.

## Related
- [[knowledge/notes/provenance-rule-6]] — the root rule that makes correct source-path labeling matter.
- [[Ingestion Workflow]] — the full capture → compile pipeline.
- [[Review Workflow]] — cadence at which this hygiene check should run.
- [[knowledge/notes/no-gitkeep-in-inbox-articles]] — sibling decision about the `inbox/articles/` directory this Q&A governs.
