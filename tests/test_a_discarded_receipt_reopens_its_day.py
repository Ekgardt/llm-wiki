"""Discarding a receipt makes its day pending again, and `--all` says it is idle.

Findings M-A5 and M-A4 of the third audit. See
`docs/research/2026-09-17-the-compile-decides-what-the-snapshot-already-knows.md`.
"""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

DAY = "2026-07-14.md"
BODY = b"## [10:00:00] session-end | manual\nA durable observation.\n"


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "vault"
    (root / "knowledge/daily/receipts").mkdir(parents=True)
    (root / "knowledge/daily" / DAY).write_bytes(BODY)

    import compile_memory
    import memory_state

    monkeypatch.setattr(compile_memory, "ROOT", root)
    monkeypatch.setattr(compile_memory, "DAILY_DIR", root / "knowledge/daily")
    monkeypatch.setattr(memory_state, "STATE_DIR", tmp_path / "run")
    monkeypatch.setattr(memory_state, "STATE_FILE", tmp_path / "run/state.json")
    return root


def _corrupt_receipt(root: Path) -> Path:
    """A receipt named for this day's one part, whose body cannot be read."""
    import compile_memory

    identity = compile_memory.compile_source_identity(
        f"knowledge/daily/{DAY}", compile_memory.sha256_bytes(BODY)
    )
    path = root / f"knowledge/daily/receipts/v3-{identity}.md"
    path.write_bytes(b"---\ntype: compile-receipt\n---\ntruncated\n")
    return path


def _mirror_says_compiled(root: Path) -> None:
    import compile_memory
    from memory_state import update_state

    digest = compile_memory.sha256_bytes(BODY)
    update_state(lambda state: state.update({"compiled_daily_hashes": {DAY: digest}}))
    del root


def test_a_discarded_receipt_takes_its_day_out_of_the_mirror(vault, capsys):
    """Without this the day stays "compiled" with no evidence, and is never redone."""
    import compile_memory
    from memory_state import load_state

    receipt = _corrupt_receipt(vault)
    _mirror_says_compiled(vault)

    discarded = compile_memory.discard_unusable_receipts()

    assert (discarded, receipt.exists()) == ([receipt.name], False)
    assert load_state()["compiled_daily_hashes"] == {}
    assert "1 day(s) are pending again" in capsys.readouterr().err


def test_the_reopened_day_is_offered_to_the_next_compile(vault, monkeypatch):
    import compile_memory
    from memory_state import load_state

    _corrupt_receipt(vault)
    _mirror_says_compiled(vault)
    monkeypatch.setattr(
        compile_memory, "_receipt_predicate", lambda _coordinator: lambda *_a: False
    )

    compile_memory.discard_unusable_receipts()
    selected = compile_memory.select_dailies(
        Namespace(file=None), load_state(), coordinator=object()
    )

    assert selected == [vault / "knowledge/daily" / DAY]


def test_a_day_the_discard_did_not_touch_keeps_its_record(vault):
    """Vaults compiled before receipts existed keep their mirror-only days."""
    import compile_memory
    from memory_state import load_state

    (vault / "knowledge/daily/receipts/v3-orphan.md").write_bytes(b"unreadable\n")
    _mirror_says_compiled(vault)

    compile_memory.discard_unusable_receipts()

    assert list(load_state()["compiled_daily_hashes"]) == [DAY]


def test_the_all_flag_says_it_does_nothing(capsys):
    """M-A4: a flag that changes nothing says so instead of looking busy."""
    import compile_memory

    compile_memory._report_deprecated_flags(Namespace(all=True))
    announced = capsys.readouterr().err
    compile_memory._report_deprecated_flags(Namespace(all=False))

    assert "--all is deprecated and does nothing" in announced
    assert capsys.readouterr().err == ""
