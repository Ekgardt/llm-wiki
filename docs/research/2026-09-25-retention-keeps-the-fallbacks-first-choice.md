# Retention keeps the fallback's first choice

Date: 2026-09-25. Audit item C-27 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code)

- `generation_catalog._retained_generations` kept the active generation plus its ancestors along
  `parent_generation_id`. `_fallback_order`, which picks a generation when the active one stops
  validating, tries the active, then the activation history newest first, then ancestors.
- A full rebuild (`doctor --rebuild-generation`, `force_rebuild=True`) registers a generation
  with no parent. After it, retention kept only the active one, and `prune_generations` removed
  the previously active generation — the fallback's first alternative. Reproduced in a test.

## Source

- rpm-ostree administrator handbook, https://coreos.github.io/rpm-ostree/administrator-handbook/
  (fetched 2026-09-25): "By default, the `rpm-ostree upgrade` will keep at most two bootable
  'deployments', though the underlying technology supports more." The retained spare is the
  deployment one can roll back to, which here is the previous activation.

## Decision

- The spares are the first ones the fallback would try: earlier activations newest first, then
  the parent chain, bounded by `RETAINED_ANCESTOR_GENERATIONS` (still 1). An incremental chain
  keeps exactly what it kept before.

## Files

- `scripts/generation_catalog.py`
- `tests/test_retention_keeps_the_fallbacks_first_choice.py`
- `CHANGELOG.md`
