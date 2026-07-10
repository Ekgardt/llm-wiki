---
type: decision
title: "No .gitkeep In knowledge/inbox/articles"
description: "Do not add `.gitkeep` to `knowledge/inbox/articles/` — the directory will be created on demand by scripts at first use."
timestamp: 2026-07-03T05:41:37
confidence: high
source_authority: user
---
# No .gitkeep In knowledge/inbox/articles

One-sentence summary: Do not add `.gitkeep` to `knowledge/inbox/articles/` — the directory will be created on demand by scripts at first use.

## Decision
Date: 2026-04-18.

Chose: leave `knowledge/inbox/articles/` untracked until real content lands. Scripts that write into the path create the directory themselves (`mkdir -p` / `Path.mkdir(parents=True, exist_ok=True)` semantics).

Rejected: add a `.gitkeep` placeholder to pre-reserve the directory in git — unnecessary churn, and a placeholder file has no operational purpose once the first real article arrives.

Why: the directory's existence is not a precondition for any workflow — the first ingestion creates it. Tracking an empty directory adds noise without buying anything.

Follow-up: if a future script ever *reads* `knowledge/inbox/articles/` before anything has written to it and fails on missing-dir, fix the script to tolerate absence rather than reintroducing `.gitkeep`.

## Generalization
The rule is narrower than "never track empty directories" and broader than its title suggests. Apply it anywhere an empty directory would exist *only* to anchor a future workflow, when:
- no script reads the directory before its first write, AND
- the writing script(s) can create the directory themselves.

Counter-examples where `.gitkeep` is still warranted:
- Directories referenced by config that errors on missing-dir before any write happens.
- Directories documented as a contract to external tooling that enumerates contents.

Under those conditions the empty-directory marker *is* operational, and the rule does not apply.

## Related
- [[knowledge/notes/inbox-vs-raw-after-compile]] — governs the `knowledge/inbox/` → `knowledge/raw/` lifecycle this directory participates in.
