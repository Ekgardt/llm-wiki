# Tests of `scripts/installed_memory_repair.py`

- `tests/test_fresh_install_has_compile_journal.py`: A newly initialized vault can compile without an upgrade of its empty queue.

`scripts/installed_memory_repair.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_fresh_install_has_compile_journal.py -q`.
