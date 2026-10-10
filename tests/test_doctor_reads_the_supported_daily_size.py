"""Doctor must inspect genuine receipts for the supported daily source family."""
from __future__ import annotations

import hashlib
from contextlib import contextmanager

import compile_memory as compiler
import doctor
import pytest
from evidence_resolver import MAX_DAILY_BYTES
from markdown_transaction import MarkdownCoordinator


@pytest.fixture(scope="module")
def compiled_day(tmp_path_factory):
    root = tmp_path_factory.mktemp("large-receipted-day")
    state = root / "state"
    for relative in ("knowledge/daily/receipts", "knowledge/notes", "knowledge/projects"):
        (root / relative).mkdir(parents=True)
    mapping = {"ROOT": root, "STATE_ROOT": state, "MEMORY": root / "knowledge", "DAILY_DIR": root / "knowledge/daily", "KNOWLEDGE": root / "knowledge/notes", "INDEX": root / "knowledge/index.md", "LOG": root / "knowledge/log.local.md", "AGENTS": root / "AGENTS.md"}
    patch = pytest.MonkeyPatch()
    for name, path in mapping.items():
        patch.setattr(compiler, name, path)
    for relative in ("knowledge/index.md", "knowledge/log.local.md", "AGENTS.md"):
        (root / relative).write_bytes(b"# Isolated transaction fixture\n")
    daily = root / "knowledge/daily/2026-09-14.md"
    content = b"## [10:00:00] manual\n" + b"<!-- ordinary test source -->\n" * 145000
    assert 4 * 1024 * 1024 < len(content) < MAX_DAILY_BYTES
    daily.write_bytes(content)
    coordinator = MarkdownCoordinator(root, state)
    inputs = compiler.snapshot_compile_inputs([daily])
    for index, batch in enumerate(compiler.pack_compile_batches(inputs, model=None)):
        _publish(coordinator, batch, index)
    yield root, state, daily, coordinator, content
    patch.undo()


def _publish(coordinator, batch, index):
    compiler.apply_compile_plan(
        batch.inputs, {"schema_version": "compile-plan/v2", "operations": []},
        action_key=hashlib.sha256(str(index).encode()).hexdigest(), trigger="manual",
        coordinator=coordinator, batch=batch,
        provider_budget={"provider": "fake", "model": "test", "max_output_tokens": 4000},
    )


@contextmanager
def _selection(fixture):
    root, state, _daily, coordinator, _content = fixture
    with coordinator._authority_read_connection(float("inf")) as database:
        yield doctor._CompiledDaySupersession(root, state, database=database)


def _logical(fixture):
    return fixture[2].relative_to(fixture[0]).as_posix()


def test_a_large_current_day_has_genuine_committed_part_authority(compiled_day):
    with _selection(compiled_day) as selection:
        assert selection._day_compiled(_logical(compiled_day), set())


def test_large_legacy_whole_source_uses_exact_current_digest(compiled_day):
    source = {"logical_path": _logical(compiled_day), "sha256": hashlib.sha256(compiled_day[4]).hexdigest()}
    with _selection(compiled_day) as selection:
        assert selection._whole_legacy_source_compiled(source)


def test_changed_whole_source_digest_does_not_resolve(compiled_day):
    source = {"logical_path": _logical(compiled_day), "sha256": "0" * 64}
    with _selection(compiled_day) as selection:
        assert not selection._whole_legacy_source_compiled(source)


def test_missing_one_actual_part_receipt_keeps_the_day_pending(compiled_day):
    receipt = next((compiled_day[0] / "knowledge/daily/receipts").glob("v4-*.md"))
    raw = receipt.read_bytes()
    receipt.unlink()
    try:
        with _selection(compiled_day) as selection:
            assert not selection._day_compiled(_logical(compiled_day), set())
    finally:
        receipt.write_bytes(raw)


def test_changed_original_source_keeps_existing_receipts_pending(compiled_day):
    daily, content = compiled_day[2], compiled_day[4]
    daily.write_bytes(content.replace(b"manual", b"edited", 1))
    try:
        with _selection(compiled_day) as selection:
            assert not selection._day_compiled(_logical(compiled_day), set())
    finally:
        daily.write_bytes(content)


def test_foreign_day_cannot_borrow_the_large_days_receipts(compiled_day):
    other = compiled_day[2].with_name("2026-09-13.md")
    other.write_bytes(compiled_day[4])
    try:
        with _selection(compiled_day) as selection:
            assert not selection._day_compiled(other.relative_to(compiled_day[0]).as_posix(), set())
    finally:
        other.unlink()


def test_above_supported_daily_capacity_is_still_refused(compiled_day):
    daily, content = compiled_day[2], compiled_day[4]
    daily.write_bytes(b"x" * (MAX_DAILY_BYTES + 1))
    try:
        with _selection(compiled_day) as selection:
            with pytest.raises(ValueError, match="exceeds"):
                selection._day_compiled(_logical(compiled_day), set())
    finally:
        daily.write_bytes(content)
