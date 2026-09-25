# The health resource is readable

Date: 2026-09-25. Found while checking the live memory server after the encoder
change.

## Question

Reading `llm-wiki://health` from Claude Code failed with
`'TextResourceContents' object has no attribute 'content'`. Where does it fail,
and what should the handler return?

## Sources

- MCP specification 2025-06-18, "Resources" (fetched 2026-09-25,
  https://modelcontextprotocol.io/specification/2025-06-18/server/resources): a
  `resources/read` result is `{"contents": [{"uri", "mimeType", "text"}]}`.
- The installed MCP Python SDK 1.29.0, `mcp/server/lowlevel/server.py`,
  `Server.read_resource`: the decorated handler returns `str`, `bytes` or
  `Iterable[ReadResourceContents]`, and the SDK builds each protocol content
  from `content_item.content` and `content_item.mime_type`.
  `ReadResourceContents(content, mime_type=None, meta=None)` lives in
  `mcp.server.lowlevel.helper_types`.

## Findings (facts)

1. A fresh server driven over stdio answers `resources/read` for
   `llm-wiki://health` with the JSON-RPC error `code 0`,
   `'TextResourceContents' object has no attribute 'content'`. The error comes
   from the server, not the client.
2. `mcp_server._register_resources` returns `[TextResourceContents(uri=...,
   mimeType=..., text=...)]`, the protocol model, where the SDK expects its own
   helper type. Neither the health nor the context resource could be read.
3. The two unit tests of the handler replace `TextResourceContents` with a
   stand-in, so they never met the SDK's real reading of the value.

## Decision (conclusion)

- The handler returns `[ReadResourceContents(content=text,
  mime_type="application/json")]`; the SDK builds the protocol content and its
  `uri` itself.
- Resource support is decided by the presence of `ReadResourceContents`, not of
  the protocol model the handler no longer uses.
- A test reads both resources through a real stdio session with the real SDK.

## Edited files

- `scripts/mcp_server.py`
- `tests/test_mcp_server.py`, `tests/test_the_resources_are_readable.py`
