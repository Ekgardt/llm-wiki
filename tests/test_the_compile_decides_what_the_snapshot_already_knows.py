"""The snapshot decides create-or-update, and every operation needs a verdict.

Findings M-A6 and M-A7 of the third audit. See
`docs/research/2026-09-17-the-compile-decides-what-the-snapshot-already-knows.md`.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from compile_cache import CompileCache
from markdown_transaction import MarkdownCoordinator

from tests.the_compile_decides_what_the_snapshot_already_knows_helpers import (
    QUOTE,
    SLUG,
    _operation,
    _replies,
    vault,  # noqa: F401
)


def _daily(root: Path) -> Path:
    path = root / "knowledge/daily/2026-07-14.md"
    path.write_bytes(f"## [10:00:00] session-end | manual\n{QUOTE}\n".encode())
    return path


def _draft(action: str, slug: str = SLUG) -> str:
    return json.dumps({"operations": [_operation(action, slug)], "audit": {}})


def _reviews(*verdicts: tuple[str, str]) -> str:
    return json.dumps(
        {
            "reviews": [
                {"slug": slug, "verdict": verdict, "reason": "ok"}
                for slug, verdict in verdicts
            ]
        }
    )


def _resolved(root: Path, state_root: Path, daily: Path):
    from llm_wiki import compile_pipeline_batches, compile_pipeline_sources

    inputs = compile_pipeline_sources.snapshot_compile_inputs([daily])
    return compile_pipeline_batches.resolve_compile_plan(
        inputs,
        CompileCache(state_root),
        coordinator=MarkdownCoordinator(root, state_root),
    )


def _kinds(resolved) -> list[str]:
    operations = resolved.plan["operations"]
    return [str(item["kind"]) for item in operations]


def test_a_create_for_a_page_that_exists_becomes_an_update(vault, monkeypatch, capsys):  # noqa: F811
    """The model is never shown most slugs, so the snapshot decides the action."""
    root, state_root = vault
    daily = _daily(root)
    (root / f"knowledge/notes/{SLUG}.md").write_bytes(
        b"---\ntype: pattern\n---\n# Exact Byte Pattern\n"
    )
    _replies(monkeypatch, [_draft("create"), _reviews((SLUG, "pass"))])

    resolved = _resolved(root, state_root, daily)

    assert _kinds(resolved) == ["replace"]
    assert "drafted create, the snapshot says update" in capsys.readouterr().err


def test_an_operation_for_a_retired_page_is_dropped_not_written(vault, monkeypatch, capsys):  # noqa: F811
    """Rule 12: a superseded page is history, so the compile does not append to it."""
    root, state_root = vault
    daily = _daily(root)
    (root / f"knowledge/notes/{SLUG}.md").write_bytes(
        b"---\ntype: pattern\nstatus: superseded\n---\n# Exact Byte Pattern\n"
    )
    _replies(monkeypatch, [_draft("create")])

    resolved = _resolved(root, state_root, daily)

    assert _kinds(resolved) == []
    assert f"{SLUG}: dropped, that page is superseded" in capsys.readouterr().err


def test_an_update_for_a_page_that_is_absent_becomes_a_create(vault, monkeypatch):  # noqa: F811
    root, state_root = vault
    daily = _daily(root)
    _replies(monkeypatch, [_draft("update"), _reviews((SLUG, "pass"))])

    resolved = _resolved(root, state_root, daily)

    assert _kinds(resolved) == ["create"]


def test_an_operation_the_critique_skipped_is_asked_about_again(vault, monkeypatch):  # noqa: F811
    """A verdict for another slug is no verdict for this one."""
    root, state_root = vault
    daily = _daily(root)
    prompts = _replies(
        monkeypatch,
        [
            _draft("create"),
            _reviews(("some-other-slug", "drop")),
            _reviews((SLUG, "pass")),
        ],
    )

    resolved = _resolved(root, state_root, daily)

    assert (_kinds(resolved), len(prompts)) == (["create"], 4)
    assert SLUG in prompts[2]
    assert 'COMPLETE SELECTED SOURCES' in prompts[3]


def test_an_operation_without_a_verdict_is_never_written(vault, monkeypatch, capsys):  # noqa: F811
    root, state_root = vault
    daily = _daily(root)
    replies = [_draft("create"), _reviews(), _reviews()] * 3
    _replies(monkeypatch, replies)

    with pytest.raises(RuntimeError, match="validated compile plan"):
        _resolved(root, state_root, daily)

    assert f"critique gave no verdict for: {SLUG}" in capsys.readouterr().err
