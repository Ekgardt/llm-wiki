# Adopt capture intents that were published durably but never dispatched

`scripts/capture_adoption.py` and the tests that exercise them most.

The capture path publishes a durable intent — the whole redacted record, on disk under `run/capture-intents/ready/`, plus a row in `capture_intents` — and only then enqueues the task that gives it a worker. Between those two steps the publisher holds one capture owner and one intent fence. If either is lost there, the intent is committed and the task never exists, and nothing looks for it: `recover_expired_leases` recovers a task whose *lease* expired, which presupposes a task.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.
