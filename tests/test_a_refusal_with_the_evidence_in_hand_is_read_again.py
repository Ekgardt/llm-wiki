"""A refusal on evidence that covers the question is read again, not searched for.

The reader's confidence is one signal and it is nearly blind to whether the
question is answerable (Two Axes, 2026). The second is ours: does one shown
span state the question's terms, its figures and a day inside its window?
When it does, the refusal look re-reads that evidence with the coverage stated
as data; when it does not, the look searches, as it has since 2026-09-08. The
threshold was set on the tune half of the recorded run and frozen.
See `docs/research/2026-09-22-quote-then-answer-and-a-refusal-calibrated-on-twins.md`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import evidence_sufficiency  # noqa: E402
import refusal_pass  # noqa: E402
from corpus_snapshot import collect_corpus  # noqa: E402
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
    _write_note(root, "game.md", "Last weekend I finally beat the Dark Souls game after weeks.")
    _write_note(root, "shop.md", "I bought the game at the store downtown.")
    return root


def _refusal() -> str:
    return json.dumps(
        {
            "schema_version": "grounded-answer/v1",
            "status": "unsupported_time_scope",
            "claims": [],
            "citations": [],
            "reason": "The only game beaten was on a different weekend.",
        }
    )


def _answer_citing(prompt: str, text: str, wanted: str) -> str:
    manifest = prompt.split("<evidence_manifest>\n", 1)[1].split("\n</evidence_manifest>", 1)[0]
    evidence = next(item for item in json.loads(manifest) if wanted in item["relative_path"])
    return json.dumps(
        {
            "schema_version": "grounded-answer/v1",
            "status": "answered",
            "claims": [{"text": text, "citation_ids": [evidence["citation_id"]]}],
            "citations": [{"citation_id": evidence["citation_id"]}],
            "reason": None,
        }
    )


class _Stand:
    """Retrieval finds one page; a search finds the other."""

    def __init__(self, vault: Path, retrieved: str = "game", found: str = "shop") -> None:
        self.snapshot = collect_corpus(vault)
        self.chunks = {
            name: next(c for c in self.snapshot.chunks if c.source_path.endswith(f"{name}.md"))
            for name in ("game", "shop")
        }
        self.retrieved = retrieved
        self.found = found
        self.queries: list[str] = []
        self.answer_prompts: list[str] = []

    def retrieve(self, limit: int) -> tuple:
        return (self.chunks[self.retrieved],)

    def search(self, query: str, limit: int) -> tuple:
        self.queries.append(query)
        return (self.chunks[self.found],)

    def generate(self, prompt: str, system_prompt: str, max_tokens: int) -> str:
        if system_prompt == refusal_pass.MISSING_SYSTEM_PROMPT:
            return '{"queries": ["game beaten weekend"]}'
        self.answer_prompts.append(prompt)
        if len(self.answer_prompts) == 1:
            return _refusal()
        return _answer_citing(prompt, "You finally beat the Dark Souls game.", "game")


def _ask(stand: _Stand, vault: Path) -> dict:
    return grounded_qa(
        "What game did I finally beat?",
        vault=vault,
        snapshot=stand.snapshot,
        retrieve=stand.retrieve,
        search=stand.search,
        generator=stand.generate,
        profile="BASE",
    )


def test_a_refusal_on_covering_evidence_is_read_again_with_the_coverage_beside_the_question(vault: Path) -> None:
    stand = _Stand(vault)

    document = _ask(stand, vault)

    assert stand.queries == []
    assert len(stand.answer_prompts) == 2
    assert "<evidence_coverage>" in stand.answer_prompts[1]
    assert (document["status"], "Dark Souls" in document["claims"][0]["text"]) == ("answered", True)


def test_the_coverage_is_stated_as_data_and_published_on_the_answer(vault: Path) -> None:
    stand = _Stand(vault)

    document = _ask(stand, vault)
    second = stand.answer_prompts[1]

    assert "Measured by the system, not by you" in second
    assert "beat, finally, game" in second
    assert document["sufficiency"]["score"] == 1.0
    assert document["sufficiency"]["missing"] == []


def test_a_refusal_on_evidence_that_covers_little_still_searches(vault: Path) -> None:
    """The shop page names one word of six; the search finds the game page and the answer follows."""
    stand = _Stand(vault, retrieved="shop", found="game")

    document = grounded_qa(
        "Which boss did I finally defeat in the dark souls game?",
        vault=vault,
        snapshot=stand.snapshot,
        retrieve=stand.retrieve,
        search=stand.search,
        generator=stand.generate,
        profile="BASE",
    )

    assert stand.queries == ["game beaten weekend"]
    assert document["status"] == "answered"
    assert document["sufficiency"]["score"] < evidence_sufficiency.SUFFICIENT_TO_READ_AGAIN
