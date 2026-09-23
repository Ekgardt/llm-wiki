# Legacy that nothing reads

Dated 2026-09-23. The owner asked for the legacy to be cleared. This note removes the
legacy paths that no supported vault can reach, and states, with the evidence, which
legacy paths remain and what removing each one would change.

Files: `scripts/access_tracking.py`, `scripts/impact_analysis.py`,
`scripts/evidence_graph_builder.py`, `tests/test_access_tracking.py`,
`tests/test_evidence_graph_incremental.py`, `tests/test_legacy_that_nothing_reads.py`,
`docs/research/2026-09-23-legacy-that-nothing-reads.md`.

## What was found

- `access_tracking` still reads `cache/access_log.jsonl`, the pre-telemetry access log,
  and merges it into `get_access_stats`. Nothing writes that file any more; it does not
  exist on the live vault; `retrieval_telemetry` has carried access events since
  2026-08-20 (15 284 events on the live vault). Two tests and two limits exist only for it.
- `impact_analysis.analyze_impact` accepts a positional `git_range` ("a..b") and rewrites it
  into `comparison="two-commits"` through `_legacy_endpoints`. Every caller — the CLI, the
  MCP tool, session start, six tests — passes `comparison`/`base`/`target` by name; nothing
  passes a range.
- `evidence_graph_builder` reads incremental manifests of versions v1–v4 beside the v5 it
  writes: version sets, a v3-only `workspace_sensitive_sources` validator with its own
  10 000 bound, and version branches in the entry writers. All nine generations on the
  live vault are v5; a fresh install builds v5; nothing else ever wrote a manifest here.
  One parametrised test builds v1–v3 parents by monkeypatching the writer's version.
- A parent whose manifest cannot be validated raises out of `_incremental_parent_state`
  and fails the whole build (`_load_incremental_manifest` is called with no handler at
  line 1825). That is true today for any malformed parent, not only for an old version.
- The larger legacy stays, for reasons stated here rather than guessed:
  - The legacy FTS index (`cache/index.sqlite`, `.paths-manifest`, `vectors.npy`) answered
    537 of 15 284 retrievals on the live vault, the last on 2026-09-05; since then every
    answer came from a generation. It is still the only answer on a fresh install before
    the first nightly builds a generation, because `install.sh` adopts and fetches models
    but builds no generation. Removing it means the installer builds the first generation
    (about two minutes with the models fetched), which is a change to what an install does.
  - The v2 queue and coordinator readers, the JSON queue migration (`run/queue/*.json`),
    the `queue-migrated-v2` marker, the retired v2 databases and their tombstones: the
    live vault adopted v3 on 2026-08-26, `run/queue/` does not exist, and every install
    adopts before first use. Their removal touches the adoption record's own validation
    (`_LEGACY_EVIDENCE`, the upgrade path that verifies the retired database's digest) and
    about 530 lines of `memory_queue`, 100 of `doctor`, part of `installed_memory_repair`
    and eleven test files. Rule 4's "break nothing" puts that behind its own note and its
    own full-suite run.
  - `flush_memory._legacy_ok` (a bare `FLUSH_OK` answer), `integration_adapter`'s
    `legacy_context` field and `_legacy_output` (the session-start hook's output shape),
    `build_tiers.tier_legacy_cache_path` and `mcp_server`'s `"legacy"` generation label
    are live protocol or live data compatibility, not dead code; their names say "legacy"
    about a format, not about the path.

## Practice on this date

- Dead code is removed, not commented or flagged; a removal is safe when nothing reaches
  it, which is what the callers and the data on disk establish above
  ([Martin Fowler, "Dead Code" refactoring, Refactoring 2nd ed.](https://refactoring.com/catalog/removeDeadCode.html)).
- A build must not fail on a parent it merely cannot reuse; reuse is an optimisation and
  the full build is the correct fallback (the same rule the builder applies when the
  parent has no incremental manifest at all).

## The decisions

1. The legacy access log reader and its two limits go; `get_access_stats` reads telemetry
   only. The CLI stops naming a file that does not exist.
2. `git_range` and `_legacy_endpoints` go; `analyze_impact` takes its endpoints by name only.
3. The builder reads and writes `evidence-graph-incremental/v5` only; the v1–v4 sets,
   the v3 validator and the version branches go. A parent whose manifest fails validation
   is treated like a parent with no manifest: no reuse, a full build, and the result
   shows it in `reused_sources == ()`.
4. The three larger removals above are the next stage, each with its own note.

## Cost, by rule 4

Fewer branches on the build and stats paths; no runtime cost added.

## Sources

- [Remove Dead Code — Refactoring catalog](https://refactoring.com/catalog/removeDeadCode.html) — fetched 2026-09-23.
- `cache/evidence-graph/telemetry.sqlite3` (read-only), `cache/evidence-graph/generations/*/incremental-manifest.json`, `run/` on the live vault, 2026-09-23.
