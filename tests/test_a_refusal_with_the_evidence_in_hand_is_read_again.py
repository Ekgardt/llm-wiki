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
from collections import namedtuple
from datetime import date
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import evidence_sufficiency  # noqa: E402
import refusal_pass  # noqa: E402
from corpus_snapshot import collect_corpus  # noqa: E402
from query_memory import grounded_qa  # noqa: E402

Span = namedtuple("Span", "text relative_path")
ASKED = date(2023, 5, 30)


def test_the_unit_is_one_span_not_the_union_of_all_of_them() -> None:
    """Twelve topical distractors say every word between them; one span says them together."""
    scattered = [Span("I finally beat the boss.", "a.md"), Span("A great game night.", "b.md")]
    together = [Span("I finally beat the game's last boss.", "c.md")]
    question = "What game did I finally beat?"

    best_of_scattered = evidence_sufficiency.sufficiency(question, scattered, ASKED)

    assert (best_of_scattered.covered, best_of_scattered.missing) == (("beat", "finally"), ("game",))
    assert evidence_sufficiency.sufficiency(question, together, ASKED).score == 1.0


def test_a_day_inside_the_window_is_an_aspect_and_the_file_name_dates_the_span() -> None:
    """"Last weekend" asked on Tuesday 2023-05-30 is 05-27/28; a Friday 05-26 entry is inside three days."""
    question = "What game did I finally beat last weekend?"
    friday = [Span("I finally beat the game.", "knowledge/daily/2023-05-26.md")]
    far = [Span("I finally beat the game.", "knowledge/daily/2023-05-01.md")]

    near, distant = (
        evidence_sufficiency.sufficiency(question, spans, ASKED) for spans in (friday, far)
    )

    assert evidence_sufficiency.question_window(question, ASKED) == (date(2023, 5, 27), date(2023, 5, 28))
    assert (near.score, near.days_from_window) == (1.0, 1)
    assert (distant.score, distant.days_from_window, distant.missing) == (0.75, 26, ("days 2023-05-27..2023-05-28",))


def test_the_threshold_is_the_frozen_tune_value() -> None:
    covered = evidence_sufficiency.Sufficiency(("a", "b", "c"), ("d",))
    thin = evidence_sufficiency.Sufficiency(("a",), ("b", "c"))
    nothing = evidence_sufficiency.Sufficiency((), ())

    assert evidence_sufficiency.SUFFICIENT_TO_READ_AGAIN == 0.69
    assert (evidence_sufficiency.reads_again(covered), evidence_sufficiency.reads_again(thin)) == (True, False)
    assert (nothing.score, evidence_sufficiency.reads_again(nothing)) == (None, False)


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
