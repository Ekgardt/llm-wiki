"""A refusal names what it lacks, and retrieval is asked for exactly that, once.

Run 1, 2026-09-08: 28 of 186 answerable questions were refused with reasons
that name the missing evidence. Now a refusal, or an answer that lost a claim
at a gate, turns its reason and the dropped claims into a few queries, what
they find joins the candidates, and the answer is generated once more. The
dropped claims leave the product's answer; the stand's answer mode may keep
them, labelled. See `docs/research/2026-09-08-a-refusal-searches-again.md`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
BENCHMARK = Path(__file__).resolve().parents[1] / "benchmark"
for folder in (SCRIPTS, BENCHMARK):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

import refusal_pass  # noqa: E402
from corpus_snapshot import collect_corpus  # noqa: E402
from longmemeval_vault import hypothesis_of  # noqa: E402
from query_memory import grounded_qa  # noqa: E402


def _write_note(vault: Path, name: str, body: str) -> None:
    (vault / "knowledge" / "notes" / name).write_text(
        "---\ntype: concept\nsource_authority: user\nconfidence: high\n---\n\n"
        f"# {name}\n\n{body}\n",
        encoding="utf-8",
    )


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "knowledge" / "notes").mkdir(parents=True)
    (root / "knowledge" / "daily").mkdir(parents=True)
    _write_note(root, "racket.md", "I have been practising with my new tennis racket all week.")
    _write_note(root, "shop.md", "I bought the tennis racket at the sports store downtown on Monday.")
    return root


def _refusal(reason: str) -> str:
    return json.dumps(
        {
            "schema_version": "grounded-answer/v1",
            "status": "insufficient_evidence",
            "claims": [],
            "citations": [],
            "reason": reason,
        }
    )


def _answer_citing(prompt: str, text: str, wanted: str) -> str:
    manifest = prompt.split("<evidence_manifest>\n", 1)[1].split("\n</evidence_manifest>", 1)[0]
    evidence = next(item for item in json.loads(manifest) if wanted in item["relative_path"])
    citation = {key: evidence[key] for key in evidence if key != "text"}
    return json.dumps(
        {
            "schema_version": "grounded-answer/v1",
            "status": "answered",
            "claims": [{"text": text, "citation_ids": [evidence["citation_id"]]}],
            "citations": [citation],
            "reason": None,
        }
    )


class _Stand:
    """Retrieval finds the racket page; only a second search finds the shop page."""

    def __init__(self, vault: Path) -> None:
        self.snapshot = collect_corpus(vault)
        self.chunks = {
            name: next(c for c in self.snapshot.chunks if c.source_path.endswith(f"{name}.md"))
            for name in ("racket", "shop")
        }
        self.queries: list[str] = []
        self.missing_prompts: list[str] = []
        self.answer_prompts: list[str] = []
        self.first_reply = _refusal("The evidence confirms a new tennis racket but no span states where it was bought.")

    def retrieve(self, limit: int) -> tuple:
        return (self.chunks["racket"],)

    def search(self, query: str, limit: int) -> tuple:
        self.queries.append(query)
        return (self.chunks["shop"],)

    def generate(self, prompt: str, system_prompt: str, max_tokens: int) -> str:
        if system_prompt == refusal_pass.MISSING_SYSTEM_PROMPT:
            self.missing_prompts.append(prompt)
            return '{"queries": ["tennis racket bought store", "sports store downtown"]}'
        self.answer_prompts.append(prompt)
        if len(self.answer_prompts) == 1:
            return self.first_reply
        return _answer_citing(prompt, "You bought the tennis racket at the sports store downtown.", "shop")


def _ask(stand: _Stand, vault: Path, **extra) -> dict:
    return grounded_qa(
        "Where did I buy my new tennis racket?",
        vault=vault,
        snapshot=stand.snapshot,
        retrieve=stand.retrieve,
        search=stand.search,
        generator=stand.generate,
        profile="BASE",
        **extra,
    )


def test_a_refusal_searches_for_what_its_reason_names_and_answers(vault: Path) -> None:
    stand = _Stand(vault)

    document = _ask(stand, vault)

    assert stand.queries == ["tennis racket bought store", "sports store downtown"]
    assert "no span states where it was bought" in stand.missing_prompts[0]
    assert len(stand.answer_prompts) == 2
    assert "shop.md" in stand.answer_prompts[1]
    assert document["status"] == "answered"
    assert "sports store downtown" in document["claims"][0]["text"]


def _unsupported_claim(prompt: str) -> str:
    """A claim citing the racket page for a fact that page does not carry."""
    manifest = prompt.split("<evidence_manifest>\n", 1)[1].split("\n</evidence_manifest>", 1)[0]
    item = json.loads(manifest)[0]
    return json.dumps(
        {
            "schema_version": "grounded-answer/v1",
            "status": "answered",
            "claims": [{"text": "Purchased at a downtown sports store.", "citation_ids": [item["citation_id"]]}],
            "citations": [{key: item[key] for key in item if key != "text"}],
            "reason": None,
        }
    )


class _DroppingStand(_Stand):
    """The first answer carries one claim the citation gate refuses."""

    def generate(self, prompt: str, system_prompt: str, max_tokens: int) -> str:
        if system_prompt != refusal_pass.MISSING_SYSTEM_PROMPT and not self.answer_prompts:
            self.answer_prompts.append(prompt)
            return _unsupported_claim(prompt)
        return super().generate(prompt, system_prompt, max_tokens)


def test_a_dropped_claim_is_a_reason_to_search_and_its_text_is_the_query_material(vault: Path) -> None:
    stand = _DroppingStand(vault)

    document = _ask(stand, vault)

    assert "Purchased at a downtown sports store." in stand.missing_prompts[0]
    assert len(stand.answer_prompts) == 2
    assert document["status"] == "answered"
    assert "unverified_claims" not in document


def test_answer_mode_keeps_the_dropped_claims_labelled_and_the_product_does_not(vault: Path) -> None:
    stand = _Stand(vault)
    stand.search = lambda query, limit: ()  # the second search finds nothing

    refused = _ask(stand, vault)
    kept = _ask(_Stand(vault), vault, keep_unverified=True)

    assert refused["status"] == "insufficient_evidence"
    assert "unverified_claims" not in refused
    assert kept["status"] == "answered"
    assert kept["unverified_claims"] == []


def test_the_stand_reports_an_uncited_answer_only_under_answer_policy() -> None:
    document = {"status": "insufficient_evidence", "claims": [], "unverified_claims": ["Bought downtown."]}

    assert hypothesis_of(document) == ""
    assert hypothesis_of(document, "answer") == "(uncited) Bought downtown."
    assert hypothesis_of({"status": "answered", "claims": [{"text": "Yes."}]}, "answer") == "Yes."


def test_a_reply_without_queries_searches_nothing_and_keeps_the_refusal(vault: Path) -> None:
    stand = _Stand(vault)
    stand.generate = lambda prompt, system_prompt, max_tokens: (
        "not json" if system_prompt == refusal_pass.MISSING_SYSTEM_PROMPT else stand.first_reply
    )

    document = _ask(stand, vault)

    assert stand.queries == []
    assert document["status"] == "insufficient_evidence"
