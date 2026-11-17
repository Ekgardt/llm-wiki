# Compile health counts the compiler’s inputs

2026-10-02. The installed health forecast counted recursive daily Markdown, including receipts and README, and archived notes. It omitted the metadata snapshots. A source-count warning therefore described different inputs from the compiler. A real readonly snapshot regression reproduced the discrepancy: 117 source bytes versus 8283 forecast bytes. Corrected regressions initially failed twice, with one passing case.

The compiler and doctor now share the existing live-note, canonical-daily and metadata selectors. The forecast considers all eligible canonical dailies; an individual compile may select fewer. Metadata follows the same docs/AGENTS.md fallback as the compiler. No source ceiling, directory contract, setting or runtime location changes. Other pipelines retain their separate scopes.

Primary sources checked on 2026-10-02:

- [Python 3.10 pathlib glob](https://docs.python.org/3.10/library/pathlib.html#pathlib.Path.glob): glob and recursive traversal have different scope.
- [OpenTelemetry Metrics Data Model, stable](https://opentelemetry.io/docs/specs/otel/metrics/data-model/): measurement meaning includes the observed entity and unit.
- [Prometheus instrumentation guidance](https://prometheus.io/docs/practices/instrumentation/): metrics need clear meanings and useful failure diagnostics.

Alternatives: raising the ceiling preserves the faulty forecast; copying the filtering rules into doctor risks drift. Sharing existing selectors keeps the forecast aligned with actual snapshot selection without importing a second policy. This reads directory entries and file sizes; it does not run compilation or call a model. Concurrent filesystem changes can still invalidate an observation, and a size forecast is not an admission or freshness guarantee.

Verification: three regression cases passed, including exact byte equality with a real readonly compiler snapshot; 316 related cases passed and three skipped. Lizard measured every changed/new function including tests: maximum CCN 5. Ruff passed. Installation and the complete regression result remain pending at this checkpoint. The first edit command failed because python was not on PATH; it made no changes, and the successful edit used python3.

The existing numerical ceilings remain separately under audit. Correcting their measurement does not prove their numerical basis.
