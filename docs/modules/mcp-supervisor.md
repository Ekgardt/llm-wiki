# MCP stdio supervisor

The existing scripts/mcp_supervisor.py owns the long-lived stdio connection,
worker process generation and request-in-flight tracking. Public symbols and the
mcp_server.py entry command stay unchanged. The child uses the existing
LLM_WIKI_MCP_WORKER flag; there is no new daemon, service or runtime location.
HTTP transport and tool/model implementations belong to their existing owners.

Client and child exchange newline-delimited JSON-RPC bytes. Initialization is
retained and replayed after restart; supervisor replay replies are private.
Cancellation removes an in-flight request. Code changes reload only at an idle
request; a worker exit answers retained in-flight IDs with the existing error and
the next request starts another worker. Late events from an older generation are
ignored. The supervisor imports only stdlib and cannot reload its own code.

Fingerprints cover Python/JSON paths, sizes and modification times, including a
gone-file marker for listing/stat races. This is the existing change detector,
not a content-hash or hostile-replacement integrity guarantee. Shutdown closes
stdin, waits for the worker's inference settlement, then escalates terminate/kill.
The existing 35/5-second constants are unchanged; the separate real-worker test
checks that graceful waiting outlasts its actual inference-settlement constant.

The packet owns the unchanged protocol/fake-child, fingerprint and graceful-wait
tests. The actual-server twelve-tool stdio test remains in
 tests/test_the_memory_server_reloads_its_own_code.py; the cross-owner shutdown
comparison remains in tests/test_the_supervisor_lets_the_worker_finish.py.
Both run as integration checks and remain visible ownership obligations. They are
not replaced by the packet's fake child or silently counted as owned here.

Run the owner packet together with those two original test files. Verify Python
3.10 compatibility and actual subprocess behavior; Linux results do not qualify
native Windows/macOS. Boundaries, source hashes, moved AST checks and primary
sources are recorded in ../research/2026-11-03-mcp-supervisor-owner-qualification.md.
