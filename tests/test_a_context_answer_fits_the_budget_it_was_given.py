"""A context answer fits the budget it was given, names each item once, and counts one way (audit 2026-09-27 B-12).

One page returned 200 bytes of text inside 4.3 KB of lists and traces; the budget
bounded only the text, counted at one token per byte, while the cost block counted
`chars/4`. See docs/research/2026-09-27-a-context-answer-fits-the-budget-it-was-given.md.
"""
from __future__ import annotations

import json
import time
import tracemalloc
from pathlib import Path

import answer_budget
import answer_cost
import code_navigation_renderer
import mcp_server
import memory_state
import pytest

SLUGS = ["choice", "incident", "state"]
ANSWER_KEYS = {
    "text", "packed_tokens", "token_budget", "corpus_generation", "repo_map", "items", "dropped",
    "missing_slugs", "include", "collected_at",
}


@pytest.fixture
def vault(tmp_path: Path, monkeypatch) -> Path:
    notes, project = tmp_path / "knowledge/notes", tmp_path / "knowledge/projects/demo"
    notes.mkdir(parents=True)
    project.mkdir(parents=True)
    (notes / "choice.md").write_text(
        "---\ntype: decision\nstatus: active\n---\n# Choice\n\nOne-sentence summary: Keep one package.\n\n"
        "## Evidence\n\nproof\n", encoding="utf-8")
    (notes / "incident.md").write_text(
        "---\ntype: debugging\nstatus: active\n---\n# Incident\n\nOne-sentence summary: Compiler regression.\n",
        encoding="utf-8")
    (project / "state.md").write_text(
        "---\ntype: project-state\nstatus: active\n---\n# Demo\n\nOne-sentence summary: Active Task17.\n",
        encoding="utf-8")
    monkeypatch.setattr(memory_state, "ROOT", tmp_path)
    return tmp_path


# The same bytes these budgets held under the earlier 4-bytes estimate (400 and 1200).
@pytest.mark.parametrize("budget", [800, 2400])
def test_the_whole_answer_fits_the_budget_not_only_its_text(vault: Path, budget: int) -> None:
    answer = mcp_server._get_context(SLUGS, token_budget=budget)

    assert answer_budget.estimate_tokens(answer) <= budget


def test_the_answer_names_each_item_once(vault: Path) -> None:
    answer = mcp_server._get_context(SLUGS, token_budget=1200)
    places = [json.dumps(item, sort_keys=True) for item in answer["items"]]

    assert (set(answer), len(places)) == (ANSWER_KEYS, len(set(places)))


def test_an_item_does_not_spell_its_source_again(vault: Path) -> None:
    answer = mcp_server._get_context(SLUGS, token_budget=1200)

    assert [item for item in answer["items"] if {"item_id", "source_sha256", "text"} & set(item)] == []


def test_every_answer_counts_tokens_one_way(vault: Path) -> None:
    answer = mcp_server._get_context(SLUGS, token_budget=1200)

    assert (
        answer["packed_tokens"] == answer_budget.estimate_text_tokens(answer["text"]),
        code_navigation_renderer.estimate_tokens is answer_budget.estimate_text_tokens,
        answer_cost.ESTIMATE_METHOD,
    ) == (True, True, "utf8_bytes/2")


def test_the_estimate_counts_bytes_so_cyrillic_is_not_undercounted() -> None:
    assert (answer_budget.estimate_text_tokens("abcd"), answer_budget.estimate_text_tokens("привет")) == (2, 6)


def test_a_budget_the_item_list_cannot_fit_is_refused_not_exceeded(vault: Path, monkeypatch) -> None:
    monkeypatch.setattr(answer_budget, "estimate_tokens", lambda data: 10**6)

    with pytest.raises(ValueError, match="token_budget cannot hold"):
        mcp_server._get_context(SLUGS, token_budget=400)


def _unexpected_model_call(*args, **kwargs):
    pytest.fail("packing an explicit context request must not call a model")


