# Auto-archive stale knowledge pages

`scripts/archive_stale.py` and the tests that exercise them most.

Moves pages older than `--days` (default 180) from active directories into `archive/YYYY/`. Archived pages remain available as Markdown and in Git history, but are excluded from active search, index.md, and SessionStart context.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_archive_recency_signal.py tests/test_archive_stale.py -q`.
