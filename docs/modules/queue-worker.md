# The queue worker: claiming tasks, running processors and settling their outcomes

`scripts/llm_wiki/queue_worker.py`, split out of `scripts/queue_core.py`, which re-exports the names its
callers read.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.
