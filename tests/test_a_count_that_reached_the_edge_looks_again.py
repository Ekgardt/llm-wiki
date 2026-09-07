"""An answer that counted what it was shown gets one more look, and only one.

Measured over three judged runs of 200 on 2026-09-07: a declared count or sum
whose inputs reached the edge of what retrieval returned was wrong 50% of the
time against 18% otherwise, and a count over everything it found was still
wrong 4.0 times a run because two names for one thing were counted as two.

The trigger is what the answer declared it did — `derivation` is `count` or
`sum` — never a word in the question. The remedy is bounded: retrieval asked
once more and wider when the count cites the last page it still held, one
clustering call over the counted items, one more answer, adopted only when it
answered. See `scripts/aggregation_pass.py`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import aggregation_pass  # noqa: E402
from corpus_snapshot import collect_corpus  # noqa: E402
from query_memory import QA_MAX_CANDIDATES, WIDENED_CANDIDATES, grounded_qa  # noqa: E402

# --- the pieces --------------------------------------------------------------


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


# --- the whole path -------------------------------------------------------------


def _write_page(vault: Path, name: str, body: str) -> Path:
    path = vault / "knowledge" / "notes" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\ntype: concept\nsource_authority: user\nconfidence: high\n---\n\n"
        f"# {name.removesuffix('.md').title()}\n\n{body}\n",
        encoding="utf-8",
        newline="",
    )
    return path


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    for name in ("notes", "projects", "daily"):
        (root / "knowledge" / name).mkdir(parents=True)
    return root


def _manifest(prompt: str) -> list[dict]:
    body = prompt.split("<evidence_manifest>\n", 1)[1].split("\n</evidence_manifest>", 1)[0]
    return json.loads(body)


def _count_answer(prompt: str, text: str, inputs: list[str]) -> str:
    evidence = _manifest(prompt)
    return json.dumps(
        {
            "schema_version": "grounded-answer/v1",
            "status": "answered",
            "claims": [
                {
                    "text": text,
                    "citation_ids": [item["citation_id"] for item in evidence],
                    "derivation": "count",
                    "inputs": inputs,
                }
            ],
            "citations": [
                {key: item[key] for key in item if key != "text"} for item in evidence
            ],
            "reason": None,
        }
    )


class _Stand:
    """Two pages, a third one retrieval finds only when asked wider, and a scripted model."""

    def __init__(self, vault: Path) -> None:
        _write_page(vault, "alpha.md", "I ordered from Domino's on Monday.")
        _write_page(vault, "beta.md", "Domino's Pizza delivered again on Friday.")
        _write_page(vault, "gamma.md", "Pizza Hut on Sunday, from Domino's rival.")
        self.vault = vault
        self.snapshot = collect_corpus(vault)
        self.chunks = {
            name: next(c for c in self.snapshot.chunks if c.source_path.endswith(f"{name}.md"))
            for name in ("alpha", "beta", "gamma")
        }
        self.limits: list[int] = []
        self.answer_prompts: list[str] = []
        self.cluster_prompts: list[str] = []
        self.second_status = "answered"

    def retrieve(self, limit: int) -> tuple:
        self.limits.append(limit)
        if limit > QA_MAX_CANDIDATES:
            return (self.chunks["alpha"], self.chunks["beta"], self.chunks["gamma"])
        return (self.chunks["alpha"], self.chunks["beta"])

    def generate(self, prompt: str, system_prompt: str, max_tokens: int) -> str:
        if system_prompt == aggregation_pass.CLUSTER_SYSTEM_PROMPT:
            self.cluster_prompts.append(prompt)
            return '{"groups": [[0, 1]]}'
        self.answer_prompts.append(prompt)
        if len(self.answer_prompts) == 1:
            return _count_answer(
                prompt, "Two places: Domino's and Domino's Pizza.", ["Domino's", "Domino's Pizza"]
            )
        return self._second(prompt)

    def _second(self, prompt: str) -> str:
        if self.second_status != "answered":
            return json.dumps(
                {
                    "schema_version": "grounded-answer/v1",
                    "status": self.second_status,
                    "claims": [],
                    "citations": [],
                    "reason": "nothing",
                }
            )
        return _count_answer(prompt, "One place, Domino's, counted once.", ["Domino's"])


def test_a_count_at_the_edge_with_two_names_for_one_thing_is_answered_again(vault: Path) -> None:
    stand = _Stand(vault)

    document = grounded_qa(
        "How many places did I order from?",
        vault=vault,
        snapshot=stand.snapshot,
        retrieve=stand.retrieve,
        generator=stand.generate,
        profile="BASE",
    )

    assert stand.limits == [QA_MAX_CANDIDATES, WIDENED_CANDIDATES]
    assert len(stand.cluster_prompts) == 1
    assert len(stand.answer_prompts) == 2
    assert "<entity_groups>" in stand.answer_prompts[1]
    assert "- Domino's; Domino's Pizza" in stand.answer_prompts[1]
    # The wider pass reached the third page.
    assert "gamma.md" in stand.answer_prompts[1]
    assert document["claims"][0]["text"] == "One place, Domino's, counted once."


def test_a_second_look_that_goes_silent_keeps_the_first_answer(vault: Path) -> None:
    stand = _Stand(vault)
    stand.second_status = "insufficient_evidence"

    document = grounded_qa(
        "How many places did I order from?",
        vault=vault,
        snapshot=stand.snapshot,
        retrieve=stand.retrieve,
        generator=stand.generate,
        profile="BASE",
    )

    assert len(stand.answer_prompts) == 2
    assert document["status"] == "answered"
    assert document["claims"][0]["text"] == "Two places: Domino's and Domino's Pizza."


def test_fixed_candidates_cannot_be_widened_but_are_still_clustered(vault: Path) -> None:
    stand = _Stand(vault)

    grounded_qa(
        "How many places did I order from?",
        vault=vault,
        snapshot=stand.snapshot,
        candidates=(stand.chunks["alpha"], stand.chunks["beta"]),
        generator=stand.generate,
        profile="BASE",
    )

    assert stand.limits == []
    assert len(stand.cluster_prompts) == 1
    assert len(stand.answer_prompts) == 2
    assert "gamma.md" not in stand.answer_prompts[1]


def test_an_answer_that_counted_nothing_is_generated_once(vault: Path) -> None:
    stand = _Stand(vault)

    def plain(prompt: str, system_prompt: str, max_tokens: int) -> str:
        stand.answer_prompts.append(prompt)
        evidence = _manifest(prompt)[0]
        return json.dumps(
            {
                "schema_version": "grounded-answer/v1",
                "status": "answered",
                "claims": [{"text": "Domino's on Monday.", "citation_ids": [evidence["citation_id"]]}],
                "citations": [{key: evidence[key] for key in evidence if key != "text"}],
                "reason": None,
            }
        )

    grounded_qa(
        "Where did I order on Monday?",
        vault=vault,
        snapshot=stand.snapshot,
        retrieve=stand.retrieve,
        generator=plain,
        profile="BASE",
    )

    assert stand.limits == [QA_MAX_CANDIDATES]
    assert len(stand.answer_prompts) == 1


def test_the_schema_admits_inputs_only_as_short_strings() -> None:
    from query_memory import ANSWER_SCHEMA
    from reliable_memory import validate_schema

    def document(inputs: object) -> dict:
        return {
            "schema_version": "grounded-answer/v1",
            "status": "answered",
            "claims": [{"text": "two", "citation_ids": ["E1"], "derivation": "count", "inputs": inputs}],
            "citations": [],
            "reason": None,
        }

    validate_schema(document(["a", "b"]), ANSWER_SCHEMA)
    for bad in (["", "b"], [1], "a", ["x" * 201]):
        with pytest.raises(Exception):
            validate_schema(document(bad), ANSWER_SCHEMA)


def test_the_bound_is_one_wider_pass_of_twice_the_candidates() -> None:
    assert WIDENED_CANDIDATES == 2 * QA_MAX_CANDIDATES
    assert aggregation_pass.CLUSTER_SET_SIZE == 9
