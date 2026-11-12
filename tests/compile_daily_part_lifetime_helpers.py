"""Helpers shared by test_compile_daily_part_lifetime.py and the test files importing them."""

import compile_pipeline


def _long_day(root):
    path = root / 'knowledge/daily/2026-10-20.md'
    body = b'# Daily\n' + b''.join(
        f'\n## [10:00:{index:02d}] session-end | manual\n'.encode()
        + bytes([65 + index]) * compile_pipeline.MAX_DAILY_PART_BYTES
        for index in range(8))
    path.write_bytes(body)
    return path
