# Import past agent transcripts as session records — an explicit operator action

`scripts/backfill_sessions.py` and the tests that exercise them most.

Session records started on 2026-08-23, so the vault remembers nothing that happened before it. The transcripts are still on disk; this walks them once and writes the same record the live capture writes, through the same transaction and the same DLP boundary. Nothing is deleted and nothing is moved: the transcript stays where it is.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_backfill_sessions.py -q`.
