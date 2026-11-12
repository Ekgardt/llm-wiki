# Two stand workers on four cores get two threads each, not four

- `tests/test_two_workers_share_four_cores.py`: Two stand workers on four cores get two threads each, not four.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_two_workers_share_four_cores.py -q`.
