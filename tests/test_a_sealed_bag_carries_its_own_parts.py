"""A v2 bag is checked against the parts it recorded, not today's split rule (audit 2026-09-27 B-17).

docs/research/2026-09-27-a-sealed-bag-carries-its-own-parts.md
"""
from __future__ import annotations

import shutil

import pytest
from markdown_transaction import MarkdownCoordinator

from tests.a_split_day_is_archived_with_every_part_helpers import (
    _archive,
    _compile_every_part,
    _vault,
)


def _sealed_split_day(tmp_path, monkeypatch):
    root, state_root, daily = _vault(tmp_path, monkeypatch)
    content = daily.read_bytes()
    _compile_every_part(root, state_root, daily)
    return root, state_root, daily, content, _archive(root, state_root, daily.stem)


def test_a_bag_sealed_under_one_rule_validates_under_another(tmp_path, monkeypatch) -> None:
    import evidence_resolver

    root, state_root, _daily, _content, archived = _sealed_split_day(tmp_path, monkeypatch)
    monkeypatch.setattr(evidence_resolver, "MAX_DAILY_PART_BYTES", evidence_resolver.MAX_DAILY_PART_BYTES // 2)

    bag = evidence_resolver.validate_bag(archived.bag_path, coordinator=MarkdownCoordinator(root, state_root), vault=root)

    assert bag.manifest["schema_version"] == "archive-manifest/v2"


def test_a_part_quote_resolves_after_the_rule_changes(tmp_path, monkeypatch) -> None:
    import evidence_resolver
    from reliable_memory import sha256_bytes

    root, state_root, daily, content, _archived = _sealed_split_day(tmp_path, monkeypatch)
    start, end = evidence_resolver.daily_part_bounds(content)[1]
    part = content[start:end]
    block_id, block_start, block_end = evidence_resolver.daily_entries(part)[0]
    ref = evidence_resolver.EvidenceRef(daily.stem, sha256_bytes(part), block_id, block_start, block_end)
    shutil.rmtree(state_root / "run")
    monkeypatch.setattr(evidence_resolver, "MAX_DAILY_PART_BYTES", evidence_resolver.MAX_DAILY_PART_BYTES * 4)

    resolved = evidence_resolver.EvidenceResolver(root, state_root=state_root).resolve(ref)

    assert resolved.bytes == part[block_start:block_end]


_ENTRY = b"<!-- llm-wiki-operation:1 -->\nbody\n"
_PAYLOAD = b"# day\n" + _ENTRY + _ENTRY
_CUT = len(b"# day\n") + len(_ENTRY)
_END = len(_PAYLOAD)


@pytest.mark.parametrize(
    "spans",
    [
        [(0, _CUT), (_CUT + 1, _END)],
        [(0, _CUT), (_CUT - 1, _END)],
        [(1, _END)],
        [(0, _END - 1)],
        [(0, _CUT + 2), (_CUT + 2, _END)],
        [],
    ],
    ids=["gap", "overlap", "late-start", "short-end", "cut-inside-an-entry", "no-parts"],
)
def test_a_part_table_that_does_not_tile_the_day_at_entries_is_refused(spans) -> None:
    from evidence_resolver import EvidenceResolutionError, require_recorded_parts

    with pytest.raises(EvidenceResolutionError):
        require_recorded_parts(spans, _PAYLOAD)


def test_a_part_table_cut_where_entries_begin_is_accepted() -> None:
    from evidence_resolver import require_recorded_parts

    require_recorded_parts([(0, _CUT), (_CUT, _END)], _PAYLOAD)
