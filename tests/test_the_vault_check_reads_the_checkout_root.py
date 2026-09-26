"""A question about a vault subfolder never starts a daytime refresh of the vault (audit 2026-09-26 B-10).

docs/research/2026-09-26-the-vault-check-reads-the-checkout-root.md
"""
from __future__ import annotations

from types import SimpleNamespace

import mcp_server
import memory_state


def test_a_vault_subfolder_is_left_to_the_nightly(tmp_path, monkeypatch) -> None:
    spawned: list[list[str]] = []
    monkeypatch.setattr(memory_state, "ROOT", tmp_path, raising=False)
    monkeypatch.setattr(memory_state, "STATE_ROOT", tmp_path, raising=False)
    monkeypatch.setattr(memory_state, "spawn_detached", lambda args, **_kw: spawned.append(args) or 1)
    monkeypatch.setattr(mcp_server, "_REFRESH_REQUESTED", {})
    vault_checkout = SimpleNamespace(
        checkout_root=str(tmp_path), checkout_id="checkout:" + "v" * 64, git_commit="c" * 40
    )

    answer = mcp_server._refresh_action(vault_checkout, stale=True)

    assert (answer, spawned) == ("vault_nightly", [])
