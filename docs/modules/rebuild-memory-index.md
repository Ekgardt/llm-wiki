# Regenerate `knowledge/index.md` from `knowledge/notes/**/*.md`

`scripts/rebuild_memory_index.py` and the tests that exercise them most.

Supports both flat notes (current public layout) and typed subdirs (concepts/decisions/patterns/debugging/qa) when present.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_index_publishes_only_public_pages.py tests/test_manifest_only_source_capture.py tests/test_streamed_index_rendering.py -q`.
