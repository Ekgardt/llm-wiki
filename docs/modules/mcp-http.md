# One shared local MCP server over HTTP, for many agents at once (OPS-01)

`scripts/mcp_http.py` and the tests that exercise them most.

Why this exists. Every agent on this machine starts its own stdio MCP subprocess, and each one pays the same cold start: the semantic encoder alone costs 7.99 s and ~1.1 GiB resident per process (`knowledge/log.md`, 2026-08-26). One resident server pays that once and every agent shares it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.
