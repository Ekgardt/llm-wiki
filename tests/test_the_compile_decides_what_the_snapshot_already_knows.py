"""The snapshot decides create-or-update, and every operation needs a verdict.

Findings M-A6 and M-A7 of the third audit. See
`docs/research/2026-09-17-the-compile-decides-what-the-snapshot-already-knows.md`.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType

import pytest
from compile_cache import CompileCache
from llm_client import LLMResult, ProviderDescriptor
from markdown_transaction import MarkdownCoordinator

QUOTE = "A durable exact-byte observation."
SLUG = "exact-byte-pattern"


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    root = tmp_path / "vault"
    state_root = tmp_path / "state"
    state_root.mkdir()
    for relative in ("knowledge/daily/receipts", "knowledge/notes"):
        (root / relative).mkdir(parents=True)
    (root / "knowledge/index.md").write_bytes(b"# Old index\n")
    (root / "knowledge/log.md").write_bytes(b"# Session Memory Log\n")
    (root / "AGENTS.md").write_bytes(b"agent contract\n")

    import compile_memory

    for name, value in (
        ("ROOT", root),
        ("STATE_ROOT", state_root),
        ("MEMORY", root / "knowledge"),
        ("DAILY_DIR", root / "knowledge/daily"),
        ("KNOWLEDGE", root / "knowledge/notes"),
        ("INDEX", root / "knowledge/index.md"),
        ("LOG", root / "knowledge/log.md"),
        ("AGENTS", root / "AGENTS.md"),
    ):
        monkeypatch.setattr(compile_memory, name, value)
    return root, state_root


def _daily(root: Path) -> Path:
    path = root / "knowledge/daily/2026-07-14.md"
    path.write_bytes(f"## [10:00:00] session-end | manual\n{QUOTE}\n".encode())
    return path


def _operation(action: str, slug: str = SLUG) -> dict[str, object]:
    return {
        "action": action,
        "category": "patterns",
        "slug": slug,
        "title": "Exact Byte Pattern",
        "summary": "Compile the bytes that were actually reviewed.",
        "body_section": "Lesson",
        "body_markdown": "Use an immutable snapshot so later appends remain pending.",
        "evidence": [
            {
                "daily_date": "2026-07-14",
                "timestamp": "10:00:00",
                "quoted_text": QUOTE,
                "claim": "The reviewed source is immutable.",
            }
        ],
        "related": [],
    }


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


def _provider() -> ProviderDescriptor:
    return ProviderDescriptor(
        provider="fake",
        model="fake-model",
        capabilities=MappingProxyType(
            {"structured_output": "native", "max_tokens_enforced": True}
        ),
        inference_settings=MappingProxyType({"max_tokens": 4000}),
        candidate_index=0,
        fallback_from=(),
    )


def _replies(monkeypatch: pytest.MonkeyPatch, replies: list[str]) -> list[str]:
    """Answer each provider call with the next reply, remembering the prompts."""
    import compile_memory

    prompts: list[str] = []
    provider = _provider()
    monkeypatch.setattr(
        compile_memory, "provider_candidates", lambda *args, **kwargs: [provider]
    )
    monkeypatch.setattr(compile_memory, "probe_candidate", lambda descriptor: True)

    def call(descriptor, prompt, system_prompt, **kwargs):
        prompts.append(prompt)
        return LLMResult(descriptor, replies.pop(0), True, None, "native")

    monkeypatch.setattr(compile_memory, "call_candidate", call)
    return prompts


def _resolved(root: Path, state_root: Path, daily: Path):
    import compile_memory

    inputs = compile_memory.snapshot_compile_inputs([daily])
    return compile_memory.resolve_compile_plan(
        inputs,
        CompileCache(state_root),
        coordinator=MarkdownCoordinator(root, state_root),
    )


def _kinds(resolved) -> list[str]:
    operations = resolved.plan["operations"]
    return [str(item["kind"]) for item in operations]


def test_a_create_for_a_page_that_exists_becomes_an_update(vault, monkeypatch, capsys):
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


def test_an_update_for_a_page_that_is_absent_becomes_a_create(vault, monkeypatch):
    root, state_root = vault
    daily = _daily(root)
    _replies(monkeypatch, [_draft("update"), _reviews((SLUG, "pass"))])

    resolved = _resolved(root, state_root, daily)

    assert _kinds(resolved) == ["create"]


def test_an_operation_the_critique_skipped_is_asked_about_again(vault, monkeypatch):
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

    assert (_kinds(resolved), len(prompts)) == (["create"], 3)
    assert SLUG in prompts[2]


def test_an_operation_without_a_verdict_is_never_written(vault, monkeypatch, capsys):
    root, state_root = vault
    daily = _daily(root)
    replies = [_draft("create"), _reviews(), _reviews()] * 3
    _replies(monkeypatch, replies)

    with pytest.raises(RuntimeError, match="validated compile plan"):
        _resolved(root, state_root, daily)

    assert f"critique gave no verdict for: {SLUG}" in capsys.readouterr().err
