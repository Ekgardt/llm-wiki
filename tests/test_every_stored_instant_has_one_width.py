"""A stored instant's text order is its time order (audit 2026-09-26 C-12).

docs/research/2026-09-26-every-stored-instant-has-one-width.md
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

WHOLE = datetime(2026, 9, 26, 10, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("module_name", ["iso_time", "markdown_transaction", "project_journal"])
def test_a_whole_second_sorts_before_the_half_second_after_it(module_name) -> None:
    import importlib

    module = importlib.import_module(module_name)
    write = getattr(module, "utc_text", None) or module._timestamp

    earlier, later = write(WHOLE), write(WHOLE + timedelta(milliseconds=500))

    assert (earlier, earlier < later) == ("2026-09-26T10:00:00.000000Z", True)