@pytest.mark.parametrize("budget", [8192, 32_768, 32_769, 65_536, 1_000_000])
def test_a_caller_budget_is_an_allowance_not_a_fixed_server_ceiling(vault, monkeypatch, budget):
    import llm_client

    monkeypatch.setattr(llm_client, "call_llm_result", _unexpected_model_call)
    arguments = {"slugs": SLUGS, "token_budget": budget}
    assert mcp_server._validate_tool_arguments("get_context", arguments) is None
    tracemalloc.start()
    started = time.perf_counter()
    try:
        answer = mcp_server._get_context(SLUGS, token_budget=budget)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    elapsed = time.perf_counter() - started
    ordinary = mcp_server._get_context(SLUGS, token_budget=8192)

    assert answer["text"] == ordinary["text"]
    assert answer["items"] == ordinary["items"]
    assert answer["missing_slugs"] == []
    assert answer_budget.estimate_tokens(answer) <= budget
    print(f"budget={budget} elapsed={elapsed:.6f}s peak_python_bytes={peak}")


def test_a_tiny_positive_budget_is_refused_for_actual_answer_size(vault):
    assert mcp_server._validate_tool_arguments("get_context", {"slugs": SLUGS, "token_budget": 1}) is None
    with pytest.raises(ValueError, match="token_budget cannot hold"):
        mcp_server._get_context(SLUGS, token_budget=1)


def test_a_larger_requested_budget_can_return_more_real_evidence(vault):
    sections = [f"## Section {index}\n\n" + "actual-source-evidence " * 30 for index in range(200)]
    (vault / "knowledge/notes/large.md").write_text(
        "---\ntype: concept\n---\n# Large evidence\n\n" + "\n\n".join(sections), encoding="utf-8"
    )

    # Ask for room for the complete selected evidence and response metadata;
    # 65,536 was too small for this fixture's complete evidence package.
    answer = mcp_server._get_context(["large"], token_budget=200_000)

    assert 32_768 < answer_budget.estimate_tokens(answer) <= 200_000
    assert "actual-source-evidence" in answer["text"]
    assert answer["repo_map"] == ["knowledge/notes/large.md"]
    assert answer["missing_slugs"] == []


@pytest.mark.parametrize("budget", [True, False, 0, -1, 1.5, "65536"])
def test_invalid_context_budgets_remain_refused(vault, budget):
    assert mcp_server._validate_tool_arguments("get_context", {"slugs": SLUGS, "token_budget": budget})
    with pytest.raises(ValueError, match="token_budget"):
        mcp_server._get_context(SLUGS, token_budget=budget)


def _write_context_pages(vault, slugs):
    for slug in slugs:
        (vault / "knowledge/notes" / f"{slug}.md").write_text(
            f"---\ntype: concept\nstatus: active\n---\n# {slug}\n\nOne-sentence summary: Fact for {slug}.\n"
        )


def test_twenty_one_real_pages_fit_the_callers_existing_budget(vault):
    slugs = [f"extra-{number}" for number in range(21)]
    _write_context_pages(vault, slugs)
    included = [f"compatibility-{number}-" + "x" * 65 for number in range(11)]
    arguments = {"slugs": slugs, "include": included, "token_budget": 8192}
    assert mcp_server._validate_tool_arguments("get_context", arguments) is None
    answer = mcp_server._get_context(slugs, included, token_budget=8192)
    assert answer["repo_map"] == sorted(f"knowledge/notes/{slug}.md" for slug in slugs)
    assert answer["missing_slugs"] == []
    assert answer["include"] == included
    assert all(f"Fact for {slug}." in answer["text"] for slug in slugs)
    assert answer_budget.estimate_tokens(answer) <= 8192


def test_compatibility_metadata_cannot_overrun_the_callers_budget(vault):
    with pytest.raises(ValueError, match="token_budget cannot hold"):
        mcp_server._get_context(SLUGS, ["x" * 2000], token_budget=100)
