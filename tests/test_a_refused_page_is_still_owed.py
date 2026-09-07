"""A page a refused transaction wrote is restored from the trail, not rewritten.

A compile that plans several files applies them as one transaction. When a
shared target — `knowledge/log.md` — is appended to between plan and apply,
the precondition fails and the whole transaction is refused, including the
pages it had already produced. On this vault that lost two notes on
2026-09-02, both about the owner's own stated preferences.
"""

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import repair_refused_page_creation as repair  # noqa: E402
from markdown_transaction import _compressed_image  # noqa: E402
from reliable_memory import sha256_bytes  # noqa: E402

PAGE = b"---\ntype: decision\n---\n\n# A page that was owed\n"


def _transaction(directory: Path, operations: list[dict]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "plan.json").write_text(json.dumps({"operations": operations}))
    return directory


def _created(path: str, content: bytes, artifact: str) -> dict:
    return {
        "kind": "create",
        "path": path,
        "before": "absent",
        "after": {"artifact": artifact, "sha256": sha256_bytes(content)},
    }


def _with_image(directory: Path, artifact: str, content: bytes) -> None:
    target = directory / artifact
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_compressed_image(content))


def test_a_page_the_vault_does_not_have_is_owed(tmp_path):
    directory = _transaction(
        tmp_path / "txn",
        [_created("knowledge/notes/owed.md", PAGE, "after/000000.bin")],
    )
    _with_image(directory, "after/000000.bin", PAGE)

    owed = repair._owed_pages(directory, tmp_path / "vault")

    assert owed == [("knowledge/notes/owed.md", PAGE)]


def test_a_page_that_exists_today_is_left_alone(tmp_path):
    vault = tmp_path / "vault"
    (vault / "knowledge" / "notes").mkdir(parents=True)
    (vault / "knowledge" / "notes" / "owed.md").write_bytes(b"newer content")
    directory = _transaction(
        tmp_path / "txn",
        [_created("knowledge/notes/owed.md", PAGE, "after/000000.bin")],
    )
    _with_image(directory, "after/000000.bin", PAGE)

    assert repair._owed_pages(directory, vault) == []


def test_a_replace_is_never_restored(tmp_path):
    operation = _created("knowledge/log.md", PAGE, "after/000000.bin")
    operation["kind"] = "replace"
    directory = _transaction(tmp_path / "txn", [operation])
    _with_image(directory, "after/000000.bin", PAGE)

    assert repair._owed_pages(directory, tmp_path / "vault") == []


def test_an_image_that_no_longer_hashes_to_its_plan_is_refused(tmp_path):
    directory = _transaction(
        tmp_path / "txn",
        [_created("knowledge/notes/owed.md", PAGE, "after/000000.bin")],
    )
    _with_image(directory, "after/000000.bin", b"different bytes")

    assert repair._owed_pages(directory, tmp_path / "vault") == []


def test_a_path_outside_knowledge_is_refused(tmp_path):
    directory = _transaction(
        tmp_path / "txn", [_created("scripts/x.py", PAGE, "after/000000.bin")]
    )
    _with_image(directory, "after/000000.bin", PAGE)

    assert repair._owed_pages(directory, tmp_path / "vault") == []


def test_a_transaction_with_no_plan_owes_nothing(tmp_path):
    assert repair._owed_pages(tmp_path / "missing", tmp_path / "vault") == []


def test_only_a_race_is_replayable_never_a_dlp_refusal():
    """A DLP refusal is a decision the boundary made, not an accident."""
    database = sqlite3.connect(":memory:")
    database.execute(
        'CREATE TABLE "transaction" (id TEXT, state TEXT, error_code TEXT, '
        "created_at TEXT)"
    )
    database.executemany(
        'INSERT INTO "transaction" VALUES (?, ?, ?, ?)',
        [
            ("raced", "quarantined", "precondition_failed", "2026-09-02"),
            ("refused", "quarantined", "dlp_content_blocked", "2026-08-22"),
            ("done", "committed", None, "2026-09-03"),
        ],
    )

    assert repair._quarantined_ids(database) == ["raced"]
