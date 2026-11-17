# Tests of `scripts/queue_core.py`

- `tests/test_legacy_queue_entry_points_upgrade_recover_and_refuse.py`

`scripts/queue_core.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_legacy_queue_entry_points_upgrade_recover_and_refuse.py -q`.
