# Benchmark MCP stdio client

Owns the existing `benchmark/mcp_stdio_client.py` at its current path. Its full
source stayed byte-identical during initial ownership qualification; the later
UTF-8 repair is recorded below. This is a synchronous,
one-shot benchmark adapter, not the product MCP server or a general MCP SDK.
Initialize, initialized notification, tool call/list, response extraction and
cleanup retain their existing command/API, protocol version and output shapes.
It spawns the supplied command with inherited environment and optional cwd, without
a shell; stderr is discarded. No installation, provider call or network request
is necessary for local protocol qualification.

## Existing behavior and unresolved limits

Replies match the requested ID; unrelated messages and malformed/non-object lines
are skipped. UTF-8 decode replacement, textual block joining, JSON fallback,
error truncation and result truncation stay unchanged. This tolerance does not
establish full JSON-RPC/MCP validation. Benchmarks using malformed peers are not
proof of standards conformance.

The `timeout` parameter of `call_tool` is currently ignored by blocking IO;
`list_tools` has no deadline, including when the CLI accepts `--timeout`.
This defect was reproduced before qualification. The nominal 8 MiB preamble
counter is checked after an unbounded readline and is not a strict allocation
bound. Cleanup waits ten seconds, then kills only the direct child and waits ten
more; it does not own a descendant tree. Reported call seconds exclude cleanup.
These are unresolved implementation defects/limitations, not guarantees or
accepted target design. A source-preserving ownership change does not fix them.
Tests do not enshrine ignored deadlines as desirable behavior or claim timeout
safety. Future repair must cover cancellable reads/writes and owned cleanup; an
unjoined timeout thread is not a repair.

## Research and qualification — 2026-11-04

Checked [Python subprocess](https://docs.python.org/3.14/library/subprocess.html),
[MCP 2025-06-18 transport](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports),
and [JSON-RPC 2.0](https://www.jsonrpc.org/specification). These distinguish framing,
request correlation and process lifetime. The original MCP version is retained;
this is not a claim it is the latest version. Replacing the adapter with the
already-installed MCP SDK is an alternative, but changes parsing, environment and
negotiation and needs separate compatibility evidence. No dependency is added here.

Qualification uses finite real local subprocesses, independent wire examples,
error/result cases, isolated imports, source hash identity, actual complexity,
measured test weights and exact complete maintenance context. Whole-project
completion, corrected transport limits and full-cycle token savings are not claimed.


## UTF-8 CLI repair (2026-11-04)

Windows CI55eb6f95 exposed a non-UTF-8 finite-server fixture; a separate old-source
probe reproduced the benchmark CLI's UnicodeEncodeError under cp1252 even with
a valid UTF-8 server. The fixture explicitly reads/writes UTF-8, and the CLI entrypoint explicitly
configures stdout as UTF-8. Library call_tool/list_tools behavior and binary wire
encoding are unchanged. Real Unicode requests, replies and tool descriptions are
checked with both normal and forced-cp1252 process defaults. Original assertions
remain; this is an encoding repair, not deadline/tree-cleanup qualification.
Full evidence and primary references: ../research/2026-11-04-mcp-stdio-utf8-output.md.
