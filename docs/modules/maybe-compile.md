# Concurrency-safe compile trigger

`scripts/maybe_compile.py` and the tests that exercise them most.

Checks if compile is needed and no other compile is running, then spawns compile_memory.py in a detached background process. Admission reads complete context receipts; a caller may provide its deadline for that work.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_context_receipts_survive_all_consumers.py tests/test_maybe_compile.py -q`.
