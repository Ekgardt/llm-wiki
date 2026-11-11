"""A day compiled in several parts archives with every part's receipt (audit 2026-09-26 B-1).

docs/research/2026-09-26-a-split-day-is-archived-with-every-part.md
"""
from __future__ import annotations

import shutil

from markdown_transaction import MarkdownCoordinator

from tests.a_split_day_is_archived_with_every_part_helpers import (
    _archive,
    _compile_every_part,
    _vault,
)


def test_a_split_day_archives_one_receipt_per_part(tmp_path, monkeypatch) -> None:
    from evidence_resolver import validate_bag

    root, state_root, daily = _vault(tmp_path, monkeypatch)
    parts = _compile_every_part(root, state_root, daily)

    archived = _archive(root, state_root, daily.stem)
    bag = validate_bag(archived.bag_path, coordinator=MarkdownCoordinator(root, state_root), vault=root)
    embedded = sorted(path.name for path in archived.bag_path.glob("compile-receipt-*.md"))

    assert (parts > 1, archived.state, bag.manifest["schema_version"]) == (True, "archived", "archive-manifest/v2")
    assert embedded == [f"compile-receipt-{index}.md" for index in range(parts)]


def test_a_part_quote_resolves_from_the_archive(tmp_path, monkeypatch) -> None:
    from evidence_resolver import EvidenceRef, EvidenceResolver, daily_entries, daily_part_bounds
    from reliable_memory import sha256_bytes

    root, state_root, daily = _vault(tmp_path, monkeypatch)
    content = daily.read_bytes()
    _compile_every_part(root, state_root, daily)
    start, end = daily_part_bounds(content)[1]
    part = content[start:end]
    block_id, block_start, block_end = daily_entries(part)[0]
    ref = EvidenceRef(daily.stem, sha256_bytes(part), block_id, block_start, block_end)

    _archive(root, state_root, daily.stem)
    shutil.rmtree(state_root / "run")

    assert EvidenceResolver(root, state_root=state_root).resolve(ref).bytes == part[block_start:block_end]


def test_the_schema_admits_as_many_parts_as_a_day_can_hold() -> None:
    import json

    from evidence_resolver import ARCHIVE_SCHEMAS, MAX_COMPILE_PARTS

    schema = json.loads(ARCHIVE_SCHEMAS["archive-manifest/v2"].read_text(encoding="utf-8"))

    assert schema["properties"]["compile_parts"]["maxItems"] == MAX_COMPILE_PARTS
