# The vault check reads the checkout root

Date: 2026-09-26. Audit 2026-09-26 item B-10 (a regression of the B-38 fix).

## Fact
- `mcp_server._refresh_action` decides "the vault is refreshed by the nightly"
  with `_is_the_vault(resolved)`, the directory the question named.
- Since B-38 the refresh it starts runs on `checkout.checkout_root`. A question
  about `<vault>/scripts` is not the vault by that test, so it answered `started`
  and spawned `repository_index.py refresh <vault root>` in the daytime, against
  the comment (G-L5) and `docs/CODE-NAVIGATION.md`.

## Source (fetched 2026-09-26)
git-rev-parse documentation, https://git-scm.com/docs/git-rev-parse,
`--show-toplevel`: "Show the (by default, absolute) path of the top-level
directory of the working tree." The checkout a subfolder belongs to is named by
its top level, which is what `checkout.checkout_root` holds; the vault test must
read the same thing the refresh acts on.

## Decision
`_refresh_action` tests `checkout.checkout_root`; the directory asked about is no
longer passed down (the refresh never used it).

## Files
- scripts/mcp_server.py
- tests/test_the_vault_check_reads_the_checkout_root.py
