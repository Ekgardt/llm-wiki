from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.evidence_resolver_helpers import (
    _authorize_archive_source,
    _long_day,
    _reference,
    _sha,
    _write_bag,
)


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    (tmp_path / "knowledge" / "daily" / "archive").mkdir(parents=True)
    return tmp_path


def test_parse_requires_exact_canonical_logical_reference() -> None:
    from evidence_resolver import EvidenceRef

    value = f"daily:2026-01-01 sha256:{'a' * 64} block:evt-1 bytes:10-20"
    assert str(EvidenceRef.parse(value)) == value
    for invalid in (
        value + " trailing",
        value.replace("2026-01-01", "../secret"),
        value.replace("bytes:10-20", "bytes:20-10"),
        value.replace("block:evt-1", "block:../evt"),
        value.replace("sha256:", "sha256:A"),
    ):
        with pytest.raises(ValueError):
            EvidenceRef.parse(invalid)


def test_direct_construction_enforces_the_same_reference_contract() -> None:
    from evidence_resolver import EvidenceRef

    with pytest.raises(ValueError, match="daily ID"):
        EvidenceRef("../2026-01-01", "a" * 64, "evt-1", 1, 2)
    with pytest.raises(ValueError, match="SHA-256"):
        EvidenceRef("2026-01-01", "A" * 64, "evt-1", 1, 2)
    with pytest.raises(ValueError, match="block ID"):
        EvidenceRef("2026-01-01", "a" * 64, "../evt", 1, 2)
    with pytest.raises(ValueError, match="half-open"):
        EvidenceRef("2026-01-01", "a" * 64, "evt-1", 2, 2)


@pytest.mark.parametrize(
    "candidate",
    [
        "daily:2026-01-01",
        f"daily:2026-01-01 sha256:{'a' * 64} block:evt-1 bytes:1-2 trailing",
        f"xdaily:2026-01-01 sha256:{'a' * 64} block:evt-1 bytes:1-2",
        f"daily:2026-01-01 sha256:{'a' * 64} block:../evt bytes:1-2",
    ],
)
def test_extraction_rejects_every_malformed_daily_candidate(candidate: str) -> None:
    from evidence_resolver import extract_evidence_references

    with pytest.raises(ValueError, match="evidence reference"):
        extract_evidence_references(f"## Evidence\n- `{candidate}`\n")


def test_block_parser_uses_reference_block_id_grammar() -> None:
    from evidence_resolver import EvidenceResolutionError, daily_entries

    with pytest.raises(EvidenceResolutionError, match="block ID"):
        daily_entries(b"## [../evt] event\ntext\n")


def test_flat_resolution_uses_utf8_half_open_bytes_lines_and_block_hash(vault: Path) -> None:
    from evidence_resolver import EvidenceRef, EvidenceResolver

    source = "# day\n## [evt-1] event\nαβ line\nnext\n".encode()
    path = vault / "knowledge" / "daily" / "2026-01-01.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(source)
    start = source.index("α".encode())
    end = start + len("αβ line".encode())

    result = EvidenceResolver(vault).resolve(
        EvidenceRef.parse(_reference("2026-01-01", source, "evt-1", start, end))
    )

    block = source[source.index(b"## [evt-1]") :]
    assert result.bytes == "αβ line".encode()
    assert result.sha256 == _sha(result.bytes)
    assert result.source_sha256 == _sha(source)
    assert result.block_sha256 == _sha(block)
    assert (result.byte_start, result.byte_end) == (start, end)
    assert (result.line_start, result.line_end) == (3, 4)
    assert result.location == "flat"


def test_resolution_rejects_non_utf8_boundary_outside_or_ambiguous_block(vault: Path) -> None:
    from evidence_resolver import EvidenceRef, EvidenceResolutionError, EvidenceResolver

    source = "## [evt-1] first\nα\n## [evt-1] second\nα\n".encode()
    path = vault / "knowledge" / "daily" / "2026-01-01.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(source)
    alpha = source.index("α".encode())
    resolver = EvidenceResolver(vault)

    # A span that no single entry holds is refused; one inside an entry names it
    # (docs/research/2026-09-26-an-evidence-span-names-its-own-block.md).
    with pytest.raises(EvidenceResolutionError, match="ambiguous"):
        resolver.resolve(EvidenceRef.parse(_reference("2026-01-01", source, "evt-1", alpha, len(source))))

    unique = b"preamble\n## [evt-1] event\n" + "α".encode() + b"\n"
    path.write_bytes(unique)
    alpha = unique.index("α".encode())
    with pytest.raises(EvidenceResolutionError, match="UTF-8"):
        resolver.resolve(EvidenceRef.parse(_reference("2026-01-01", unique, "evt-1", alpha + 1, alpha + 2)))
    with pytest.raises(EvidenceResolutionError, match="block"):
        resolver.resolve(EvidenceRef.parse(_reference("2026-01-01", unique, "evt-1", 0, 2)))


