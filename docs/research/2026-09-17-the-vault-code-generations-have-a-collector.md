# The vault's code generations have a collector

Dated 2026-09-17. Third audit, finding G-H1. The research before the fix.

Files: `scripts/repository_retention.py`, `scripts/prune_generations.py`,
`tests/test_repository_retention.py`,
`tests/test_the_vault_code_generations_are_retired_behind_the_newest_two.py`.

## What was found

- Since 2026-09-12 the vault carries two kinds of generation for one checkout: memory, which
  the active pointer names, and code, which is only ever registered and is found through
  `GenerationCatalog.code_generation_for_repository`.
- `prune_generations._memory_publications` (fix of 2026-09-15) never calls a generation that
  holds `code_roots` abandoned, and says its lifecycle belongs to `repository_retention`.
- `repository_retention._foreign_groups` skips every generation whose scope is the vault's
  checkout. So no collector owns the vault's code generations: each refresh of a changed
  checkout adds a tree and nothing removes one. They appear forever as `PENDING` in the prune
  report.
- Verified on a temp state root: three code generations of the vault checkout, then
  `retire_repositories` — the plan is empty and all three stay.
- The same exclusion makes `_orphan_hints` remove the vault's hint table every night: `live`
  is built from the plan, and the plan never holds the vault.
- Why the vault was excluded: its group would otherwise contain memory publications in flight
  (registered, not yet activated), which `discard_unactivated` would happily remove. The
  exclusion was by checkout; the thing that needed excluding was the kind.

## Practice on this date

- A collector removes only what no root reaches, and every kind of reference is a root.
  git-gc, NOTES: "git gc tries very hard not to delete objects that are referenced anywhere
  in your repository. In particular, it will keep not only objects referenced by your current
  set of branches and tags, but also objects referenced by the index, remote-tracking
  branches, reflogs [...] and anything else in the refs/* namespace."
  (https://git-scm.com/docs/git-gc, fetched 2026-09-17). Identity first, age never alone.
- For a code generation the root is "newest registration holding `code_roots` for the
  checkout" — the rule every reader uses. Retention for foreign checkouts already keeps that
  one plus one behind it (`RETAINED_ANCESTOR_GENERATIONS`, a reader that resolved just before
  the refresh).

## The decision

- The vault's checkout gets the same collector as any other checkout, for its code
  generations only: `_foreign_groups` skips a vault generation only when its manifest holds
  no `code_roots` (a memory publication, which stays with `prune_generations`). Activated
  generations stay excluded as before. The newest two code generations per checkout are
  always kept; the discard is `discard_unactivated` under the repository fence, as for every
  other checkout.
- `prune_generations` keeps its 2026-09-15 rule unchanged: it never judges a generation that
  holds code. Only its docstring is corrected to name the collector truthfully.
- No path, env contract or runtime location changes.
