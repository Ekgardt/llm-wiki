# Replay the delta of an append that a race refused, as a new attempt

`scripts/repair_refused_appends.py` and the tests that exercise them most.

A daily append is written as a whole-file replace with a precondition on the bytes it read. When another session lands first the precondition fails, the attempt is quarantined, and the block it carried — a tool breadcrumb, a checkpoint line — is written nowhere. Measured on this vault on 2026-09-07: nine such attempts stood open, six of them tool breadcrumbs for `knowledge/daily/2026-09-05.md`, each one a marker line and a block of about 250 bytes that nothing wrote since.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_refused_append_is_owed_its_delta.py -q`.
