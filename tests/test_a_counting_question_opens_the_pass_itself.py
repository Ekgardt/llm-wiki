"""A counting question opens the counting pass by its own shape, and the count counts things.

Measured over the 500 recorded rows of the 2026-09-18 run: the pass used to
open only when the first answer tagged a claim `count` or `sum`, which
happened on 79 rows of 500 and on 74 of 133 multi-session rows. 18 of the 30
multi-session failures never opened it, among them "How many points do I need
to earn" (said 300, gold 100), "the total number of online courses" (12
against 20) and "the total number of siblings" (1 against 4) — three answers
generated once, with no derivation named, so the safety net was never hung.

`asks_to_aggregate` reads the question instead of asking a model what kind of
question it is, so the second door costs no token. It joins the older signal
rather than replacing it, and yields to the calendar, which owns "how many
days ago".

The count then counts things, not mentions: the unit is read from the question
by `counted_kind`, the clustering call is asked in that unit, mentions of one
thing are blocked together so they share a call, and the counting rule names
it. A deterministic merge on the same key was measured and rejected — it fixes
one wrong count and breaks three right ones.
See `docs/research/2026-09-19-the-aggregation-pass-is-chosen-by-the-question.md`.
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
from query_memory import grounded_qa  # noqa: E402

# --- the trigger --------------------------------------------------------------

# The four the diagnosis of 2026-09-19 names, with the unit each one counts.
NAMED = (
    ("3a704032", "How many plants did I acquire in the last month?", "plant"),
    (
        "9ee3ecd6",
        "How many points do I need to earn to redeem a free skincare product at Sephora?",
        "point",
    ),
    ("67e0d0f2", "What is the total number of online courses I've completed?", "course"),
    ("8e91e7d9", "What is the total number of siblings I have?", "sibling"),
)
# Counting and summing questions taken verbatim from other recorded rows.
SAMPLE = (
    "How many weddings have I attended in this year?",
    "How much money did I raise for charity in total?",
    "How many bikes did I service or plan to service in March?",
    "What is the total amount I spent on luxury items in the past few months?",
    "What is the average age of me, my parents, and my grandparents?",
    "How many different types of citrus fruits have I used in my cocktail recipes?",
    "How many hours in total did I spend driving to my three road trip destinations combined?",
    "What is the total distance I covered in my four road trips?",
)
# Plain lookups: one fact, no derivation, nothing to aggregate.
LOOKUPS = (
    "What degree did I graduate with?",
    "Where did I order on Monday?",
    "What did the doctor say about my knee?",
    "When did I move into the new apartment?",
    "Which airline did I fly with in March?",
)


def test_the_four_questions_the_diagnosis_named_open_the_pass() -> None:
    read = tuple(
        (name, aggregation_pass.asks_to_aggregate(question), aggregation_pass.counted_kind(question))
        for name, question, _ in NAMED
    )

    assert read == tuple((name, True, kind) for name, _, kind in NAMED)


def test_a_representative_sample_of_counting_questions_opens_the_pass() -> None:
    assert [q for q in SAMPLE if not aggregation_pass.asks_to_aggregate(q)] == []


def test_a_plain_lookup_question_leaves_the_pass_shut() -> None:
    assert [q for q in LOOKUPS if aggregation_pass.asks_to_aggregate(q)] == []


def test_the_counting_rule_names_the_thing_being_counted() -> None:
    named = aggregation_pass.counting_rule("How many bikes did I service in March?")
    plain = aggregation_pass.counting_rule("What did the doctor say about my knee?")

    assert "Count bikes, not mentions or events" in named
    assert "not mentions or events" not in plain
    assert plain.startswith("<counting_rule>\n")


# --- what a count counts ---------------------------------------------------------

BIKES = (
    "A road bike serviced at Pedal Power on March 10",
    "Commuter bike front tire replaced before April",
    "The road bike chain cleaned and lubricated on March 2",
)


def _replies(groups: str):
    def cluster(prompt: str) -> str:
        cluster.prompts.append(prompt)
        return groups

    cluster.prompts = []  # type: ignore[attr-defined]
    return cluster


def test_two_mentions_of_one_thing_collapse_into_one() -> None:
    cluster = _replies('{"groups": [[0], [1, 2]]}')

    groups = aggregation_pass.duplicate_groups(BIKES, cluster, "bike")

    assert groups == [[BIKES[0], BIKES[2]]]


def test_two_different_things_are_never_collapsed() -> None:
    cluster = _replies('{"groups": [[0], [1], [2]]}')

    groups = aggregation_pass.duplicate_groups(BIKES, cluster, "bike")

    assert groups == []


def test_mentions_of_one_thing_share_a_clustering_call() -> None:
    """Blocking, not merging: the key decides the order, the model decides the groups."""
    cluster = _replies('{"groups": []}')

    aggregation_pass.duplicate_groups(BIKES, cluster, "bike")
    lines = cluster.prompts[0].splitlines()  # type: ignore[attr-defined]

    assert lines[0] == "Each group must name one and the same bike."
    # Plain alphabetical order would have put the commuter bike between the two
    # road bikes, because one of them opens "A" and the other "The".
    assert [line.split(": ", 1)[1] for line in lines[1:]] == [BIKES[1], BIKES[0], BIKES[2]]


def test_the_note_beside_the_question_names_the_unit() -> None:
    note = aggregation_pass.entity_note([["A road bike", "The road bike"]], "bike")

    assert "name one and the same bike" in note
    assert "name one and the same thing" in aggregation_pass.entity_note([["a", "b"]])


# --- the whole path ---------------------------------------------------------------


def _write_note(vault: Path, name: str, body: str) -> None:
    (vault / "knowledge" / "notes" / name).write_text(
        "---\ntype: concept\nsource_authority: user\nconfidence: high\n---\n\n"
        f"# {name}\n\n{body}\n",
        encoding="utf-8",
        newline="",
    )


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    for name in ("notes", "projects", "daily"):
        (root / "knowledge" / name).mkdir(parents=True)
    return root


def _manifest(prompt: str) -> list[dict]:
    body = prompt.split("<evidence_manifest>\n", 1)[1].split("\n</evidence_manifest>", 1)[0]
    return json.loads(body)


def _derivation(derivation: str | None) -> dict:
    if not derivation:
        return {}
    return {"derivation": derivation, "inputs": ["Austin", "Portland"]}


def _citations(evidence: list[dict]) -> list[dict]:
    return [{key: item[key] for key in item if key != "text"} for item in evidence]


def _answer(prompt: str, text: str, derivation: str | None) -> str:
    evidence = _manifest(prompt)
    claim = {"text": text, "citation_ids": [item["citation_id"] for item in evidence]}
    claim.update(_derivation(derivation))
    return json.dumps(
        {
            "schema_version": "grounded-answer/v1",
            "status": "answered",
            "claims": [claim],
            "citations": _citations(evidence),
            "reason": None,
        }
    )


HELPER_REPLIES = {
    aggregation_pass.FANOUT_SYSTEM_PROMPT: '{"queries": ["documentary screenings"]}',
    aggregation_pass.CLUSTER_SYSTEM_PROMPT: '{"groups": [[0], [1]]}',
}


class _Stand:
    """A first answer that declares nothing, and a third page only a sub-query finds."""

    def __init__(self, vault: Path) -> None:
        _write_note(vault, "alpha.md", "I attended the Austin Film Festival festival in March.")
        _write_note(vault, "beta.md", "The Portland Film Festival festival was in April.")
        _write_note(vault, "gamma.md", "Tribeca screenings festival, a documentary week in May.")
        self.snapshot = collect_corpus(vault)
        self.chunks = {
            name: next(c for c in self.snapshot.chunks if c.source_path.endswith(f"{name}.md"))
            for name in ("alpha", "beta", "gamma")
        }
        self.answer_prompts: list[str] = []

    def retrieve(self, limit: int) -> tuple:
        return (self.chunks["alpha"], self.chunks["beta"])

    def search(self, query: str, limit: int) -> tuple:
        return (self.chunks["gamma"], self.chunks["alpha"])

    def generate(self, prompt: str, system_prompt: str, max_tokens: int) -> str:
        canned = HELPER_REPLIES.get(system_prompt)
        if canned is not None:
            return canned
        self.answer_prompts.append(prompt)
        return self._answered(prompt)

    def _answered(self, prompt: str) -> str:
        if len(self.answer_prompts) == 1:
            return _answer(prompt, "I went to the Austin and Portland festivals.", None)
        return _answer(prompt, "Three festivals: Austin, Portland and Tribeca.", "count")


def test_a_countable_question_is_answered_again_though_the_reader_declared_nothing(
    vault: Path,
) -> None:
    stand = _Stand(vault)

    document = grounded_qa(
        "How many film festivals did I attend?",
        vault=vault,
        snapshot=stand.snapshot,
        retrieve=stand.retrieve,
        search=stand.search,
        generator=stand.generate,
        profile="BASE",
    )

    assert len(stand.answer_prompts) > 1
    assert "Count festivals, not mentions or events" in stand.answer_prompts[1]
    assert "gamma.md" in stand.answer_prompts[1]
    assert document["claims"][0]["text"].startswith("Three festivals")


def _difference_answer(prompt: str) -> str:
    evidence = _manifest(prompt)[0]
    return json.dumps(
        {
            "schema_version": "grounded-answer/v1",
            "status": "answered",
            "claims": [
                {
                    "text": "You attended it on 2022-03-09.",
                    "citation_ids": [evidence["citation_id"]],
                    "derivation": "difference",
                    "inputs": ["2022-03-09"],
                }
            ],
            "citations": [{key: evidence[key] for key in evidence if key != "text"}],
            "reason": None,
        }
    )


def test_a_date_question_stays_with_the_calendar(vault: Path) -> None:
    """"How many days ago" fires the trigger's words and must not spend the regeneration."""
    stand = _Stand(vault)
    seen: list[str] = []

    def generate(prompt: str, system_prompt: str, max_tokens: int) -> str:
        seen.append(system_prompt)
        return _difference_answer(prompt)

    grounded_qa(
        "How many days ago did I attend a film festival?\n(Current date: 2022/04/04 (Mon) 10:00)",
        vault=vault,
        snapshot=stand.snapshot,
        retrieve=stand.retrieve,
        search=stand.search,
        generator=generate,
        profile="BASE",
    )

    assert aggregation_pass.asks_to_aggregate("How many days ago did I attend a film festival?")
    assert aggregation_pass.FANOUT_SYSTEM_PROMPT not in seen
    assert aggregation_pass.CLUSTER_SYSTEM_PROMPT not in seen
