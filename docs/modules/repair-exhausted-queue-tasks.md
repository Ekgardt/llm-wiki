# Retire a task that used every attempt and was left looking ready

`scripts/repair_exhausted_queue_tasks.py` and the tests that exercise them most.

`failure_state` used to send an ordinary failure back to `ready` whatever the attempt count was, so a task that failed on its last allowed attempt kept the state of work still to do and the attempt count of work already given up on. The worker will not claim it, because the budget is spent. `redrive` will not take it, because redrive requires a dead task. Nothing else looks at it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.
