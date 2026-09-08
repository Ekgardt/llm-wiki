"""The model reads before it claims, and the reading never reaches a reader.

LongMemEval's authors found that a reader which first notes what each
passage says and then answers in JSON gains up to ten points. Our schema was
closed, so the note had nowhere to go. Now `working` is an optional field
written first, and `grounded_qa` removes it with the other private keys.
See `docs/research/2026-09-08-working-before-the-claims.md`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from corpus_snapshot import collect_corpus  # noqa: E402
from query_memory import ANSWER_SCHEMA, _qa_system_prompt, grounded_qa  # noqa: E402
from reliable_memory import validate_schema  # noqa: E402


def _document(working: object) -> dict:
    return {
        "schema_version": "grounded-answer/v1",
        "status": "insufficient_evidence",
        "claims": [],
        "citations": [],
        "reason": "nothing",
        "working": working,
    }


def test_the_schema_takes_a_working_note_or_none() -> None:
    schema = json.loads(ANSWER_SCHEMA.read_text(encoding="utf-8"))

    validate_schema(_document("E1 says the class is on Fridays, 2023-06-30."), ANSWER_SCHEMA)
    validate_schema(_document(None), ANSWER_SCHEMA)
    assert list(schema["properties"]).index("working") < list(schema["properties"]).index("reason")


def test_the_prompt_asks_for_the_working_first() -> None:
    prompt = _qa_system_prompt()

    assert "Write working first" in prompt
    assert prompt.index("Write working first") < prompt.index("Output only JSON")


def _answer_with_working(prompt: str) -> str:
    manifest = prompt.split("<evidence_manifest>\n", 1)[1].split("\n</evidence_manifest>", 1)[0]
    item = json.loads(manifest)[0]
    return json.dumps(
        {
            "schema_version": "grounded-answer/v1",
            "status": "answered",
            "working": "E1: the class is on Fridays, said 2023-06-30.",
            "claims": [{"text": "The class is on Fridays.", "citation_ids": [item["citation_id"]]}],
            "citations": [{key: item[key] for key in item if key != "text"}],
            "reason": None,
        }
    )


def test_the_working_is_removed_before_a_reader_sees_the_answer(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / "knowledge" / "notes").mkdir(parents=True)
    (vault / "knowledge" / "daily").mkdir(parents=True)
    (vault / "knowledge" / "notes" / "class.md").write_text(
        "---\ntype: concept\nsource_authority: user\nconfidence: high\n---\n\n"
        "# Class\n\nI have a cocktail-making class on Fridays.\n",
        encoding="utf-8",
    )
    snapshot = collect_corpus(vault)

    document = grounded_qa(
        "What day is my class?",
        vault=vault,
        snapshot=snapshot,
        candidates=tuple(snapshot.chunks),
        generator=lambda prompt, system_prompt, max_tokens: _answer_with_working(prompt),
        profile="BASE",
    )

    assert document["status"] == "answered"
    assert "working" not in document
