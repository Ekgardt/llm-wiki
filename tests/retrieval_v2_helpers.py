"""Shared paths and fixtures for the retrieval v2 benchmark tests; puts benchmark/ on sys.path."""
from __future__ import annotations

import sys
from pathlib import Path

BENCHMARK = Path(__file__).resolve().parent.parent / "benchmark"
if str(BENCHMARK) not in sys.path:
    sys.path.insert(0, str(BENCHMARK))
ROOT = BENCHMARK.parent
CORPUS = BENCHMARK / "retrieval-v2.json"
SCHEMA = BENCHMARK / "retrieval-v2.schema.json"
RUNNER = BENCHMARK / "run_retrieval_v2.py"

import retrieval_v2_contract  # noqa: E402


def all_normalized(scores) -> bool:
    return all(0.0 <= score <= 1.0 for score in scores)


def spec_identity(spec) -> tuple[str, str, str | None]:
    reranker = spec["reranker"]
    reranker_id = reranker["id"] if reranker else None
    return (spec["embedding"]["id"], spec["embedding"]["variant_id"], reranker_id)


def lexical_candidate(evidence_id, text, *, language="EN", project="alpha"):
    return retrieval_v2_contract.Candidate(
        evidence_id=evidence_id,
        parent_id=f"parent-{evidence_id}",
        relative_path=f"synthetic/{evidence_id}.md",
        language=language,
        project=project,
        status="active",
        valid_from="2025-01-01",
        valid_to=None,
        heading_path=("Heading",),
        text=text,
    )


class PinnedJieba:
    __version__ = "0.42.1"

    def __init__(self):
        self.calls = []

    def cut(self, text, HMM=True):
        self.calls.append((text, HMM))
        return text.replace("\u7f13\u5b58\u76ee\u5f55", "\u7f13\u5b58 \u76ee\u5f55").split()
