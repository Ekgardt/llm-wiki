# Queue compile recovery

`llm_wiki.queue_compile_recovery` runs the operator command that settles a compile whose answer is uncertain: it holds a fenced owner, claims the work, keeps its lease alive while the call is resolved, and settles or aborts under that owner. The queue, the call resolver and the owner heartbeat are passed in by the `compile-recover` command in `queue_core`, so this module does not import the compile runtime or the ownership registry.

Own tests are under `tests/modules/queue_compile_recovery`. The owned files and the complete maintenance
context are listed in the `-context.json` manifest next to this guide.
