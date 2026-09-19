# One page, one identity, and one set of windows

Dated 2026-09-17. Third audit, retrieval L13 (one telemetry column, three identities) and L12
(a "half-life" that is not one, over days that disagree with the contract). Both were marked
"owner's decision"; the owner delegated deciding after the five laws.

Files: `scripts/retrieval.py`, `scripts/access_tracking.py`, `scripts/mcp_server.py`,
`tests/test_one_page_has_one_identity_in_the_telemetry.py`,
`tests/test_access_tracking.py`

## What was found — L13, the identity

- `retrieval_events` has one `candidate_id` column, and three writers put three different
  things in it: a generation-mode impression writes the chunk hash
  (`retrieval._impression_candidate_id` prefers `chunk_id`), a cited or refused event writes
  the page's vault-relative path (`query_memory._write_cited_events`,
  `_record_refused_evidence`), and `mcp_server._decision_impression_events` and
  `access_tracking.record_access` write a bare slug.
- The readers each expect one of those. `retrieval_disposition.disposition` is looked up in
  `retrieval._weigh_by_trust` by `meta[key]["relative_path"]`, so it reads the path rows and
  only those. `access_tracking` reads `read_events(candidate_id=slug)` and writes
  `access_count` into `knowledge/notes/<slug>.md`.
- So no generation-mode impression ever reaches a page's access count, and the export scan
  (`list_candidate_ids` → `KNOWLEDGE_DIR/<candidate>.md`) walks chunk hashes that can never be
  a page file, spending its bounded scan on rows it can never flush.
- A slug is also not an identity: `knowledge/projects/alpha/state.md` and
  `…/beta/state.md` share the stem `state`. That is the same class of defect as R-M7, fixed in
  the legacy search on 2026-09-17 by keying on the path.

## What was found — L12, the decay

- `access_tracking.decay_score` documents and computes "half-life" as `exp(-Δ/h)`. At Δ = h
  that is 0.368, not 0.5, so every stated half-life is about 1.44× longer than the number says.
  The sibling of the same shape in this repository is right: `co_activation.py` uses
  `0.5 ** (age_days / HALF_LIFE_DAYS)`.
- Its per-type days are a private copy that disagrees with the contract. `CLAUDE.md` §5 and
  `okf_types.TYPE_AGE_DAYS` say debugging 60, gap 90, pattern 180, workflow 365, qa 365,
  default 180, and concept/entity/decision/synthesis never archive. `decay_score` had
  debugging 30, gap 60, pattern 90, qa 180, concept 365, synthesis 365 — six of eight
  different. `okf_types` states the rule in its own comment: "one set, two readers".
- The two feed one decision: `archive_stale._access_keeps_alive` keeps a page whose
  `decay_score` is above 0.3, so a page's reprieve was computed against windows the archiver
  itself does not use.

## Practice on this date

- Exponential decay with a stated half-life is `2 ** (-Δ/h)`; `exp(-Δ/h)` is decay with a
  *time constant* h, and the two differ by ln 2. Naming one as the other is the classic
  units defect: the formula is fine, the contract it claims to implement is not.
- One identity per entity is the ordinary rule for an event log that several readers join on:
  the join key must name the thing, not one view of it. The vault already has a name for a
  page that is unique, stable and directly openable — its vault-relative path — and the
  strongest signal in this table (a citation) already uses it.

## The decision

- **One identity: the page's vault-relative path**, for every event kind and every writer.
  Impressions take `path` (the legacy row always carries it); the decision-filter impressions
  in `mcp_server` take `result["path"]`; `record_access` resolves its slug to
  `knowledge/notes/<slug>.md`. Nothing else changes shape.
- `access_tracking` reads and exports by path. Its public functions keep their slug-shaped
  signatures, because the CLI and `archive_stale` call them that way, and resolve the slug to
  the page path in one place. The export scan now sees paths and flushes the ones that are
  pages of this vault.
- Rows written before today keep whatever they hold. The column is free-form text, the
  database is derived and disposable (`cache/evidence-graph/telemetry.sqlite3`), and an old
  chunk-hash row simply never matches — exactly as it never matched before. No migration, no
  schema change.
- **The decay follows the contract.** `decay_score` reads `okf_types.TYPE_AGE_DAYS`,
  `DEFAULT_AGE_DAYS` and `NEVER_ARCHIVE_TYPES` instead of its own copy, and computes
  `0.5 ** (Δ/h)`, so a page at its window has decayed by exactly half. The contract text in
  `CLAUDE.md` does not change: the code now says what the contract said all along.
- A type that never archives keeps its base importance, which is what "never decays" means
  and what the old `99999` sentinel was imitating.
