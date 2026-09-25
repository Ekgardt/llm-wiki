# The memory server reloads its own code

Dated 2026-09-24. Audit items A-5 and C-9 (`docs/AUDIT-2026-09-24-live.md`).

Files: `scripts/mcp_supervisor.py` (new), `scripts/mcp_server.py`,
`tests/test_the_memory_server_reloads_its_own_code.py` (new), `docs/STRUCTURE.md`,
`docs/ARCHITECTURE.md`, `CHANGELOG.md`,
`docs/research/2026-09-24-the-memory-server-reloads-its-own-code.md`.

## What was found

- The stdio MCP server lives as long as the Claude Code session that started it. The live
  one (pid 4105477) started 2026-09-23 10:41:58; commit 8a6e0d52, which added
  `bounded_io.IO_CHUNK_BYTES`, reached the checkout at 11:07. The process kept the old
  `bounded_io` in memory and imported the new `evidence_graph` lazily, so every
  `get_architecture` answered `operation_failed`; the real cause
  (`ImportError: cannot import name 'IO_CHUNK_BYTES'`) appeared only in
  `logs/capture-failures.jsonl`. The nightly fast-forward makes this routine: 25 commits
  reached the checkout after that process started.
- The envelope's `source_commit` is read from disk, so the server named a commit it was
  not running.
- `mcp_server.py --help` starts the server (it has no argument parsing).

## Practice on this date

- MCP stdio: "Messages are delimited by newlines, and MUST NOT contain embedded
  newlines", and "The initialization phase MUST be the first interaction between client
  and server"; a cancelled request may get no response (MCP specification 2025-06-18,
  Transports and Lifecycle, fetched 2026-09-24). A process that sits between the client
  and a server can therefore forward whole lines, keep the one `initialize` exchange, and
  replay it to a fresh server without the client seeing a second session.
- A long-running Python service picks up new code by restarting a worker process under a
  supervisor that owns the external connection — the reloader pattern of development
  servers such as uvicorn's `--reload`. Reloading modules inside one process
  (`importlib.reload`) leaves exactly the old/new mixture seen here.

## The decisions

1. `scripts/mcp_server.py`, run as a program, is a small supervisor
   (`scripts/mcp_supervisor.py`) unless `LLM_WIKI_MCP_WORKER=1`; the registered command
   does not change. The supervisor imports only the standard library.
2. The supervisor starts the real server as a child process with that variable set and
   forwards newline-delimited JSON-RPC in both directions. It remembers the client's
   `initialize` request and `notifications/initialized`, tracks the ids of requests in
   flight (a `notifications/cancelled` removes its id), and fingerprints the code (every
   `.py` and `.json` under `scripts/`, by path, size and modification time).
3. When a client request arrives, nothing is in flight and the fingerprint changed, it
   stops the child (closes its stdin, waits, then terminates), starts a new one, replays
   `initialize` under a private id whose response it swallows, replays
   `notifications/initialized`, and forwards the request. Lines from a stopped child are
   dropped.
4. If the child exits on its own, every request in flight is answered with a JSON-RPC
   error naming the exit, and the next request starts a new child. The client's own
   shutdown (stdin closed) stops the child and ends the supervisor.
5. `--help` prints usage and exits without starting anything.

## Limits (rule 3)

The first request after new code arrives pays one server start (about 2 s, plus the
encoder warm-up the server already runs in the background). A request that is in flight
when code changes is served by the old child; the reload waits for the next idle moment.
The HTTP transport (`--http`, a separate process per run) is not supervised.

## Sources

- Model Context Protocol specification 2025-06-18, "Transports" —
  https://modelcontextprotocol.io/specification/2025-06-18/basic/transports — and
  "Lifecycle" — https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle —
  fetched 2026-09-24.
- `logs/capture-failures.jsonl` and `ps -o lstart` of the live server, 2026-09-24.
