# The tool list has a token budget

Date: 2026-09-26. Audit 2026-09-26, finding C-10 (tool schemas about 23 KB, the
envelope `outputSchema` repeated for all twelve tools).

## What was measured

Measured 2026-09-26 from `mcp_server._build_tool_definitions()`: the twelve tools
serialise to 21 262 bytes on the wire. Of these, 9 486 bytes are names,
descriptions and input schemas; the rest is the same 1 062-byte envelope
`outputSchema` twelve times.

## Decision

- The output schema stays on every tool. MCP defines it per tool, a server that
  provides one must conform to it, and clients should validate against it; there
  is no cross-tool reference to share one copy.
- It is not what a Claude model reads. A Claude tool definition carries `name`,
  `description`, `input_schema` and optional `input_examples`; there is no output
  field. So the 12 KB of repetition is wire bytes, read once per session by the
  client, not prompt tokens. I did not verify what other clients (Codex,
  OpenCode) put in their prompt.
- The model-facing part gets a budget: a test fails when names, descriptions and
  input schemas together exceed 10 KiB, so the part that costs tokens cannot grow
  without a deliberate change of the number. The same test checks the list still
  holds every tool.
- The descriptions are not cut: the same documentation calls detailed
  descriptions "by far the most important factor in tool performance".

## Source

Claude API documentation, "Define tools", fetched 2026-09-26 from
https://platform.claude.com/docs/en/agents-and-tools/tool-use/implement-tool-use
(served from `.../define-tools`):

- A user-defined tool definition includes `name`, `description`, `input_schema`,
  and "(Optional) An array of example input objects".
- "**Provide extremely detailed descriptions.** This is by far the most important
  factor in tool performance."

Model Context Protocol specification, Tools, revision 2025-11-25, fetched
2026-09-26 from https://modelcontextprotocol.io/specification/2025-11-25/server/tools:

- "`outputSchema`: Optional JSON Schema defining expected output structure"
- "If an output schema is provided: Servers **MUST** provide structured results
  that conform to this schema. Clients **SHOULD** validate structured results
  against this schema."

## Files

- `tests/test_the_tool_list_has_a_token_budget.py`
