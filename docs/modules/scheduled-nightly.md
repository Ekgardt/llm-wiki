# Nightly consolidation pass — started at 03:00 by the installed scheduler

`scripts/scheduled_nightly.py` and the tests that exercise them most.

The scheduler is Task Scheduler on Windows, a user LaunchAgent on macOS, a user systemd timer on Linux, cron as the explicit degraded fallback (`install_control.py`). The pass runs, in order: capture-intent adoption, runtime reclaim, the deferred memory queue, pending session consolidation; user-turn keying and the compile (an owned child whose exit is observed directly); then the steps that read the compile's output — structural lint, backlink repair, registered-repository refresh and retention, generation pruning, orphaned-checkpoint clearing, LSP failure-evidence retirement, retirement of the memory's own call transcripts, retirement of old benchmark runs, model weights, the bounded memory-generation refresh, telemetry compaction and the health report; last, report pruning and the bounded fast-forward of the checkout. The log numbers the steps in the order they ran (`StepLog`). Never requires user interaction. All output goes to $LLM_WIKI_STATE_ROOT/logs/nightly-YYYY-MM-DD.md.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.
