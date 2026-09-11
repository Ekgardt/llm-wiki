# A row carries its page, not the trace

Date: 2026-09-11. Trigger: audit finding M7, measured at the MCP boundary
(a `recall` call with `limit=5` through `_execute_tool_call` on
2026-09-11): every row carried 32 keys and 1 112 bytes, of which the page
content was 55 bytes; twelve of the keys were the retrieval trace repeated
per row (`requested_mode`, `effective_mode`, `signals_used`,
`fallback_reason`, `generation`, `partial`, the six `reranker_*`) and
thirteen were score bookkeeping (`bm25_*`, `vector_*`, `graph_*`,
`rrf_score`, `rerank_score`, `final_score`). The envelope already carries
the trace once under `retrieval_trace`.

## Sources

1. Rule 4: a system that uses an LLM in operation spends tokens sparingly;
   a ten-row answer repeating twelve identical values ten times is the
   opposite.
2. `mcp_server._retrieval_trace`: the trace is reported by the search
   itself and only recovered from rows when nothing reported it — so the
   rows can drop it once the trace is built.
3. The consumers of the score fields in `scripts/` are the retrieval and
   search modules themselves (fusion, tests of fusion); the MCP client is
   an agent that ranks by `score` and reads `path`, `title`, `summary`,
   `content`.

## Decision

`recall` rows through MCP keep `candidate_id`, `path`, `title`,
`summary`, `content`, `score`, `vector_score` (the one per-signal score the
envelope's quality reads when no trace was reported), `chunk_id`,
`heading_ancestry`, `project`, `timestamp`, `source_sha256`; the trace and the per-signal scores are
dropped from rows after the envelope's trace is built. `search_memory`
and `retrieval` keep their full rows for their own callers. Measured
after the change on the same call: a row falls from 1 112 to 541 bytes and
the whole envelope from 8 849 to 4 996 (the content is unchanged).

Files: `scripts/mcp_server.py`, `tests/test_mcp_server.py`, `CHANGELOG.md`,
`docs/AUDIT-2026-09-10-memory-and-retrieval.md`.
