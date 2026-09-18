# The documents name what the product actually uses

Dated 2026-09-17. Finding I-E1 of the third audit (documentation drift, confirmed). The
research before the fix.

## What was found

- `docs/ARCHITECTURE.md` still described the hybrid tier's vector leg as "LanceDB HNSW
  compatibility backend", the deployment shape as "SQLite + optional LanceDB", and the v4.0
  optional list as "LanceDB hybrid vectors"; the diagram said "legacy FTS/vector/Lance
  compatibility". `scripts/lookup_mode.py` and `skills/knowledge-lookup/SKILL.md` said
  "vectors/LanceDB" too.
- LanceDB was retired on 2026-09-07, and `CLAUDE.md` already says so. Checked here: no
  `lancedb` in `pyproject.toml` or `uv.lock`, and the only live vector path in
  `scripts/search_memory.py` is cosine similarity over cached embeddings
  (`_cosine_similarity`, `_cosine_similarities`). The remaining code mentions are the scale
  benchmark's adapters, which are measurement history and stay.
- `uv` must be exactly 0.12.3 (`[tool.uv] required-version`, both installers, every CI job).
  The three READMEs say so; `docs/USER-GUIDE.md` and `CONTRIBUTING.md` said only "install
  uv".
- `CLAUDE.md`/`AGENTS.md` §7 listed `scripts/mcp_server.py` and `scripts/doctor.py` under
  "v4.0 optional features (require --extra flags)". MCP is a base dependency (`mcp>=1.29` in
  `[project] dependencies`), `mcp-server` is an empty compatibility alias, and doctor needs
  no extra either.
- The `--semantic` flag in the skill's hybrid row is the same stale claim
  `tests/test_readme_i18n.py` already refuses in the user guide: vectors are on by default
  and `--no-semantic` turns them off.

## The decision

Each document says what the code does: the hybrid extra adds the embedding model, and the
vector leg is cosine similarity over the cached vectors on every tier; the guide and the
contributor's page name uv 0.12.3; the quick reference moves MCP and doctor above the
optional-extras line; the skill drops `--semantic` and LanceDB. `AGENTS.md` is rewritten from
`CLAUDE.md` so the two stay byte-identical.

Files: `docs/ARCHITECTURE.md`, `docs/USER-GUIDE.md`, `CONTRIBUTING.md`, `CLAUDE.md`,
`AGENTS.md`, `scripts/lookup_mode.py`, `skills/knowledge-lookup/SKILL.md`,
`docs/research/2026-09-17-the-documents-name-what-the-product-actually-uses.md`.
