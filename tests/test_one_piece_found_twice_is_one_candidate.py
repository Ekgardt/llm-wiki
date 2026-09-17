"""A piece two searches found leads the pieces one search found, whatever shape its row has.

Third audit, 2026-09-17: a retrieval row was keyed by its id and a row without
an id by its position, so the same piece from two legs never earned its second
vote. See `docs/research/2026-09-17-one-piece-found-twice-is-one-candidate.md`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import refusal_pass  # noqa: E402
from corpus_snapshot import collect_corpus  # noqa: E402
from query_memory import grounded_qa  # noqa: E402

NOTES = {
    "racket.md": "I have been practising with my new tennis racket all week.",
    "shop.md": "I bought the tennis racket at the sports store downtown on Monday.",
    "club.md": "The tennis club downtown opens at nine on weekdays.",
}
REFUSAL = json.dumps(
    {
        "schema_version": "grounded-answer/v1",
        "status": "insufficient_evidence",
        "claims": [],
        "citations": [],
        "reason": "No span states where the racket was bought.",
    }
)


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "knowledge" / "notes").mkdir(parents=True)
    (root / "knowledge" / "daily").mkdir(parents=True)
    for name, body in NOTES.items():
        (root / "knowledge" / "notes" / name).write_text(
            f"---\ntype: concept\nsource_authority: user\nconfidence: high\n---\n\n# {name}\n\n{body}\n",
            encoding="utf-8",
        )
    return root


def _by_position(chunk) -> dict:
    """The same piece as a leg without ids returns it."""
    return {"path": chunk.source_path, "byte_start": chunk.byte_start, "byte_end": chunk.byte_end}


def _manifest_paths(prompt: str) -> list[str]:
    manifest = prompt.split("<evidence_manifest>\n", 1)[1].split("\n</evidence_manifest>", 1)[0]
    return [item["relative_path"].rsplit("/", 1)[1] for item in json.loads(manifest)]


def test_a_piece_the_second_search_found_again_by_position_moves_ahead(vault: Path) -> None:
    snapshot = collect_corpus(vault)
    chunk = {name: next(c for c in snapshot.chunks if c.source_path.endswith(name)) for name in NOTES}
    prompts: list[str] = []

    def generate(prompt: str, system_prompt: str, max_tokens: int) -> str:
        if system_prompt == refusal_pass.MISSING_SYSTEM_PROMPT:
            return '{"queries": ["tennis racket bought store"]}'
        prompts.append(prompt)
        return REFUSAL

    grounded_qa(
        "Where did I buy my new tennis racket?",
        vault=vault,
        snapshot=snapshot,
        retrieve=lambda limit: (chunk["racket.md"], chunk["shop.md"]),
        search=lambda query, limit: (_by_position(chunk["shop.md"]), chunk["club.md"]),
        generator=generate,
        profile="BASE",
    )

    assert [_manifest_paths(prompt) for prompt in prompts] == [
        ["racket.md", "shop.md"],
        ["shop.md", "racket.md", "club.md"],
    ]
