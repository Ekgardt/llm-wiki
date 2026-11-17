# Move session records out of the active tree once they are old enough

`scripts/archive_sessions.py` and the tests that exercise them most.

Every session leaves a redacted copy of itself under `knowledge/raw/sessions/<date>/`. Those records are never deleted — they are the evidence a compiled page is built from — but they need not stay in the active tree forever: this vault writes about a megabyte of them a day, and the consolidation pass only ever reads yesterday.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_archive_sessions.py tests/test_breadcrumb_archive_compatibility.py -q`.
