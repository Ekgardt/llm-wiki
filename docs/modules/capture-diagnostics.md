# Durable diagnostics for capture hooks — a lost capture must leave a trace

`scripts/capture_diagnostics.py` and the tests that exercise them most.

Prompt and post-tool capture are best-effort by design: they must never break the user's session, so every failure path returns quietly. Silence is not the same as safety. A capture that fails without a record is indistinguishable from a session that had nothing worth capturing, and the loss is invisible to the user and to maintenance alike.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.
