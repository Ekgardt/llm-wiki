"""A selected piece brings its whole entry; a count fans out before it is final.

A session is one daily entry cut into pieces of at most 4 096 bytes, and
retrieval ranks the pieces. Until 2026-09-08 the model read the piece that
matched while the sentence it needed sat in the next piece of the same
session; every wrong multi-session count of run 1 was one instance short.
Now every piece of a selected entry goes into the window, in byte order,
where the entry first ranked. And when the answer counted or summed, the
question is fanned out into a few concrete sub-queries, what they find joins
the candidates, and the answer is generated once more under a rule to list
every instance before counting.
See `docs/research/2026-09-08-whole-entries-and-a-fan-out-for-counts.md`.
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

import aggregation_pass  # noqa: E402
from context_budget import ContextBudget  # noqa: E402
from corpus_snapshot import collect_corpus  # noqa: E402
from longmemeval_vault import daily_block  # noqa: E402
from query_memory import (  # noqa: E402
    QA_MAX_CANDIDATES,
    WHOLE_ENTRIES_ENV,
    build_grounded_context,
    grounded_qa,
)

DAILY = "knowledge/daily/2023-05-01.md"


def _long_session(word: str, lines: int = 120) -> str:
    return "\n".join(
        f"- user: On day {index} I went to the {word} festival and we talked for a long while."
        for index in range(lines)
    )


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "knowledge" / "daily").mkdir(parents=True)
    (root / "knowledge" / "notes").mkdir(parents=True)
    body = (
        "# 2023-05-01\n\n"
        + daily_block("sess_a", "2023/05/01 (Mon) 10:00", _long_session("Austin"))
        + "\n"
        + daily_block("sess_b", "2023/05/01 (Mon) 12:00", _long_session("Portland", 40))
    )
    (root / DAILY).write_text(body, encoding="utf-8")
    return root


def _pieces(snapshot, session: str) -> list:
    return sorted(
        (chunk for chunk in snapshot.chunks if session in " ".join(chunk.heading_ancestry)),
        key=lambda chunk: chunk.byte_start,
    )


def _context(vault: Path, snapshot, candidates: tuple):
    return build_grounded_context(
        snapshot,
        candidates,
        vault=vault,
        profile="BASE",
        budget=ContextBudget(None, 122_880, 1200, 512),
    )


def _starts(context) -> list[int]:
    return [item.byte_start for item in context.evidence]


def _texts(context) -> str:
    return " ".join(item.text for item in context.evidence)


def test_a_middle_piece_brings_the_whole_session_in_byte_order(vault: Path, monkeypatch) -> None:
    monkeypatch.setenv(WHOLE_ENTRIES_ENV, "1")
    snapshot = collect_corpus(vault, code_roots=(), daily_paths=[DAILY])
    pieces = _pieces(snapshot, "sess_a")
    assert len(pieces) >= 3

    context = _context(vault, snapshot, (pieces[1],))

    assert _starts(context) == [piece.byte_start for piece in pieces]
    assert [item.citation_id for item in context.evidence][:2] == ["E1", "E2"]
    assert "Portland" not in _texts(context)


def test_entries_keep_retrieval_order_and_read_top_to_bottom(vault: Path, monkeypatch) -> None:
    monkeypatch.setenv(WHOLE_ENTRIES_ENV, "all")
    snapshot = collect_corpus(vault, code_roots=(), daily_paths=[DAILY])
    a_pieces, b_pieces = _pieces(snapshot, "sess_a"), _pieces(snapshot, "sess_b")

    context = _context(vault, snapshot, (b_pieces[-1], a_pieces[1]))

    assert _starts(context) == [piece.byte_start for piece in (*b_pieces, *a_pieces)]


def test_by_default_a_piece_of_plain_text_is_delivered_alone(vault: Path, monkeypatch) -> None:
    monkeypatch.delenv(WHOLE_ENTRIES_ENV, raising=False)
    snapshot = collect_corpus(vault, code_roots=(), daily_paths=[DAILY])
    pieces = _pieces(snapshot, "sess_a")

    context = _context(vault, snapshot, (pieces[1],))

    assert _starts(context) == [pieces[1].byte_start]


def test_a_budget_sheds_the_lowest_ranked_entry_first(vault: Path, monkeypatch) -> None:
    monkeypatch.setenv(WHOLE_ENTRIES_ENV, "all")
    snapshot = collect_corpus(vault, code_roots=(), daily_paths=[DAILY])
    a_pieces, b_pieces = _pieces(snapshot, "sess_a"), _pieces(snapshot, "sess_b")
    budget = ContextBudget(None, 14_000, 500, 200)

    context = build_grounded_context(
        snapshot, (a_pieces[0], b_pieces[0]), vault=vault, profile="BASE", budget=budget
    )

    kept = set(_starts(context))
    assert a_pieces[0].byte_start in kept
    assert kept < {piece.byte_start for piece in (*a_pieces, *b_pieces)}


# --- the fan-out ---------------------------------------------------------


def _count_answer(prompt: str, text: str, inputs: list[str]) -> str:
    marker = "<evidence_manifest>\n"
    manifest = prompt.split(marker, 1)[1].split("\n</evidence_manifest>", 1)[0]
    evidence = json.loads(manifest)
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


def _write_note(vault: Path, name: str, body: str) -> None:
    (vault / "knowledge" / "notes" / name).write_text(
        "---\ntype: concept\nsource_authority: user\nconfidence: high\n---\n\n"
        f"# {name}\n\n{body}\n",
        encoding="utf-8",
    )


class _Stand:
    """Two pages retrieval finds, a third only a sub-query finds, a scripted model."""

    def __init__(self, vault: Path) -> None:
        _write_note(vault, "alpha.md", "I attended the Austin Film Festival festival in March.")
        _write_note(vault, "beta.md", "The Portland Film Festival festival was in April.")
        _write_note(vault, "gamma.md", "Tribeca screenings festival, a documentary week in May.")
        self.snapshot = collect_corpus(vault)
        self.chunks = {
            name: next(c for c in self.snapshot.chunks if c.source_path.endswith(f"{name}.md"))
            for name in ("alpha", "beta", "gamma")
        }
        self.queries: list[tuple[str, int]] = []
        self.fanout_prompts: list[str] = []
        self.answer_prompts: list[str] = []

    def retrieve(self, limit: int) -> tuple:
        return (self.chunks["alpha"], self.chunks["beta"])

    def search(self, query: str, limit: int) -> tuple:
        self.queries.append((query, limit))
        return (self.chunks["gamma"], self.chunks["alpha"])

    def generate(self, prompt: str, system_prompt: str, max_tokens: int) -> str:
        if system_prompt == aggregation_pass.FANOUT_SYSTEM_PROMPT:
            self.fanout_prompts.append(prompt)
            return '{"queries": ["film festival attended", "documentary screenings", "film festival attended"]}'
        if system_prompt == aggregation_pass.CLUSTER_SYSTEM_PROMPT:
            return '{"groups": [[0], [1]]}'
        self.answer_prompts.append(prompt)
        if len(self.answer_prompts) == 1:
            return _count_answer(
                prompt,
                "Two: the Austin Film Festival and the Portland Film Festival.",
                ["Austin Film Festival", "Portland Film Festival"],
            )
        return _count_answer(
            prompt, "Three: Austin, Portland and the Tribeca festival.", ["Austin", "Portland", "Tribeca"]
        )


def test_a_count_fans_out_and_is_answered_again_under_the_counting_rule(vault: Path) -> None:
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

    # Step one found Tribeca, so step two fanned out again, found nothing new
    # and stopped without another answer: two fan-outs, two answers.
    assert [query for query, _ in stand.queries] == ["film festival attended", "documentary screenings"] * 2
    assert all(limit == QA_MAX_CANDIDATES for _, limit in stand.queries)
    assert len(stand.fanout_prompts) == 2
    assert "- Austin Film Festival" in stand.fanout_prompts[0]
    assert len(stand.answer_prompts) == 2
    assert "gamma.md" in stand.answer_prompts[1]
    assert "<counting_rule>" in stand.answer_prompts[1]
    assert "<counting_rule>" not in stand.answer_prompts[0]
    assert document["claims"][0]["text"].startswith("Three:")


def test_fixed_candidates_cannot_fan_out(vault: Path) -> None:
    stand = _Stand(vault)

    grounded_qa(
        "How many film festivals did I attend?",
        vault=vault,
        snapshot=stand.snapshot,
        candidates=stand.retrieve(QA_MAX_CANDIDATES),
        generator=stand.generate,
        profile="BASE",
    )

    assert stand.queries == []
    assert stand.fanout_prompts == []


def test_a_reply_that_is_not_queries_fans_out_to_nothing() -> None:
    assert aggregation_pass.fan_out_queries("How many?", ["a"], lambda prompt: "no json here") == []
    assert aggregation_pass.fan_out_queries("How many?", [], lambda prompt: '{"queries": "x"}') == []
    replies = '{"queries": ["How many?", " ", "one", "one", 3, "two"]}'
    assert aggregation_pass.fan_out_queries("How many?", [], lambda prompt: replies) == ["one", "two"]


def test_at_most_five_queries_are_run() -> None:
    reply = json.dumps({"queries": [f"query {index}" for index in range(9)]})
    assert len(aggregation_pass.fan_out_queries("How many?", [], lambda prompt: reply)) == 5


def test_a_piece_more_searches_agree_on_moves_ahead_of_a_piece_one_search_found() -> None:
    from query_memory import _merged

    first = ({"id": "a"}, {"id": "b"}, {"id": "drum"})
    widened = ({"id": "a"}, {"id": "b"}, {"id": "drum"}, {"id": "w"})
    gathered = ({"id": "drum"}, {"id": "g"}, {"id": "a"})

    rows = _merged(first, widened, gathered)

    assert [row["id"] for row in rows] == ["a", "drum", "b", "w", "g"]
    assert _merged(first, None, ({"id": "a"},)) is None


def test_the_second_pass_reads_the_cited_spans_and_the_new_pieces_only(vault: Path) -> None:
    stand = _Stand(vault)

    grounded_qa(
        "How many film festivals did I attend?",
        vault=vault,
        snapshot=stand.snapshot,
        retrieve=stand.retrieve,
        search=stand.search,
        generator=stand.generate,
        profile="BASE",
    )

    first, second = stand.answer_prompts
    assert first.count("relative_path") == 2
    # The cited alpha and beta, and the new gamma; nothing read twice for nothing.
    assert second.count("relative_path") == 3
    assert "gamma.md" in second


def test_the_count_loop_stops_when_a_step_adds_no_instance(vault: Path) -> None:
    from query_memory import MAX_COUNT_STEPS

    stand = _Stand(vault)
    # Every answer after the first names the same two festivals: nothing new.
    stand.generate = _same_count_every_time(stand)

    grounded_qa(
        "How many film festivals did I attend?",
        vault=vault,
        snapshot=stand.snapshot,
        retrieve=stand.retrieve,
        search=stand.search,
        generator=stand.generate,
        profile="BASE",
    )

    assert len(stand.answer_prompts) == 2
    assert len(stand.fanout_prompts) == 1 < MAX_COUNT_STEPS


def _same_count_every_time(stand: _Stand):
    def generate(prompt: str, system_prompt: str, max_tokens: int) -> str:
        if system_prompt == aggregation_pass.FANOUT_SYSTEM_PROMPT:
            stand.fanout_prompts.append(prompt)
            return '{"queries": ["film festival attended"]}'
        if system_prompt == aggregation_pass.CLUSTER_SYSTEM_PROMPT:
            return '{"groups": [[0], [1]]}'
        stand.answer_prompts.append(prompt)
        return _count_answer(
            prompt,
            "Two: the Austin Film Festival and the Portland Film Festival.",
            ["Austin Film Festival", "Portland Film Festival"],
        )

    return generate
