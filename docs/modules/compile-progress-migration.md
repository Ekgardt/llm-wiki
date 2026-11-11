# Qualified candidate migration and explicit offline upgrade/recovery

`scripts/compile_progress_migration.py` and the tests that exercise them most.

Offline operations require all agent processes stopped as well as the canonical maintenance fence. This is not an online migration of already-open SQL handles.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_compile_progress_migration.py tests/test_compile_progress_repair_cli.py -q`.
