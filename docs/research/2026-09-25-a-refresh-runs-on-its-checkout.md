# A refresh runs on its checkout

Date: 2026-09-25. Audit item B-38 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code)

- `mcp_server._request_repository_refresh` spawned `repository_index.py refresh <resolved>`, where
  `resolved` is the directory the question named. A question about a subfolder started a refresh
  that the indexer refused (it admits a checkout root), while the answer said `started`.
- The "already requested" mark was keyed by `repository_id`, which two worktrees of one repository
  share (`repository_scope.derive_repository_id` from the common Git directory), so a request from
  one worktree suppressed the other's for the same commit.

## Source

- git-worktree, https://git-scm.com/docs/git-worktree (fetched 2026-09-25): "Manage multiple
  working trees attached to the same repository. A git repository can support multiple working
  trees, allowing you to check out more than one branch at a time." One repository, several
  checkouts: the unit of a refresh is the checkout, which `RepositoryScope` already names
  (`checkout_id`, `checkout_root`).

## Decision

- The refresh runs on `checkout.checkout_root`, and the "already requested" mark and the refresh
  log are keyed by `checkout_id`.

## Files

- `scripts/mcp_server.py`
- `tests/test_a_refresh_runs_on_its_checkout.py`
- `tests/test_a_refresh_that_never_started_is_asked_for_again.py`
- `CHANGELOG.md`
