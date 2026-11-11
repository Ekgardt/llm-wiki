# A recovery-clock check must not assert a host scheduling guarantee

- `tests/test_archive_clock_recovery_ignores_external_scheduling.py`: A recovery-clock check must not assert a host scheduling guarantee.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_archive_clock_recovery_ignores_external_scheduling.py -q`.
