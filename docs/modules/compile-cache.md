# Content-addressed cache for validated, normalized compile plans

`scripts/compile_cache.py` and the tests that exercise them most.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_compile_cache.py tests/test_the_compile_decides_what_the_snapshot_already_knows.py -q`.
