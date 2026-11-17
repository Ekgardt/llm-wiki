# Tests of `scripts/project_checkpoint.py`

- `tests/test_an_idle_checkpoint_backlog_finishes.py`: Unattended checkpoint drain advances a quiet project's elapsed debounce.

`scripts/project_checkpoint.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_an_idle_checkpoint_backlog_finishes.py -q`.
