# SQLite connection IO

`llm_wiki.sqlite_io` owns connection lifetime, directory preparation, actual
SQLite locking probes, single-attempt stranded-journal recovery and streamed scans.
Successful opens transfer connection ownership to the caller. Failed opens close
their connection. SQLite owns journal recovery; no journal is deleted directly.
The root initializer may create run/; a database-directory check never does.
Probe cleanup refuses a replaced directory and preserves foreign files.
Read-only opens use metadata validation so existing process locks are retained.

Dependencies are the runtime environment, metadata and value owners, SQLite
contracts/pragmas/deadlines, and path_ancestry's ancestor check. None imports the
queue, transaction coordinator or former facade. The facade exports identical
objects, including private diagnostic names used by doctor; public function
pickle paths stay reliable_memory. Remove aliases only after consumers and
historical serialization contracts no longer require them.

Batch size bounds memory and stop-check spacing, never total rows. Replay timeout
retains its historical rationale. No new limit, SQL or storage layout is added.

Run `uv run pytest tests/modules/sqlite_io -q -s` plus the stranded-journal,
lock-probe, operational-database and doctor integration regressions. The packet
contains all lower source dependencies; broader integration fixtures run separately.
Source: ../research/2026-11-03-sqlite-io-boundary.md.
