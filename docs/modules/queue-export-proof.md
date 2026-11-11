# Queue export proof

`llm_wiki.queue_export_proof` decides whether a purge export or a corrupt-task package still proves what it recorded: its manifest, record lists, byte digests and owner-only files. It reads files and raises the queue errors; it changes no queue state. `queue_core` restores from a package only after these checks pass.

Own tests are under `tests/modules/queue_export_proof`. The owned files and the complete maintenance
context are listed in the `-context.json` manifest next to this guide.
