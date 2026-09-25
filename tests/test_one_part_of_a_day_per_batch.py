"""Two parts of one day never share a compile batch.

Sources are keyed by path, so the second part of a day in the same batch was
dropped from the prompt while its receipt was still written. See
docs/research/2026-09-25-one-part-of-a-day-per-batch.md.
"""

from __future__ import annotations

from types import SimpleNamespace

import compile_memory
import pytest


def _part(path: str, index: int, count: int) -> compile_memory.DailySnapshot:
    return compile_memory.DailySnapshot(
        path, b"x", "0" * 64, part_index=index, part_count=count, byte_start=index, byte_end=index + 1
    )


def _groups(dailies) -> list[set[str]]:
    inputs = SimpleNamespace(dailies=tuple(dailies))
    budget = SimpleNamespace(available_input_tokens=1_000_000)
    return compile_memory._group_dailies(inputs, budget, lambda paths, *_: len(paths))


def test_each_part_of_a_long_day_goes_to_its_own_batch() -> None:
    parts = [_part("knowledge/daily/2026-09-01.md", index, 3) for index in range(3)]

    groups = _groups(parts)

    assert [len(group) for group in groups] == [1, 1, 1]


def test_whole_days_still_share_a_batch() -> None:
    days = [_part(f"knowledge/daily/2026-09-0{day}.md", 0, 1) for day in (1, 2, 3)]

    assert len(_groups(days)) == 1


def test_the_first_part_of_a_long_day_joins_the_days_before_it() -> None:
    dailies = [_part("knowledge/daily/2026-09-01.md", 0, 1)] + [
        _part("knowledge/daily/2026-09-02.md", index, 2) for index in range(2)
    ]

    assert [len(group) for group in _groups(dailies)] == [2, 1]


def test_a_batch_holding_two_parts_of_one_day_is_refused() -> None:
    parts = [_part("knowledge/daily/2026-09-01.md", index, 2) for index in range(2)]

    with pytest.raises(ValueError, match="two parts of one day"):
        compile_memory._deduplicated_sources(parts)
