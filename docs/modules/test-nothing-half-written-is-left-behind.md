# A prune that died mid-rename, and a Windows publish that failed, leave nothing

- `tests/test_nothing_half_written_is_left_behind.py`: A prune that died mid-rename, and a Windows publish that failed, leave nothing.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_nothing_half_written_is_left_behind.py -q`.
