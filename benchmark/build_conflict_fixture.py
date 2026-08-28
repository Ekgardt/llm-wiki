"""Derive the conflict fixture from the MemoryAgentBench parquet.

Run once, offline afterwards. The parquet is not vendored: it is a third-party
dataset and this repository stores only the derived conflict set, together with
the URL it came from and the SHA-256 of the exact bytes that were read, so the
derivation can be checked rather than trusted.

    curl -sSL -o /tmp/cr.parquet \\
      https://huggingface.co/datasets/ai-hyz/MemoryAgentBench/resolve/main/data/Conflict_Resolution-00000-of-00001.parquet
    uv run --with pyarrow python benchmark/build_conflict_fixture.py \\
      --parquet /tmp/cr.parquet --out benchmark/conflict-resolution-v1.json

`pyarrow` is imported inside the reader so that importing this module, and the
whole rest of the stand, never needs it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from factconsolidation_data import DEFAULT_ROW, build_fixture  # noqa: E402


def read_row(parquet: Path, row_id: str) -> Mapping[str, object]:
    """The one FactConsolidation row whose question ids start with `row_id`."""
    import pyarrow.parquet as pq

    rows = pq.read_table(parquet).to_pylist()
    for row in rows:
        if _row_matches(row, row_id):
            return row
    raise SystemExit(f"no row whose qa_pair_ids start with {row_id!r}")


def _row_matches(row: object, row_id: str) -> bool:
    if not isinstance(row, Mapping):
        return False
    identifiers = _qa_pair_ids(row.get("metadata"))
    return bool(identifiers) and str(identifiers[0]).startswith(row_id)


def _qa_pair_ids(metadata: object) -> tuple[object, ...]:
    if not isinstance(metadata, Mapping):
        return ()
    identifiers = metadata.get("qa_pair_ids")
    if isinstance(identifiers, (str, bytes)) or not isinstance(identifiers, Sequence):
        return ()
    return tuple(identifiers)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parquet", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--row", default=DEFAULT_ROW)
    args = parser.parse_args(argv)

    digest = hashlib.sha256(args.parquet.read_bytes()).hexdigest()
    row = read_row(args.parquet, args.row)
    fixture = build_fixture(row, row_id=args.row, source_sha256=digest)
    args.out.write_text(
        json.dumps(fixture, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _report(fixture)
    return 0


def _report(fixture: Mapping[str, object]) -> None:
    conflicts = fixture["conflicts"]
    assert isinstance(conflicts, list)
    confirmed = sum(1 for item in conflicts if item["gold_confirmed"])
    print(f"row              {fixture['row_id']}")
    print(f"source sha256    {fixture['source_sha256']}")
    print(f"numbered lines   {fixture['numbered_lines']}")
    print(f"parsed facts     {fixture['parsed_facts']}")
    print(f"unparsed lines   {len(fixture['unparsed_lines'])}")
    print(f"conflicts        {len(conflicts)}")
    print(f"gold-confirmed   {confirmed}")


if __name__ == "__main__":
    raise SystemExit(main())
