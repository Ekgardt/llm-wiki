"""Helpers shared by test_receipt_versions_preserve_historical_archive_authority.py and the test files importing them."""
from __future__ import annotations

import json
from pathlib import Path

from archive_daily import DailyArchiver


def _restore_v3_bag(directory):
    fixture = Path(__file__).parent / 'fixtures/compile-receipt-v3-bag.json'
    files = json.loads(fixture.read_bytes())['files']
    directory.mkdir()
    for name, text in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    DailyArchiver.seal(directory)
    return directory
