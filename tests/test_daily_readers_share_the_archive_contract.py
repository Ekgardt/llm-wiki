"""A day admitted by the existing archive contract reaches every compile reader."""

import compile_memory
import pytest
from markdown_transaction import MarkdownCoordinator
from reliable_memory import sha256_bytes

from tests.test_compile_transactions import vault as vault


def _large_day(root):
    phrase = "Большая дневная запись без потери байтов. ".encode()
    content = b"# 2026-07-14\n## [10:00:00] session-end | manual\n" + phrase * (compile_memory.MAX_SOURCE_BYTES // len(phrase) + 1)
    path = root / "knowledge/daily/2026-07-14.md"
    path.write_bytes(content)
    return path, content


def test_snapshot_keeps_every_byte_and_part_above_the_old_daily_read_bound(vault):
    root, _state = vault
    day, content = _large_day(root)
    inputs = compile_memory.snapshot_compile_inputs([day])
    assert b"".join(part.content for part in inputs.dailies) == content
    daily_source = next(source for source in inputs.sources if source.logical_path == day.relative_to(root).as_posix())
    assert daily_source.content == content
    assert daily_source.sha256 == sha256_bytes(content)
    assert inputs.dailies[-1].byte_end == len(content)


def test_pending_and_explicit_readers_accept_the_same_actual_day(vault):
    root, state = vault
    day, content = _large_day(root)
    coordinator = MarkdownCoordinator(root, state)
    assert compile_memory._readable_daily(day) == content
    assert compile_memory._explicit_daily(day, coordinator) == [day]
    assert not compile_memory._daily_already_compiled(day, {}, coordinator)


def test_whole_day_mirror_keeps_its_exact_digest_and_requires_all_parts(vault):
    root, _state = vault
    day, content = _large_day(root)
    logical = day.relative_to(root).as_posix()
    assert compile_memory._whole_daily_digest(logical, lambda _path, _digest: True) == sha256_bytes(content)
    assert compile_memory._whole_daily_digest(logical, lambda _path, _digest: False) is None


def test_the_configured_total_source_budget_still_refuses_the_day(vault, monkeypatch):
    root, _state = vault
    day, content = _large_day(root)
    monkeypatch.setenv("LLM_WIKI_COMPILE_MAX_TOTAL_SOURCE_BYTES", str(len(content) - 1))
    with pytest.raises(ValueError, match="exceeds"):
        compile_memory.snapshot_compile_inputs([day])


def test_the_existing_daily_archive_bound_is_still_enforced(vault):
    from evidence_resolver import MAX_DAILY_BYTES

    root, _state = vault
    day = root / "knowledge/daily/2026-07-14.md"
    with day.open("wb") as stream:
        stream.seek(MAX_DAILY_BYTES)
        stream.write(b"x")
    with pytest.raises(ValueError, match="exceeds"):
        compile_memory.snapshot_compile_inputs([day])
    assert compile_memory._readable_daily(day) is None
