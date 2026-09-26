"""A refresh runs on its checkout's root, once per checkout and commit.

It ran on the directory asked about (a subfolder refused), and one worktree's
request stood for the whole repository. See
docs/research/2026-09-25-a-refresh-runs-on-its-checkout.md.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import mcp_server
import memory_state


def _checkout(root: Path, name: str) -> SimpleNamespace:
    return SimpleNamespace(
        repository_id="repository:" + "a" * 64,
        checkout_id=f"checkout:{name}",
        checkout_root=str(root / name),
        git_commit="c" * 40,
    )


def test_each_worktree_is_refreshed_from_its_root(tmp_path: Path, monkeypatch) -> None:
    spawned: list[list[str]] = []

    def spawn(args, **_options):
        spawned.append(args)
        return 4242

    monkeypatch.setattr(memory_state, "STATE_ROOT", tmp_path, raising=False)
    monkeypatch.setattr(memory_state, "spawn_detached", spawn)
    monkeypatch.setattr(mcp_server, "_REFRESH_REQUESTED", {})
    first, second = _checkout(tmp_path, "main"), _checkout(tmp_path, "feature")

    answers = [
        mcp_server._request_repository_refresh(first),
        mcp_server._request_repository_refresh(second),
    ]

    assert (answers, [args[-1] for args in spawned]) == (
        ["started", "started"],
        [first.checkout_root, second.checkout_root],
    )
