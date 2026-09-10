# An empty answer still names its index — 2026-09-10

**Question.** Issue #26: when `recall` finds nothing, the envelope reports
`fallback_reason: trace_unavailable`, `corpus_generation: legacy`,
`partial: true`. A user who had just published a note and got an empty
result could not tell whether the search failed, timed out, or simply ran
against a generation built before the note existed. What should an empty
result carry?

**What the field does.**

- Elasticsearch and OpenSearch answer an empty query with `hits.total: 0`
  *and* the same `_shards` block and `timed_out` flag as a non-empty one;
  zero hits is a complete answer from a named index, not a missing trace.
  Source: https://www.elastic.co/guide/en/elasticsearch/reference/current/search-search.html
  (response body: `timed_out`, `_shards`, `hits.total`).
- OpenTelemetry's span status convention leaves status `Unset` for an
  operation that completed without error, and reserves `Error` for a
  failure; an empty result is not an error and must not be recorded as one.
  Source: https://opentelemetry.io/docs/specs/otel/trace/api/#set-status
- The MCP tool-result contract separates `isError` from content; an empty
  content list with `isError: false` is the normal shape of "nothing found".
  Source: https://modelcontextprotocol.io/specification/2025-06-18/server/tools#tool-result

**What we keep.** Our trace already records the generation on every row a
search returns (`search_memory` writes `generation` per row; the legacy
paths write the literal `legacy`). The active generation is one catalog
read (`GenerationCatalog.get_active`), already used by `freshness_watch`
and `doctor`.

**Decision.** On an empty result the trace names the *selected* generation
(the active one, or `legacy` when there is no catalog), sets
`fallback_reason: no_results`, and `partial: false`. A deadline that
produced zero rows is still reported as `retrieval_deadline_exceeded` by the
row path when any lexical row survives; when nothing survives it is
indistinguishable from an honest miss and the trace says `no_results` — this
limit is stated, not hidden. The freshness contract: components inherit the
trace's generation; a signal that was not used reads `missing`, never
`fresh`, so "no rows from an old generation" stays visibly stale until the
nightly or an idle refresh publishes a newer one.

**Not done.** No new field, no second lookup path, no change to the
non-empty trace.