def test_flat_hash_mismatch_fails_closed_without_archive_fallback(vault: Path) -> None:
    from evidence_resolver import EvidenceRef, EvidenceResolutionError, EvidenceResolver

    archived = b"# day\n## [evt-1] event\narchived\n"
    _write_bag(vault, "2026-01-01", archived)
    flat = vault / "knowledge" / "daily" / "2026-01-01.md"
    flat.write_bytes(b"# day\n## [evt-1] event\nchanged\n")
    start = archived.index(b"archived")

    with pytest.raises(EvidenceResolutionError, match="hash mismatch"):
        EvidenceResolver(vault).resolve(
            EvidenceRef.parse(_reference("2026-01-01", archived, "evt-1", start, start + 8))
        )


def test_validated_archive_resolves_identically_and_ambiguity_fails(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evidence_resolver import EvidenceRef, EvidenceResolutionError, EvidenceResolver

    source = b"# day\n## [evt-1] event\narchived bytes\n"
    state_root = _authorize_archive_source(vault, source, monkeypatch)
    first = _write_bag(vault, "2026-01-01", source)
    from archive_daily import DailyArchiver

    DailyArchiver.seal(first)
    start = source.index(b"archived bytes")
    ref = EvidenceRef.parse(_reference("2026-01-01", source, "evt-1", start, len(source) - 1))
    result = EvidenceResolver(vault, state_root=state_root).resolve(ref)
    assert result.bytes == b"archived bytes"
    assert result.location == "archive"

    second = _write_bag(vault, "2026-01-01", source, suffix="two")
    DailyArchiver.seal(second)
    with pytest.raises(EvidenceResolutionError, match="ambiguous"):
        EvidenceResolver(vault, state_root=state_root).resolve(ref)


def test_archive_validation_rejects_tamper_oversize_and_links(vault: Path) -> None:
    from evidence_resolver import EvidenceRef, EvidenceResolutionError, EvidenceResolver

    source = b"# day\n## [evt-1] event\narchived bytes\n"
    bag = _write_bag(vault, "2026-01-01", source)
    start = source.index(b"archived bytes")
    ref = EvidenceRef.parse(_reference("2026-01-01", source, "evt-1", start, len(source) - 1))
    (bag / "archive-manifest.json").write_bytes(b"{" + b" " * 1_100_000 + b"}")
    with pytest.raises(EvidenceResolutionError, match="exceeds"):
        EvidenceResolver(vault).resolve(ref)

    if hasattr(os, "symlink"):
        (bag / "archive-manifest.json").unlink()
        outside = vault / "outside.md"
        outside.write_bytes(source)
        try:
            os.symlink(outside, bag / "archive-manifest.json")
        except OSError:
            pytest.skip("links require privileges on this platform")
        with pytest.raises(EvidenceResolutionError, match="non-symlink|regular"):
            EvidenceResolver(vault).resolve(ref)


def test_archive_directory_limit_is_enforced_while_iterating(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import evidence_resolver

    month = vault / "knowledge/daily/archive/2026-01"
    month.mkdir()
    for name in ("one", "two", "three"):
        (month / name).mkdir()
    monkeypatch.setattr(evidence_resolver, "MAX_DIRECTORY_ENTRIES", 2)
    ref = evidence_resolver.EvidenceRef("2026-01-01", "a" * 64, "evt-1", 1, 2)

    with pytest.raises(evidence_resolver.EvidenceResolutionError, match="entry scan limit"):
        evidence_resolver.EvidenceResolver(vault)._resolve_archive(ref)


def test_repeated_historical_evidence_reuses_search_but_checks_current_bytes(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import Mock

    import evidence_resolver as module

    original = b"## [evt-1] event\nretained evidence\n"
    grown = original + b"\n## [evt-2] later\nmore evidence\n"
    path = vault / "knowledge/daily/2026-01-02.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(grown)
    start = original.index(b"retained")
    reference = _reference("2026-01-02", original, "evt-1", start, len(original) - 1)
    search = Mock(wraps=module.compile_part_slice)
    monkeypatch.setattr(module, "compile_part_slice", search)
    resolver = module.EvidenceResolver(vault)

    assert resolver.resolve(reference).bytes == b"retained evidence"
    assert resolver.resolve(reference).bytes == b"retained evidence"
    assert search.call_count == 1
    identity = path.stat()
    path.write_bytes(grown.replace(b"retained", b"tampered", 1))
    os.utime(path, ns=(identity.st_atime_ns, identity.st_mtime_ns))
    with pytest.raises(module.EvidenceResolutionError, match="hash mismatch"):
        resolver.resolve(reference)
    assert search.call_count == 2


def test_cached_part_still_checks_each_evidence_block(vault: Path) -> None:
    from evidence_resolver import EvidenceResolutionError, EvidenceResolver

    original = b"## [evt-1] event\nretained evidence\n"
    path = vault / "knowledge/daily/2026-01-02.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(original + b"\n## [evt-2] later\nmore evidence\n")
    start = original.index(b"retained")
    resolver = EvidenceResolver(vault)
    resolver.resolve(_reference("2026-01-02", original, "evt-1", start, len(original) - 1))
    with pytest.raises(EvidenceResolutionError):
        resolver.resolve(_reference("2026-01-02", original, "evt-2", start, len(original) - 1))


def test_evidence_from_one_compile_part_resolves_after_the_day_grows(vault: Path) -> None:
    """A page is written from one part; every later reader must still find those bytes."""
    from evidence_resolver import (
        EvidenceRef,
        EvidenceResolutionError,
        EvidenceResolver,
        daily_part_bounds,
    )

    day = _long_day(150)
    bounds = daily_part_bounds(day)
    assert len(bounds) > 2, "the fixture must be long enough to be split"
    start, end = bounds[1]
    part = day[start:end]
    marker = part.index(b"quote-")
    quote = part[marker : part.index(b"\n", marker)]
    block = part[part.index(b"## [") + 4 : part.index(b"]")].decode()
    path = vault / "knowledge" / "daily" / "2026-01-02.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(day + _long_day(20))

    result = EvidenceResolver(vault).resolve(
        EvidenceRef.parse(
            _reference("2026-01-02", part, block, marker, marker + len(quote))
        )
    )

    assert result.bytes == quote
    assert result.source_sha256 == _sha(part)
    assert result.location == "flat-part"

    path.write_bytes((day + _long_day(20)).replace(quote, b"t" + quote[1:], 1))
    with pytest.raises(EvidenceResolutionError, match="hash mismatch"):
        EvidenceResolver(vault).resolve(
            EvidenceRef.parse(
                _reference("2026-01-02", part, block, marker, marker + len(quote))
            )
        )


def test_a_slice_that_ends_at_a_capture_block_still_resolves(vault: Path) -> None:
    """A daily grows by two kinds of entry, and both end a historical slice.

    The transactional appender writes `<!-- llm-wiki-operation: … -->`; the
    capture path writes a `## [HH:MM:SS] …` block and no marker. Until
    2026-09-12 only the marker was a candidate boundary, so two claims on the
    live vault cited bytes whose slice ended at a block start and could not be
    resolved at all. Research:
    `docs/research/2026-09-12-a-daily-grows-by-two-kinds-of-entry.md`.
    """
    from evidence_resolver import EvidenceRef, EvidenceResolver

    compiled = (
        b"# day\n"
        b"<!-- llm-wiki-operation: op-1 -->\n"
        b"## [09:20:13] first\n"
        b"the line the page quoted\n"
    )
    appended = compiled + b"\n## [12:50:16] second\nwritten after the compile\n"
    path = vault / "knowledge" / "daily" / "2026-01-03.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(appended)
    quote = b"the line the page quoted"
    start = compiled.index(quote)

    result = EvidenceResolver(vault).resolve(
        EvidenceRef.parse(
            _reference("2026-01-03", compiled, "09:20:13", start, start + len(quote))
        )
    )

    assert (result.bytes, result.source_sha256) == (quote, _sha(compiled))
    assert result.location == "flat-part"
