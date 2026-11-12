# Run a child process and end its whole tree when it times out or is interrupted

`scripts/process_tree.py` and the tests that exercise them most.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_an_interrupted_cli_ends_its_child.py tests/test_sync_memory.py -q`.
