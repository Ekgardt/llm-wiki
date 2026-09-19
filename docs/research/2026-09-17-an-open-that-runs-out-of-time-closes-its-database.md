# An open that runs out of time closes its database

Dated 2026-09-17. Third audit, finding G-M7 (second point). The research before the fix.

Files: `scripts/evidence_graph.py`,
`tests/test_an_open_that_runs_out_of_time_closes_its_database.py`.

## What was found

- `EvidenceGraph._opened_active_once`, `_opened_code_generation` and
  `_opened_repository_once` open the graph database and then re-check the generation's seal
  under the caller's deadline. Their `except` closes the graph for
  `FileNotFoundError, PermissionError, TypeError, ValueError, sqlite3.Error` — and a stop is
  none of those: the seal check raises `TimeoutError`, which passes through with the
  connection still open.
- Reproduced with a real code generation and a cancellation that arrives at the n-th time
  the open asks about it, for every n until the open gets through: after the stopped opens,
  `/proc/self/fd` still holds `evidence.sqlite3` descriptors, for both
  `open_code_for_repository` and `open_active_for_repository`.
- Deadlines are routine under MCP, and the server is one long process: each leak is a file
  descriptor and an SQLite connection held until exit.

## Practice on this date

- Python's sqlite3 documentation: "The context manager neither implicitly opens a new
  transaction nor closes the connection. If you need a closing context manager, consider
  using contextlib.closing()." and "Changed in version 3.13: A ResourceWarning is emitted if
  close() is not called before a Connection object is deleted."
  (https://docs.python.org/3/library/sqlite3.html, fetched 2026-09-17). Closing is the
  opener's job on every exit, the stopped one included.

## The decision

- The three openers close the graph on a stop and raise the stop again; the existing
  answers for the other errors (`_RETRY` / `None`) are unchanged. One helper closes a graph
  that may not have been opened yet, so the three sites stay the same shape.
