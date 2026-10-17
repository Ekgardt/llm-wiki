"""An append keeps only the bytes it added, and still undoes and recovers exactly.

On the live vault 7 026 appends of one day held 2.58 GB of before/after images for the
9.67 MB they added (2026-09-27). See
`docs/research/2026-09-28-an-append-keeps-only-what-it-added.md`.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import markdown_transaction
import pytest
from markdown_transaction import MarkdownChange, MarkdownCoordinator
from reliable_memory import validate_schema

from tests.slow_machine import LONG_TIMEOUT

PAGE = "knowledge/daily/2026-09-27.md"
TAIL = b"\n## [12:00:00] capture | session\n- one more line\n"


def _hard_to_compress(size: int) -> bytes:
    """Text a compressor cannot shrink much, so a full image is visibly large."""
    blocks, seed = [], b"seed"
    while sum(map(len, blocks)) < size:
        seed = hashlib.sha256(seed).hexdigest().encode()
        blocks.append(seed + b"\n")
    return b"".join(blocks)[:size]


BEFORE = _hard_to_compress(256 * 1024)


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "knowledge/daily").mkdir(parents=True)
    (root / PAGE).write_bytes(BEFORE)
    return root


@pytest.fixture
def state_root(tmp_path: Path) -> Path:
    return tmp_path / "state"


def _append(vault: Path, state_root: Path) -> tuple[MarkdownCoordinator, str]:
    coordinator = MarkdownCoordinator(vault, state_root)
    record = coordinator.prepare([MarkdownChange.replace(PAGE, BEFORE + TAIL)], operation_id="append")
    coordinator.apply(record.id)
    return coordinator, record.id


def _image_sizes(state_root: Path, transaction_id: str) -> dict[str, int]:
    root = state_root / "run/transactions" / transaction_id
    return {path.relative_to(root).as_posix(): path.stat().st_size for path in root.glob("*/*.bin")}


def _plan_states(state_root: Path, transaction_id: str) -> tuple[object, object]:
    plan = json.loads((state_root / "run/transactions" / transaction_id / "plan.json").read_bytes())
    operation = plan["operations"][0]
    return set(operation["before"]), set(operation["after"])


def test_an_append_stores_only_the_bytes_it_added(vault: Path, state_root: Path) -> None:
    _coordinator, transaction_id = _append(vault, state_root)
    sizes = _image_sizes(state_root, transaction_id)
    assert (list(sizes), sizes["after/000000.bin"] < len(TAIL) + 64) == (["after/000000.bin"], True)
    assert _plan_states(state_root, transaction_id) == (
        {"sha256", "length"},
        {"sha256", "base_length", "suffix", "suffix_sha256"},
    )
    assert (vault / PAGE).read_bytes() == BEFORE + TAIL


def test_an_undone_append_restores_the_file_exactly(vault: Path, state_root: Path) -> None:
    coordinator, transaction_id = _append(vault, state_root)
    undo = coordinator.undo(transaction_id)
    coordinator.apply(undo.id)
    assert (vault / PAGE).read_bytes() == BEFORE


def test_a_replace_that_is_not_an_append_keeps_both_images(vault: Path, state_root: Path) -> None:
    coordinator = MarkdownCoordinator(vault, state_root)
    record = coordinator.prepare([MarkdownChange.replace(PAGE, b"rewritten\n")], operation_id="rewrite")
    coordinator.apply(record.id)
    assert sorted(_image_sizes(state_root, record.id)) == ["after/000000.bin", "before/000000.bin"]


def test_a_v1_plan_of_full_images_is_still_read_by_its_own_schema() -> None:
    digest = "0" * 64
    state = {"sha256": digest, "artifact": "before/000000.bin"}
    plan = {
        "schema_version": "markdown-transaction/v1",
        "transaction_id": "old",
        "operations": [{"kind": "replace", "path": PAGE, "before": state, "after": dict(state, artifact="after/000000.bin")}],
    }
    validate_schema(plan, markdown_transaction._plan_schema(plan))


def _crash(vault: Path, state_root: Path, killpoint: str, code: str, *arguments: str) -> int:
    scripts = Path(__file__).parents[1] / "scripts"
    env = os.environ | {"LLM_WIKI_TRANSACTION_KILLPOINT": killpoint, "PYTHONPATH": str(scripts)}
    return subprocess.run(
        [sys.executable, "-c", code, str(vault), str(state_root), *arguments],
        check=False, capture_output=True, text=True, env=env, timeout=LONG_TIMEOUT,
    ).returncode


_FORWARD = """
import sys
from pathlib import Path
from markdown_transaction import MarkdownChange, MarkdownCoordinator
c = MarkdownCoordinator(Path(sys.argv[1]), Path(sys.argv[2]))
page = Path(sys.argv[1], sys.argv[3])
tx = c.prepare([MarkdownChange.replace(sys.argv[3], page.read_bytes() + sys.argv[4].encode())], operation_id='crash')
c.apply(tx.id)
"""
_NOT_YET_WRITTEN = {"after_preparing", "after_images_fsynced"}


@pytest.mark.parametrize(
    "killpoint",
    ["after_preparing", "after_images_fsynced", "after_prepared", "after_applying", "after_each_target", "before_commit", "after_commit"],
)
def test_an_append_recovers_at_every_forward_crash_boundary(vault: Path, state_root: Path, killpoint: str) -> None:
    assert _crash(vault, state_root, killpoint, _FORWARD, PAGE, TAIL.decode()) == 86
    MarkdownCoordinator(vault, state_root).recover()
    expected = BEFORE if killpoint in _NOT_YET_WRITTEN else BEFORE + TAIL
    assert (vault / PAGE).read_bytes() == expected


_UNDO = """
import sys
from pathlib import Path
from markdown_transaction import MarkdownCoordinator
c = MarkdownCoordinator(Path(sys.argv[1]), Path(sys.argv[2]))
undo = c.undo(sys.argv[3])
c.apply(undo.id)
"""
_UNDO_NOT_YET_WRITTEN = {"after_undo_preparing", "after_undo_images_fsynced"}


@pytest.mark.parametrize(
    "killpoint",
    ["after_undo_preparing", "after_undo_images_fsynced", "after_undo_prepared", "after_undo_applying",
     "after_each_undo_target", "before_undo_commit", "after_undo_commit"],
)
def test_an_undone_append_recovers_at_every_crash_boundary(vault: Path, state_root: Path, killpoint: str) -> None:
    _coordinator, transaction_id = _append(vault, state_root)
    assert _crash(vault, state_root, killpoint, _UNDO, transaction_id) == 86
    MarkdownCoordinator(vault, state_root).recover()
    expected = BEFORE + TAIL if killpoint in _UNDO_NOT_YET_WRITTEN else BEFORE
    assert (vault / PAGE).read_bytes() == expected


def test_legacy_writer_records_its_process_identity(vault: Path, state_root: Path) -> None:
    from operational_ownership import current_process_identity

    coordinator = MarkdownCoordinator(vault, state_root)
    identity = current_process_identity()
    with coordinator.writer_gate():
        with sqlite3.connect(coordinator.database_path) as database:
            row = database.execute(
                "SELECT process_id, process_start_identity FROM writer_owners"
            ).fetchone()
        assert row == (identity.pid, identity.start_identity)


def test_old_writer_rows_are_not_given_an_invented_identity() -> None:
    with sqlite3.connect(":memory:") as database:
        database.row_factory = sqlite3.Row
        database.execute("CREATE TABLE writer_owners (process_id INTEGER)")
        database.execute("INSERT INTO writer_owners VALUES (123)")
        markdown_transaction._add_writer_owner_columns(database)
        markdown_transaction._add_writer_owner_columns(database)
        row = database.execute("SELECT * FROM writer_owners").fetchone()
        assert row["process_id"] == 123
        assert row["process_start_identity"] is None


def test_unavailable_writer_identity_creates_no_owner(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from operational_ownership import OperationalOwnershipError

    coordinator = MarkdownCoordinator(vault, state_root)

    def unavailable():
        raise OperationalOwnershipError("identity_unavailable", "cannot identify this process")

    monkeypatch.setattr("operational_ownership.current_process_identity", unavailable)
    with pytest.raises(OperationalOwnershipError, match="cannot identify"):
        with coordinator.writer_gate(wait_seconds=0):
            pytest.fail("a writer without an identity entered the gate")
    with sqlite3.connect(coordinator.database_path) as database:
        assert database.execute("SELECT count(*) FROM writer_owners").fetchone()[0] == 0
        assert database.execute("SELECT count(*) FROM writer_fences").fetchone()[0] == 0
