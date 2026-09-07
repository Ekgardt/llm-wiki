"""A refused append still owes its block, and only its block.

Measured on this vault on 2026-09-07: nine quarantined attempts stood open,
six of them tool breadcrumbs for one daily file — a whole-file replace whose
precondition a faster session broke. The block each carried is the delta
between the images the refusal kept, and it is owed until its marker is in
the file. A replace that changed anything but its tail is not an append and
is never replayed; a DLP refusal is a decision, not an accident.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import repair_refused_appends as repair  # noqa: E402
from markdown_transaction import _compressed_image  # noqa: E402
from reliable_memory import sha256_bytes  # noqa: E402

MARKER = "<!-- llm-wiki-operation:" + "a" * 64 + " -->"
BEFORE = b"# Daily\n\n## [10:00:00] first\n\nfirst block\n"
BLOCK = f"\n{MARKER}\n## [10:05:00] tool\n\nsecond block\n".encode()
IDENTITY = ("txn-1", "post-tool:abc")


def _transaction(directory: Path, before: bytes, after: bytes, kind: str = "replace") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "before").mkdir()
    (directory / "after").mkdir()
    (directory / "before/000000.bin").write_bytes(_compressed_image(before))
    (directory / "after/000000.bin").write_bytes(_compressed_image(after))
    operation = {
        "kind": kind,
        "path": "knowledge/daily/2026-09-05.md",
        "before": {"artifact": "before/000000.bin", "sha256": sha256_bytes(before)},
        "after": {"artifact": "after/000000.bin", "sha256": sha256_bytes(after)},
    }
    (directory / "plan.json").write_text(json.dumps({"operations": [operation]}))
    return directory


def _vault(tmp_path: Path, daily: bytes) -> Path:
    vault = tmp_path / "vault"
    (vault / "knowledge/daily").mkdir(parents=True)
    (vault / "knowledge/daily/2026-09-05.md").write_bytes(daily)
    return vault


def test_the_block_a_refused_append_carried_is_owed(tmp_path):
    directory = _transaction(tmp_path / "txn-1", BEFORE, BEFORE + BLOCK)
    vault = _vault(tmp_path, BEFORE + b"\nsomeone else's block\n")

    owed = repair._owed_in(directory, IDENTITY, vault)

    assert [(item.path, item.delta) for item in owed] == [
        ("knowledge/daily/2026-09-05.md", BLOCK)
    ]
    assert owed[0].operation_id == "post-tool:abc"


def test_a_block_whose_marker_is_already_in_the_file_is_not_owed(tmp_path):
    directory = _transaction(tmp_path / "txn-1", BEFORE, BEFORE + BLOCK)
    vault = _vault(tmp_path, BEFORE + b"\nother\n" + BLOCK)

    assert repair._owed_in(directory, IDENTITY, vault) == []


def test_a_replace_that_changed_more_than_its_tail_is_not_an_append(tmp_path):
    rewritten = BEFORE.replace(b"first block", b"edited block") + BLOCK
    directory = _transaction(tmp_path / "txn-1", BEFORE, rewritten)
    vault = _vault(tmp_path, BEFORE)

    assert repair._owed_in(directory, IDENTITY, vault) == []


def test_a_create_is_left_to_the_other_repair(tmp_path):
    directory = _transaction(tmp_path / "txn-1", b"", BLOCK, kind="create")
    vault = _vault(tmp_path, BEFORE)

    assert repair._owed_in(directory, IDENTITY, vault) == []


def test_an_image_that_no_longer_hashes_to_its_plan_is_refused(tmp_path):
    directory = _transaction(tmp_path / "txn-1", BEFORE, BEFORE + BLOCK)
    (directory / "after/000000.bin").write_bytes(_compressed_image(BEFORE + b"tampered"))
    vault = _vault(tmp_path, BEFORE)

    assert repair._owed_in(directory, IDENTITY, vault) == []


def test_only_a_race_is_replayable():
    assert repair.REPLAYABLE == "precondition_failed"


def test_a_block_without_a_marker_is_matched_by_its_bytes(tmp_path):
    plain = b"\n## [10:05:00] tool\n\nplain block\n"
    directory = _transaction(tmp_path / "txn-1", BEFORE, BEFORE + plain)

    assert repair._owed_in(directory, IDENTITY, _vault(tmp_path, BEFORE + plain)) == []
    assert len(repair._owed_in(directory, IDENTITY, _vault(tmp_path / "b", BEFORE))) == 1


def test_a_journal_append_is_left_to_the_journal(tmp_path):
    directory = _transaction(tmp_path / "txn-1", BEFORE, BEFORE + BLOCK)
    plan = json.loads((directory / "plan.json").read_text())
    plan["operations"][0]["path"] = "knowledge/projects/demo/journal.md"
    (directory / "plan.json").write_text(json.dumps(plan))

    assert repair._owed_in(directory, IDENTITY, _vault(tmp_path, BEFORE)) == []


def test_an_attempt_a_retry_already_closed_is_not_listed():
    import sqlite3

    database = sqlite3.connect(":memory:")
    database.execute(
        'CREATE TABLE "transaction" (id TEXT, operation_id TEXT, state TEXT, '
        "error_code TEXT, parent_transaction_id TEXT, created_at TEXT)"
    )
    database.executemany(
        'INSERT INTO "transaction" VALUES (?,?,?,?,?,?)',
        [
            ("refused", "post-tool:x", "quarantined", "precondition_failed", None, "1"),
            ("retry", "post-tool:x#2", "committed", None, "refused", "2"),
            ("lost", "post-tool:y", "quarantined", "precondition_failed", None, "3"),
        ],
    )

    assert repair._quarantined(database) == [("lost", "post-tool:y")]
