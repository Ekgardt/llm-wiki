# Explicit SQLite transactions

`llm_wiki.sqlite_transactions.begin_immediate(connection, before_commit=None)`
starts BEGIN IMMEDIATE, yields the caller's connection, runs the optional
precommit check, then commits. Any BaseException rolls back and re-raises the
original exception; if rollback also fails, that failure remains its context.

It opens/closes no connection, chooses no runtime path and performs no retries.
Connection lifetime, ownership/admission, deadlines and whether nested use is
appropriate remain caller responsibilities. There is no new transaction policy:
the existing function was moved unchanged from reliable_memory. Schema repair,
queue settlement, Markdown coordination and telemetry use the same primitive.

reliable_memory's old name is an identity alias. The decorated function and its
wrapped function retain the historical reliable_memory module name for existing
serialized lookups/introspection; those aliases remain while historical values
are supported. No second implementation or forwarding wrapper exists.

Owned tests preserve the previous three precommit/rollback assertions and cover
KeyboardInterrupt, commit failure and isolated import. Operational-file locking,
queue/migration, admission and transaction-recovery tests remain integration
qualification. The full owned packet includes all tests, fixture, instructions,
pytest bootstrap and framing; dependency-contract consumers can read this guide
and the complete small source without loading the filesystem implementation.

Run `uv run pytest tests/modules/sqlite_transactions -q -s`.
Source: ../research/2026-11-02-sqlite-transactions-boundary.md.
