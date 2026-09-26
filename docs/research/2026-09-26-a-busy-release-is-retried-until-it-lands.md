# A busy release of the canonical writer gate is retried until it lands

Date: 2026-09-26. Audit 2026-09-26 A-13.

## Facts

- `MarkdownCoordinator._leave_canonical_gate` (scripts/markdown_transaction.py)
  deletes the `writer_owners` projection and releases the registry lease in one
  immediate transaction, once. SQLite's busy handler (https://www.sqlite.org/c3ref/busy_timeout.html,
  fetched 2026-09-26): "After at least "ms" milliseconds of sleeping, the handler
  returns 0 which causes sqlite3_step() to return SQLITE_BUSY." So a database busy
  longer than `markdown_busy_ms` (10 s) made the release raise, the rows stayed,
  and the heartbeat had already stopped.
- The canonical registry reclaims only an owner proven dead (CLAUDE.md: live and
  expired-but-not-proven-dead owners block). The process that failed its release
  is alive, often a long-lived MCP server, so every other writer timed out until
  that process exited.
- The nested gate already records a failed release; the canonical one did not.

## Decision

- A release that fails with transient contention (`_is_transient_writer_contention`)
  is retried by a daemon thread of the same process, after 1, 2, 5, 10 and 30 s
  and then every 60 s, for about an hour; the first success, or an error that is
  not contention (the lease was already fenced away), ends it. No persistent
  daemon is added: the thread lives only as long as the process that holds the row.
- The caller's operation, which already committed, is no longer reported failed
  by a busy release; any other release error still propagates.

## Files

- `scripts/markdown_transaction.py`
- `tests/test_a_busy_release_is_retried_until_it_lands.py`
- `CHANGELOG.md`
