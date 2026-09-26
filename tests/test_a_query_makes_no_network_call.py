"""A language server answering a query does not fetch modules or crates.

rust-analyzer's `cargo metadata` and gopls's `go list` could download into the
managed root while answering. See
docs/research/2026-09-25-a-query-makes-no-network-call.md.
"""

from __future__ import annotations

import lsp_profiles
from lsp_server_profile import thaw_profile_value


def test_the_query_environments_are_offline() -> None:
    go = dict(lsp_profiles.GOPLS_ENVIRONMENT_TEMPLATE)
    rust = dict(lsp_profiles.RUST_ENVIRONMENT_TEMPLATE)

    assert (go["GOPROXY"], go["GOTOOLCHAIN"], rust["CARGO_NET_OFFLINE"]) == ("off", "local", "true")


# What keeps each managed server off the network while it answers. A profile
# added to the registry without an entry here fails the guard below, so the
# question is asked for every new server, not remembered (audit 2026-09-26 C-8,
# docs/research/2026-09-26-no-managed-server-reaches-the-network.md). Pyright
# resolves only what is on disk and has no download path to switch off.
OFFLINE_SWITCHES = {
    "pyright": (),
    "typescript": (("initialization", ("disableAutomaticTypingAcquisition",), True),),
    "gopls": (("environment", ("GOPROXY",), "off"),),
    "rust-analyzer": (
        ("environment", ("CARGO_NET_OFFLINE",), "true"),
        ("configuration", ("rust-analyzer", "cargo", "noDeps"), True),
    ),
}


def _declared(profile, where: str) -> object:
    if where == "environment":
        return dict(profile.environment_template)
    if where == "initialization":
        return thaw_profile_value(profile.initialization_options)
    return profile.wire_configuration()


def _at(value: object, path: tuple[str, ...]) -> object:
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def _switch_values(name: str) -> list[object]:
    profile = lsp_profiles.REGISTRY.get(name)
    return [_at(_declared(profile, where), path) for where, path, _ in OFFLINE_SWITCHES[name]]


def _expected_values(name: str) -> list[object]:
    return [value for _, _, value in OFFLINE_SWITCHES[name]]


def test_every_managed_server_declares_how_it_stays_offline() -> None:
    names = sorted(lsp_profiles.REGISTRY.names())

    assert names == sorted(OFFLINE_SWITCHES)
    assert [_switch_values(name) for name in names] == [_expected_values(name) for name in names]
