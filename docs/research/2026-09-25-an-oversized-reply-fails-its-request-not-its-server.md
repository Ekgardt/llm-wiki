# An oversized reply fails its request, not its server

Date: 2026-09-25. Audit item C-38 (docs/AUDIT-2026-09-25-full.md).

## Fact
- `lsp_protocol._content_length` raises `ProtocolViolation("LSP frame exceeds 8 MiB")`
  for a frame over `MAX_FRAME_BYTES`; the reader loop turns it into `_become_fatal`,
  so the whole generation dies. `lsp_process._serve_lsp_request` then replays the
  request on the new generation, which answers the same way: the second fatal
  failure makes the process terminal and writes failure evidence.
- The same shape holds one level up: a well-framed response whose result breaks a
  client bound (`location result exceeds 10,000 items`, a hover over 256 KiB, more
  than 10 000 document symbols) goes through `_handle_response` → `_become_fatal`.
  A `publishDiagnostics` notification over 10 000 items is fatal too.
- `_serve_lsp_request` re-raises a `ProtocolViolation` without failing the
  generation when the protocol is not fatal (`_objectively_fatal`), so a request
  can be refused on its own while the server stays.

## Source (fetched 2026-09-25)
Language Server Protocol 3.17 specification,
https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/:
- Base protocol header: "Content-Length | number | The length of the content part
  in bytes. This header is required." — the length of an oversized frame is known
  before its body is read, so the body can be consumed and the stream stays framed.
- Error codes: "RequestFailed ... A request failed but it was syntactically
  correct, e.g the method name was known and the parameters were valid." — a
  failure of one request is a per-request outcome in the protocol itself.

## Decision
- A result that breaks a client bound refuses that request with
  `ResponseRefused` (a `ProtocolViolation`), raised to its caller; the connection
  stays.
- A frame over 8 MiB and up to 256 MiB is consumed in 64 KiB chunks and
  discarded. Its first and last 256 bytes are kept: a frame whose head names no
  `"method"` is a response, and its id is read from `{"jsonrpc":"2.0","id":N` at
  the head (vscode-jsonrpc, lsp-server) or `"id":N}` at the very end (Go's field
  order). That request is refused; a frame whose id cannot be read is dropped
  with a warning and its caller waits out its own deadline.
- A diagnostics notification over 10 000 items is dropped with a warning.
- Content-Length above 256 MiB and malformed shapes stay fatal: those are a
  broken peer, not a large answer.

## Uncertainty
The id recovery relies on the three servers' serialisation order (inferred from
their libraries, not measured here); a miss costs only the caller's deadline.

## Files
- scripts/lsp_protocol.py
- tests/test_lsp_protocol.py
- tests/test_an_oversized_reply_fails_its_request_not_its_server.py
