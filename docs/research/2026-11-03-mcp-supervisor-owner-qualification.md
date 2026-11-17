# Existing MCP supervisor owner qualification

Date: 2026-11-03. Qualify scripts/mcp_supervisor.py in place, preserving every
production byte and the registered command. Its 327 lines import only stdlib;
its external boundary is the existing newline-delimited JSON-RPC child process.
It does not own tool implementations, model dispatch, HTTP MCP or installation.
The actual worker path and worker environment flag remain unchanged. A fresh
built-in index follows 725e44ba before code; source and both consumer tests have
been read in full. The existing two test files pass all eleven cases, including
the real mcp_server child listing twelve tools through the supervisor.

Three independent primary sources checked today:
- MCP's product-tested 2025-06-18 stdio transport contract:
  https://modelcontextprotocol.io/specification/2025-06-18/basic/transports
  Messages are UTF-8 JSON-RPC lines; stderr is separate from protocol stdout.
- JSON-RPC Working Group's 2.0 specification:
  https://www.jsonrpc.org/specification
  Request IDs correlate responses; notifications have no response. This is an
  established protocol, not a claim that its dated specification is a new release.
- Python's project-supported 3.10 subprocess API:
  https://docs.python.org/3.10/library/subprocess.html
  Popen supplies persistent pipes and explicit process waits. Upstream labels
  3.10 unsupported; preserving the project's version requirement does not assert
  upstream security support. No API/dependency/protocol upgrade is proposed.

The existing process boundary is preferable to a new wrapper, generic process
framework or in-process module reload. It preserves the one supervisor connection
while replacing worker code at an idle request. Fingerprints retain their current
path/size/mtime meaning; they are not cryptographic proof of a loaded code image.
Graceful/terminate/kill intervals retain their existing worker-settlement rationale.
This qualification does not silently alter framing, IDs, cancellation or restart.

Test transfer: move the seven protocol/fingerprint/help tests and their original
fake-child harness from test_the_memory_server_reloads_its_own_code.py into the
owner packet. Keep its actual-server end-to-end test and line reader in place.
Move the vanished-file and graceful-wait unit tests from
 test_the_supervisor_lets_the_worker_finish.py into the same owner. Keep its actual
worker/supervisor timeout comparison in place. Preserve every moved function/class
AST and every remaining integration function AST; the fake child's source bytes
also remain exact. Remove only now-unused imports. No assertions disappear and no
integration test is replaced by a fake. The original two integration obligations
remain explicit unassigned files until their full consumer contexts are qualified.

The packet includes full source, moved tests/harness, both historical decisions,
new qualification guide/research, bootstrap/instructions and full measurement
helpers. Add static and guarded-process isolation checks, then run original plus
moved tests on Python 3.12/3.10, real-child integration, global contracts and all
actual bundle round-trips. Moving tests alone does not establish whole-project
completion or reduced useful LLM task cost. No live worker will be restarted.


## Qualification evidence

Fresh built-in generation-18e475443e753cb2-195fc4f0 matches 725e44ba. Caller queries
resolved four code_fingerprint, three stop_process and one _reload_due callers;
the graph is partial, complemented by the complete source/consumer inspection.
The exact transfer contains nine tests plus the two harness classes, fixture and
fake-server assignment (13 top-level nodes). Original integration function ASTs
and production bytes remain identical; only unused imports and top-level/EOF
whitespace were cleaned. The move itself passes all original eleven cases.

The missing-manifest budget test failed as expected before registration; the two
isolation guards already passed for the existing independent implementation.
Final protocol, real-child integration and boundary/budget checks: 14 passed on
Python 3.12 and Python 3.10. Global module/policy checks: 1733 passed / 2 skipped
in 154.27 seconds. Ruff and actual CCN/shape analysis pass. All 69 emitted packets
round-trip; initial supervisor size is 66078 tokens and the maximum is 96390.
The final documentation is remeasured before commit. Ownership remains incomplete:
557 / 3277 tracked files owned, 2720 unassigned. Both integration test files remain
unassigned, preserving their full consumer-context obligation.

Evidence: private mcp-supervisor-originals hashes/AST transfer record,
mcp-supervisor-owner-{baseline,after-move,green,py310,global,bundles}.txt and current
ownership inventory. Full 725e44ba is running independently; it is not a full run
of these relocated tests. No current native Windows/macOS or useful model-cycle
claim is made. Production code, live worker and installed configuration are unchanged.
