# Retention and prune share one "holds code" predicate

Date: 2026-09-25. Audit item C-42 (docs/AUDIT-2026-09-25-full.md).

## Fact
- `prune_generations` treats a registered generation as memory (its to judge)
  when `not catalog.holds_code(identifier, manifest)`. `holds_code` answers from
  `manifest["code_roots"]` and, for a manifest written before 2026-09-12 that
  lacks it, from the snapshot policy the generation binds.
- `repository_retention._vault_memory` decides the same question for the vault's
  own generations with `not manifest.get("code_roots")` only. A vault code
  generation built before 2026-09-12 is therefore "memory" to retention (skipped)
  and "code" to prune (skipped): nothing ever removes it. The set is bounded (no
  new ones are written without `code_roots`), but it never shrinks.

## Source (fetched 2026-09-25)
Wikipedia, "Don't repeat yourself",
https://en.wikipedia.org/wiki/Don%27t_repeat_yourself: "The DRY principle is
stated as "Every piece of knowledge must have a single, unambiguous, authoritative
representation within a system"." Two collectors that partition one set must ask
one predicate, or the partition has a gap.

## Decision
`_foreign_groups` takes the catalog's `holds_code` and a vault generation is left
to prune only when `holds_code` says it is not code. Retention then groups the
legacy vault code generations with the vault checkout and retires all but the
kept ones, as for every checkout.

## Files
- scripts/repository_retention.py
- tests/test_repository_retention.py
