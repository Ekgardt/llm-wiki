"""Real context delivery retains the pruned source spans and their hashes."""
from __future__ import annotations

import hashlib
from pathlib import Path

import query_memory
from context_budget import ContextBudget
from corpus_snapshot import collect_corpus

from tests.modules.evidence_pruning.test_pruning import CONTENT, _encode


def _vault_with(tmp_path: Path) -> tuple[Path, str]:
    vault = tmp_path / "vault"
    (vault / "knowledge" / "daily").mkdir(parents=True)
    (vault / "knowledge" / "notes").mkdir(parents=True)
    daily = "knowledge/daily/2023-05-01.md"
    (vault / daily).write_bytes(CONTENT)
    return vault, daily


def _hashes_hold(evidence) -> bool:
    return all(
        hashlib.sha256(CONTENT[item.byte_start : item.byte_end]).hexdigest() == item.span_sha256
        for item in evidence
    )


def test_the_context_delivers_pruned_spans_that_still_hash(tmp_path: Path, monkeypatch) -> None:
    vault, daily = _vault_with(tmp_path)
    monkeypatch.setattr(query_memory, "_sentence_encoder", lambda: _encode)
    snapshot = collect_corpus(vault, code_roots=(), daily_paths=[daily])
    reply = next(chunk for chunk in snapshot.chunks if chunk.text.startswith("**assistant:**"))

    context = query_memory.build_grounded_context(
        snapshot,
        (reply,),
        vault=vault,
        profile="BASE",
        budget=ContextBudget(None, 65_536, 1200, 512),
        question="drum set care",
    )

    joined = "\n".join(item.text for item in context.evidence)
    assert "**user:** How do I care" in joined
    assert "Pearl Export" in joined and joined.count("a great deal more") < 12
    assert _hashes_hold(context.evidence)
