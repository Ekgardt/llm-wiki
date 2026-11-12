"""Helpers shared by test_the_compile_decides_what_the_snapshot_already_knows.py and the test files importing them."""
from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType

import pytest
from llm_client import LLMResult, ProviderDescriptor

from tests.compile_paths import point_compile_path

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
    monkeypatch.setenv("LLM_WIKI_ROOT", str(root))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))
    return root, state_root


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
    from llm_wiki import compile_pipeline_prompts

    prompts: list[str] = []
    provider = _provider()
    point_compile_path(monkeypatch, "provider_candidates", lambda *args, **kwargs: [provider])
    point_compile_path(monkeypatch, "probe_candidate", lambda descriptor: True)

    def call(descriptor, prompt, system_prompt, **kwargs):
        prompts.append(prompt)
        if kwargs.get('schema') == compile_pipeline_prompts.SOURCE_REVIEW_SCHEMA:
            return LLMResult(descriptor, _source_review(), True, None, "native")
        return LLMResult(descriptor, replies.pop(0), True, None, "native")

    point_compile_path(monkeypatch, "call_candidate", call)
    return prompts


def _source_review(complete=True):
    """Explicit model verdict for fixtures testing publication, not semantic quality."""
    return json.dumps({'source_review': {'complete': complete, 'reason': 'Fixture source assessment.'}})
