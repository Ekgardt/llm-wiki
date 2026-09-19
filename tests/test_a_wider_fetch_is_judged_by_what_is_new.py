"""A wider fetch that brings one new page is used, however large the pool already is.

Third audit, 2026-09-17: the wider fetch was compared by length against a pool
that also holds what the dated leg found, and was dropped for not being longer.
See `docs/research/2026-09-17-one-piece-found-twice-is-one-candidate.md`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import aggregation_pass  # noqa: E402
from corpus_snapshot import collect_corpus  # noqa: E402
from query_memory import QA_MAX_CANDIDATES, grounded_qa  # noqa: E402

PAGES = {
    "alpha.md": "I ordered from Domino's on Monday.",
    "beta.md": "Domino's Pizza delivered again on Friday.",
    "gamma.md": "Pizza Hut on Sunday, from Domino's rival.",
    "dated.md": "On 2023-05-30 I ordered from Domino's Pizza once more.",
}
HELPERS = {
    aggregation_pass.CLUSTER_SYSTEM_PROMPT: '{"groups": [[0, 1]]}',
    aggregation_pass.FANOUT_SYSTEM_PROMPT: '{"queries": []}',
}


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "knowledge" / "notes").mkdir(parents=True)
    (root / "knowledge" / "daily").mkdir(parents=True)
    for name, body in PAGES.items():
        (root / "knowledge" / "notes" / name).write_text(
            f"---\ntype: concept\nsource_authority: user\nconfidence: high\n---\n\n# {name}\n\n{body}\n",
            encoding="utf-8",
        )
    return root


def _citation(item: dict) -> dict:
    return {key: value for key, value in item.items() if key != "text"}


def _count_of_everything(prompt: str) -> str:
    manifest = prompt.split("<evidence_manifest>\n", 1)[1].split("\n</evidence_manifest>", 1)[0]
    evidence = json.loads(manifest)
    claim = {
        "text": "Two places: Domino's and Domino's Pizza.",
        "citation_ids": [item["citation_id"] for item in evidence],
        "derivation": "count",
        "inputs": ["Domino's", "Domino's Pizza"],
    }
    return json.dumps(
        {
            "schema_version": "grounded-answer/v1",
            "status": "answered",
            "claims": [claim],
            "citations": [_citation(item) for item in evidence],
            "reason": None,
        }
    )


class _Stand:
    """Two pages retrieval finds, a third only when asked wider, a fourth only by date."""

    def __init__(self, vault: Path) -> None:
        self.snapshot = collect_corpus(vault)
        self.prompts: list[str] = []

    def chunk(self, name: str):
        return next(c for c in self.snapshot.chunks if c.source_path.endswith(name))

    def retrieve(self, limit: int) -> tuple:
        narrow = (self.chunk("alpha.md"), self.chunk("beta.md"))
        if limit <= QA_MAX_CANDIDATES:
            return narrow
        return (*narrow, self.chunk("gamma.md"))

    def search(self, query: str, limit: int, since=None, as_of=None) -> tuple:
        return (self.chunk("dated.md"),)

    def generate(self, prompt: str, system_prompt: str, max_tokens: int) -> str:
        if system_prompt in HELPERS:
            return HELPERS[system_prompt]
        self.prompts.append(prompt)
        return _count_of_everything(prompt)


def test_a_pool_the_dated_leg_already_grew_still_takes_the_new_page(vault: Path) -> None:
    stand = _Stand(vault)

    grounded_qa(
        "How many places did I order from yesterday?\n(Current date: 2023/05/31 (Wed) 10:00)",
        vault=vault,
        snapshot=stand.snapshot,
        retrieve=stand.retrieve,
        search=stand.search,
        generator=stand.generate,
        profile="BASE",
    )

    seen = [("dated.md" in prompt, "gamma.md" in prompt) for prompt in stand.prompts[:2]]
    assert seen == [(True, False), (True, True)]
