# Queue artifact IO

`llm_wiki.queue_artifacts` owns queue result/export file operations. It opens
exclusive temporary files, streams producer output, flushes bytes, applies owner
permissions, links without overwrite, verifies same-content conflicts, and
cleans only the existing permitted staging paths. Stable reads preserve the
observed extent and before/after descriptor/path identity checks. The sequential
writer reports short writes and maintains its digest/size without buffering the
whole artifact. Deadlines remain cooperative checks around existing operations.

Dependencies are queue value errors, SQLite deadline checks, runtime permission
and sync policy, and Windows ACL verification. The module never imports the
queue executor or compile progress. The queue owns authorization, task state,
SQL transactions, leases and compile-specific error mapping. Compatibility names
remain identical aliases; delete them only once supported callers/probes no
longer use them. There is no duplicate implementation.

This extraction does not replace the generic runtime publisher: queue exports
have existing streaming and domain-error semantics. It preserves create-only
conflicts, cleanup, file modes and platform behavior, with no new size bound or
runtime path. File/directory flush guarantees still depend on the OS/filesystem.

Own tests exercise actual disk writes, same/different-content publication,
producer failure cleanup, deadline refusal, bounded reads, file substitution and
metadata protection. Queue/capture/adoption tests exercise callers separately.
Run `uv run pytest tests/modules/queue_artifacts -q -s`.
Source: ../research/2026-11-03-queue-artifact-io-boundary.md.
