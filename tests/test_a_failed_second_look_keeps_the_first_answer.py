"""A second look that fails leaves the verified first answer standing.

Third audit, 2026-09-17: a regenerated reply that did not parse, failed the
schema, or ran into the deadline left `grounded_qa` as an exception, and the
first answer — already verified and cited — was lost. Every look now goes
through one guard.
See `docs/research/2026-09-17-a-failed-second-look-keeps-the-first-answer.md`.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import aggregation_pass  # noqa: E402
import refusal_pass  # noqa: E402
from corpus_snapshot import collect_corpus  # noqa: E402
from query_memory import grounded_qa  # noqa: E402

HELPER_REPLIES = {
    refusal_pass.MISSING_SYSTEM_PROMPT: '{"queries": ["where the event was held"]}',
    aggregation_pass.CLUSTER_SYSTEM_PROMPT: '{"groups": [[0, 1]]}',
}
ASKED = "\n(Current date: 2022/04/04 (Mon) 10:00)"


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
    _write_note(root, "events.md", "I attended the networking event on 2022-03-09 downtown.")
    _write_note(root, "venue.md", "The networking event was held at the old library hall.")
    return root


def _citation(item: dict) -> dict:
    return {key: value for key, value in item.items() if key != "text"}


def _reply(prompt: str, claims: list[dict], spans: int) -> str:
    """An answer whose every claim cites the first `spans` pieces of evidence."""
    manifest = prompt.split("<evidence_manifest>\n", 1)[1].split("\n</evidence_manifest>", 1)[0]
    evidence = json.loads(manifest)[:spans]
    ids = [item["citation_id"] for item in evidence]
    return json.dumps(
        {
            "schema_version": "grounded-answer/v1",
            "status": "answered",
            "claims": [{**claim, "citation_ids": ids} for claim in claims],
            "citations": [_citation(item) for item in evidence],
            "reason": None,
        }
    )


class _Model:
    """The first answer is scripted; the second pass does whatever the test says."""

    def __init__(self, claims: list[dict], second: Callable[[], str], spans: int = 1) -> None:
        self.claims = claims
        self.spans = spans
        self.second = second
        self.answers = 0

    def __call__(self, prompt: str, system_prompt: str, max_tokens: int) -> str:
        if system_prompt in HELPER_REPLIES:
            return HELPER_REPLIES[system_prompt]
        self.answers += 1
        if self.answers == 1:
            return _reply(prompt, self.claims, self.spans)
        return self.second()


def _timed_out() -> str:
    raise TimeoutError("the provider ran into the deadline")


def _chunk(snapshot, name: str):
    return next(c for c in snapshot.chunks if c.source_path.endswith(name))


def test_a_second_search_whose_reply_is_prose_keeps_the_surviving_claim(vault: Path) -> None:
    snapshot = collect_corpus(vault)
    claims = [
        {"text": "You attended the networking event on 2022-03-09."},
        {"text": "Purchased at a sports store for forty dollars."},
    ]
    model = _Model(claims, lambda: "I could not find anything more, sorry.")

    document = grounded_qa(
        "When did I attend the networking event?",
        vault=vault,
        snapshot=snapshot,
        retrieve=lambda limit: (_chunk(snapshot, "events.md"),),
        search=lambda query, limit: (_chunk(snapshot, "venue.md"),),
        generator=model,
        profile="BASE",
    )

    texts = [claim["text"] for claim in document["claims"]]
    assert (model.answers, document["status"], texts) == (2, "answered", [claims[0]["text"]])


def test_a_calendar_pass_that_meets_the_deadline_keeps_the_dated_answer(vault: Path) -> None:
    snapshot = collect_corpus(vault)
    claim = {
        "text": "You attended the networking event on 2022-03-09.",
        "derivation": "difference",
        "inputs": ["2022-03-09"],
    }
    model = _Model([claim], _timed_out)

    document = grounded_qa(
        "How many days ago did I attend a networking event?" + ASKED,
        vault=vault,
        snapshot=snapshot,
        candidates=(_chunk(snapshot, "events.md"), _chunk(snapshot, "venue.md")),
        generator=model,
        profile="BASE",
    )

    assert (model.answers, document["status"]) == (2, "answered")
    assert document["claims"][0]["text"] == claim["text"]


def test_a_count_step_whose_reply_fails_the_schema_keeps_the_first_count(vault: Path) -> None:
    snapshot = collect_corpus(vault)
    claim = {
        "text": "One: the networking event on 2022-03-09.",
        "derivation": "count",
        "inputs": ["networking event", "the networking event downtown"],
    }
    model = _Model([claim], lambda: '{"status": "answered"}', spans=2)

    document = grounded_qa(
        "How many networking events did I attend?",
        vault=vault,
        snapshot=snapshot,
        candidates=(_chunk(snapshot, "events.md"), _chunk(snapshot, "venue.md")),
        generator=model,
        profile="BASE",
    )

    assert (model.answers, document["status"]) == (2, "answered")
    assert document["claims"][0]["text"] == claim["text"]
