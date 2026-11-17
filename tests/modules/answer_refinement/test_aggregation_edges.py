"""Original pure counting assertions, unchanged."""
from __future__ import annotations

import aggregation_pass


def _answer(claims: list[dict], citations: list[dict] | None = None) -> dict:
    return {"status": "answered", "claims": claims, "citations": citations or []}


def _citation(name: str, path: str) -> dict:
    return {"citation_id": name, "relative_path": path}


def test_a_count_citing_the_last_ranked_page_reaches_the_edge() -> None:
    answer = _answer(
        [{"text": "two", "citation_ids": ["E1", "E2"], "derivation": "count"}],
        [_citation("E1", "a.md"), _citation("E2", "b.md")],
    )

    assert aggregation_pass.reaches_the_edge(answer, ("a.md", "b.md"))
    assert not aggregation_pass.reaches_the_edge(answer, ("a.md", "b.md", "c.md"))


def test_a_plain_claim_never_reaches_the_edge() -> None:
    answer = _answer(
        [{"text": "blue", "citation_ids": ["E2"]}], [_citation("E2", "b.md")]
    )

    assert not aggregation_pass.reaches_the_edge(answer, ("a.md", "b.md"))
    assert not aggregation_pass.reaches_the_edge(answer, ())


def test_counted_inputs_are_gathered_once_each_and_stripped() -> None:
    answer = _answer(
        [
            {"text": "x", "citation_ids": [], "derivation": "count", "inputs": [" A ", "B"]},
            {"text": "y", "citation_ids": [], "derivation": "sum", "inputs": ["B", "", "C"]},
            {"text": "z", "citation_ids": [], "derivation": "latest", "inputs": ["D"]},
        ]
    )

    assert aggregation_pass.counted_inputs(answer) == ["A", "B", "C"]


def test_one_clustering_call_names_the_mentions_that_are_one_thing() -> None:
    prompts: list[str] = []

    def cluster(prompt: str) -> str:
        prompts.append(prompt)
        return '```json\n{"groups": [[0, 1], [2]]}\n```'

    groups = aggregation_pass.duplicate_groups(["Domino's Pizza", "Domino's", "Pizza Hut"], cluster)

    # Like spellings adjacent: the sort puts both Domino's before Pizza Hut.
    assert groups == [["Domino's", "Domino's Pizza"]]
    assert prompts == ["0: Domino's\n1: Domino's Pizza\n2: Pizza Hut"]


def test_a_reply_that_is_not_what_was_asked_finds_nothing() -> None:
    for reply in ("not json", '{"groups": "no"}', '{"groups": [[0, 9]]}', '["x"]', None):
        assert aggregation_pass.duplicate_groups(["a", "b"], lambda _prompt: reply) == []


def test_fewer_than_two_inputs_make_no_call() -> None:
    def never(_prompt: str) -> str:
        raise AssertionError("no call expected")

    assert aggregation_pass.duplicate_groups(["only"], never) == []


def test_the_set_is_cut_into_nines() -> None:
    sizes: list[int] = []

    def cluster(prompt: str) -> str:
        sizes.append(prompt.count("\n") + 1)
        return '{"groups": []}'

    aggregation_pass.duplicate_groups([f"m{index:02d}" for index in range(20)], cluster)

    assert sizes == [9, 9, 2]


def test_the_note_is_empty_without_groups_and_data_with_them() -> None:
    assert aggregation_pass.entity_note([]) == ""
    note = aggregation_pass.entity_note([["Domino's", "Domino's Pizza"]])
    assert note.startswith("<entity_groups>\n")
    assert "- Domino's; Domino's Pizza" in note
