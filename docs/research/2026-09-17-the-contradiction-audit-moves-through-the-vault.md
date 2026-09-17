# The contradiction audit moves through the vault

Dated 2026-09-17. Finding M-A11 of the third audit (medium, confirmed by reading and by a
test). The research before the fix.

Files: `scripts/lint_memory.py`,
`tests/test_the_contradiction_audit_moves_through_the_vault.py`.

## What was found

- `lint_memory._bounded_page_blob` walks the pages in sorted path order and stops at the
  first page that does not fit the 120 000-character cap. Every weekly run therefore audits
  the same alphabetically first pages and reports the rest of the vault as free of
  contradictions, with no word that it never read them.
- A first page larger than the cap yields an empty blob; the model is asked about nothing,
  answers `NO_CONTRADICTIONS`, and the report says the vault is clean.
- The step is opt-in (`MEMORY_WEEKLY_CONTRADICTIONS=1`) and costs one provider call; the fix
  must not raise that cost.

## Practice on this date

- "In statistics, sampling bias is a bias in which a sample is collected in such a way that
  some members of the intended population have a lower or higher sampling probability than
  others." ([Sampling bias, Wikipedia](https://en.wikipedia.org/wiki/Sampling_bias)). A
  fixed alphabetical prefix gives every later page a probability of zero, for ever. The
  standard remedy at a fixed budget is systematic rotation: each run takes the next window,
  so every member is covered once per cycle.
- A partial audit that reports like a whole one is the failure the repository already
  refused for a missing provider (`docs/research/2026-09-14-an-error-is-not-an-answer.md`):
  what was not checked is said, not implied clean.

## The decision

- The audit keeps a cursor in runtime state (`state.json`, key
  `contradiction_lint_next_page`): the vault-relative path where the next run starts. A run
  takes pages in path order from the cursor, wrapping round, until the next page does not
  fit; that page becomes the new cursor. The cursor moves only when a provider answered.
- A page larger than the whole cap can never be audited; it is skipped and named in the
  report rather than stopping the window.
- When the window is smaller than the vault, the findings carry one plain line saying how
  many pages of how many were read and where the next run continues. When nothing fits, no
  provider call is made.
- Known limit, stated in that line's docstring: pages that fall in different windows are
  never compared with each other. Closing that needs more than one call per run and is the
  owner's cost decision.
- The state key is derived, disposable runtime state; no path, environment variable or
  contract changes.
