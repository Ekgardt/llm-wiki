"""A repository past an extraction ceiling is a named refusal, not a crash (audit 2026-09-26 B-11).

docs/research/2026-09-26-a-ceiling-is-a-named-refusal.md
"""
from __future__ import annotations

import functools

import code_extractor
import pytest
import repository_index
from repository_refusal import RepositoryIndexRefused

from tests.test_repository_index import ALPHA, _repository, vault  # noqa: F401


def test_the_extractor_holds_what_the_index_admits() -> None:
    assert code_extractor.ExtractionLimits().max_sources >= repository_index.MAX_INDEXED_SOURCES


def test_a_repository_past_the_ceiling_is_refused_by_name(vault, tmp_path, monkeypatch):  # noqa: F811
    _root, state = vault
    files = {f"src/m{number}.py": ALPHA for number in range(3)}
    repository = _repository(tmp_path / "repo", files)
    monkeypatch.setattr(code_extractor, "ExtractionLimits", functools.partial(code_extractor.ExtractionLimits, max_sources=2))

    with pytest.raises(RepositoryIndexRefused) as refusal:
        repository_index.index_repository(repository, state_root=state)

    assert refusal.value.reason == "repository_exceeds_extraction_bounds"
