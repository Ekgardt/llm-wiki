# User-level SessionEnd hook — tag the day's daily log with the project slug

`scripts/session_end_project_tag.py` and the tests that exercise them most.

Fires at session end from any cwd. Appends a minimal marker entry to `knowledge/daily/YYYY-MM-DD.md` identifying the project slug and session metadata. This lets cross-project sessions leave breadcrumbs in the shared daily log.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_session_end_failure_status.py -q`.
