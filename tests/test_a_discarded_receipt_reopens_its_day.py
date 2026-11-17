"""Discarding a receipt makes its day pending again, and `--all` says it is idle.

Findings M-A5 and M-A4 of the third audit. See
`docs/research/2026-09-17-the-compile-decides-what-the-snapshot-already-knows.md`.
"""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from tests.compile_paths import point_compile_path

DAY = "2026-07-14.md"
BODY = b"## [10:00:00] session-end | manual\nA durable observation.\n"


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "vault"
    (root / "knowledge/daily/receipts").mkdir(parents=True)
    (root / "knowledge/daily" / DAY).write_bytes(BODY)


    monkeypatch.setenv("LLM_WIKI_ROOT", str(root))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))
    return root


def _corrupt_receipt(root: Path) -> Path:
    """A receipt named for this day's one part, whose body cannot be read."""
    import compile_pipeline

    identity = compile_pipeline.compile_source_identity(
        f"knowledge/daily/{DAY}", compile_pipeline.sha256_bytes(BODY)
    )
    path = root / f"knowledge/daily/receipts/v3-{identity}.md"
    path.write_bytes(b"---\ntype: compile-receipt\n---\ntruncated\n")
    return path


def _mirror_says_compiled(root: Path) -> None:
    import compile_pipeline
    from memory_state import update_state

    digest = compile_pipeline.sha256_bytes(BODY)
    update_state(lambda state: state.update({"compiled_daily_hashes": {DAY: digest}}))
    del root


def test_a_discarded_receipt_takes_its_day_out_of_the_mirror(vault, capsys):
    """Without this the day stays "compiled" with no evidence, and is never redone."""
    from llm_wiki import compile_pipeline_selection
    from memory_state import load_state

    receipt = _corrupt_receipt(vault)
    _mirror_says_compiled(vault)

    discarded = compile_pipeline_selection.discard_unusable_receipts()

    assert (discarded, receipt.exists()) == ([receipt.name], False)
    assert load_state()["compiled_daily_hashes"] == {}
    assert "1 day(s) are pending again" in capsys.readouterr().err


def test_the_reopened_day_is_offered_to_the_next_compile(vault, monkeypatch):
    from llm_wiki import compile_pipeline_selection
    from memory_state import load_state

    _corrupt_receipt(vault)
    _mirror_says_compiled(vault)
    point_compile_path(monkeypatch, "receipt_predicate", lambda _coordinator, **_kwargs: lambda *_a: False)

    compile_pipeline_selection.discard_unusable_receipts()
    selected = compile_pipeline_selection.select_dailies(
        Namespace(file=None), load_state(), coordinator=object()
    )

    assert selected == [vault / "knowledge/daily" / DAY]


def test_a_day_the_discard_did_not_touch_keeps_its_record(vault):
    """Vaults compiled before receipts existed keep their mirror-only days."""
    from llm_wiki import compile_pipeline_selection
    from memory_state import load_state

    (vault / "knowledge/daily/receipts/v3-orphan.md").write_bytes(b"unreadable\n")
    _mirror_says_compiled(vault)

    compile_pipeline_selection.discard_unusable_receipts()

    assert list(load_state()["compiled_daily_hashes"]) == [DAY]


def test_the_all_flag_says_it_does_nothing(capsys):
    """M-A4: a flag that changes nothing says so instead of looking busy."""
    from llm_wiki import compile_pipeline_selection

    compile_pipeline_selection.report_deprecated_flags(Namespace(all=True))
    announced = capsys.readouterr().err
    compile_pipeline_selection.report_deprecated_flags(Namespace(all=False))

    assert "--all is deprecated and does nothing" in announced
    assert capsys.readouterr().err == ""
