"""Repeated live claims reuse parsing, while each lookup still reads the source."""
import os
from unittest.mock import Mock

import evidence_resolver as module
import pytest

from tests.test_evidence_resolver import _reference


def _source(tmp_path):
    path = tmp_path / "knowledge/daily/2026-10-09.md"
    path.parent.mkdir(parents=True)
    content = b"## [evt-1] first\nfirst evidence\n## [evt-2] second\nsecond evidence\n"
    path.write_bytes(content)
    return path, content


def _quote_reference(content, block, quote):
    start = content.index(quote)
    return _reference("2026-10-09", content, block, start, start + len(quote))


def test_live_source_is_read_each_time_but_parsed_once_for_different_claims(tmp_path, monkeypatch):
    path, content = _source(tmp_path)
    read = Mock(wraps=module._flat_source)
    parse = Mock(wraps=module.daily_entries)
    monkeypatch.setattr(module, "_flat_source", read)
    monkeypatch.setattr(module, "daily_entries", parse)
    resolver = module.EvidenceResolver(tmp_path)

    first = resolver.resolve(_quote_reference(content, "evt-1", b"first evidence"))
    second = resolver.resolve(_quote_reference(content, "evt-2", b"second evidence"))

    assert first.bytes == b"first evidence"
    assert second.bytes == b"second evidence"
    assert read.call_args_list == [((path,),), ((path,),)]
    assert parse.call_count == 1


def test_same_size_restored_time_edit_cannot_reuse_the_old_entry_parse(tmp_path, monkeypatch):
    path, content = _source(tmp_path)
    parse = Mock(wraps=module.daily_entries)
    monkeypatch.setattr(module, "daily_entries", parse)
    resolver = module.EvidenceResolver(tmp_path)
    resolver.resolve(_quote_reference(content, "evt-1", b"first evidence"))
    before = path.stat()
    changed = content.replace(b"evt-1", b"evt-3")
    path.write_bytes(changed)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

    with pytest.raises(module.EvidenceResolutionError, match="ambiguous or missing"):
        resolver.resolve(_quote_reference(changed, "evt-1", b"first evidence"))
    assert resolver.resolve(_quote_reference(changed, "evt-3", b"first evidence")).bytes == b"first evidence"
    assert parse.call_count == 2


def test_reused_parse_checks_the_selected_block_for_every_claim(tmp_path):
    _path, content = _source(tmp_path)
    resolver = module.EvidenceResolver(tmp_path)
    resolver.resolve(_quote_reference(content, "evt-1", b"first evidence"))

    with pytest.raises(module.EvidenceResolutionError, match="ambiguous or missing"):
        resolver.resolve(_quote_reference(content, "evt-1", b"second evidence"))


def test_historical_part_reuses_parsing_only_after_reading_the_current_file(tmp_path, monkeypatch):
    path, original = _source(tmp_path)
    path.write_bytes(original + b"\n## [evt-3] later\nmore evidence\n")
    parse = Mock(wraps=module.daily_entries)
    read = Mock(wraps=module._flat_source)
    monkeypatch.setattr(module, "daily_entries", parse)
    monkeypatch.setattr(module, "_flat_source", read)
    resolver = module.EvidenceResolver(tmp_path)
    reference = _quote_reference(original, "evt-1", b"first evidence")
    first = resolver.resolve(reference)
    initial_parses = parse.call_count

    assert resolver.resolve(reference) == first
    assert parse.call_count == initial_parses
    assert read.call_count == 2


@pytest.mark.parametrize("content", [
    b"## [evt-1] first\nfirst evidence\n## [evt-1] second\nsecond evidence\n",
    "## [evt-1] первый\r\nfirst evidence\r\n## [evt-2] второй\r\nsecond evidence\r\n".encode(),
])
def test_live_cached_result_preserves_every_uncached_evidence_field(tmp_path, content):
    path, _original = _source(tmp_path)
    path.write_bytes(content)
    reference = _quote_reference(content, "evt-1", b"first evidence")
    resolver = module.EvidenceResolver(tmp_path)
    expected = resolver._slice(module.EvidenceRef.parse(reference), content, path, "flat")

    assert resolver.resolve(reference) == expected
    assert resolver.resolve(reference) == expected
