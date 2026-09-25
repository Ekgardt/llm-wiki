"""A language server answering a query does not fetch modules or crates.

rust-analyzer's `cargo metadata` and gopls's `go list` could download into the
managed root while answering. See
docs/research/2026-09-25-a-query-makes-no-network-call.md.
"""

from __future__ import annotations

import lsp_profiles


def test_the_query_environments_are_offline() -> None:
    go = dict(lsp_profiles.GOPLS_ENVIRONMENT_TEMPLATE)
    rust = dict(lsp_profiles.RUST_ENVIRONMENT_TEMPLATE)

    assert (go["GOPROXY"], go["GOTOOLCHAIN"], rust["CARGO_NET_OFFLINE"]) == ("off", "local", "true")
