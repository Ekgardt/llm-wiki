# Queue source identity contract

`llm_wiki.queue_sources` owns pure extraction and validation of identities from
queue payloads. It imports stdlib and the SQLite value contracts and performs no runtime IO.
The source-identity map, collectors, daily-source validators and generic task-link
validators have one implementation. The distinction matters: failure identities
must name a canonical real daily date; generic source links may name other
relative vault files. Existing byte limits and errors are preserved.

The migration parser requires one distinct identity and constructs coded errors
through sqlite_contracts; ordinary failure recording still chooses the first
encountered pair. A JSON parser accepting a value does not prove authorization,
file existence or committed Markdown. SQL and lease transitions remain in queue.

MemoryQueue's three existing static methods are exact aliases, preserving callers
in the queue and its adopted subclass. Other old names are direct compatibility
aliases. Remove an alias only when all supported callers have migrated; no second
implementation or forwarding state is retained. Boundary tests forbid the return
of function definitions to memory_queue. Integration qualification covers aliases
and migration error types separately from this isolated module's own tests.

Run `uv run pytest tests/modules/queue_sources -q -s` for the owned contract.
The context manifest includes every own test and the obligatory instructions.
Source: ../research/2026-11-02-queue-sources-boundary.md.

## Canonical ownership reference

The queue source-identity boundary is `llm_wiki.queue_sources`: pure payload
identity collection, daily-source validation, and generic source-link validation.
Strict migration identity parsing is also owned here; sqlite_contracts constructs
migration refusals. SQL remains with queue/migration owners. Existing static
method entry points are identical function aliases; no second implementation.
Daily-only failure identities and general task source links retain distinct rules.
