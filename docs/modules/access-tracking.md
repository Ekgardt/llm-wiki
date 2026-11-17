# Legacy access adapters and explicit frontmatter promotion

`scripts/access_tracking.py` and the tests that exercise them most.

Tracks how often each page is accessed (search hits, advisory injection, direct reads). This data powers: - Forgetting curve (Ebbinghaus decay): pages with no access decay over time. - Quality scoring: frequently-accessed pages are validated as useful. - Advisory ranking: recently-accessed pages get boost in SessionStart.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_access_tracking.py tests/test_one_page_has_one_identity_in_the_telemetry.py -q`.
