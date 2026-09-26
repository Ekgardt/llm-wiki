# The guide matches the passes

Date: 2026-09-25. Audit item C-33 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked against the code)

- `docs/USER-GUIDE.md` listed a nightly step that no longer exists (rebuild the FTS index), said
  "yesterday's session records", and described the weekly as "Everything nightly does +". The
  nightly steps are `_intake_steps`, compile and fact keys, `_post_compile_steps`, the generation
  refresh, telemetry, the health report, report pruning and the code update; the weekly is its own
  seven script steps plus contradictions, reflection and tiers.
- It gave Windows/systemd limits of 3 h nightly and 5 h weekly; `install_control.SCHEDULER_LIMIT_HOURS`
  is 4 and 6.
- Hints said `uv sync --extra <one>` (guide, ARCHITECTURE, `mcp_server`, `mcp_http`, `code_graph`,
  `install_models`); `mcp-server` is an empty compatibility alias, MCP is a base dependency.
- `CONTRIBUTING.md` called the repair surface "check-only"; the installers run it with `--apply`.
- ARCHITECTURE still named "the legacy optional vector path", retired on 2026-09-23.
- Already correct on this date: the dead-task retention paragraph and STRUCTURE (no `cache/models/`).

## Source

- uv, "Syncing", https://docs.astral.sh/uv/concepts/projects/sync/ (fetched 2026-09-25): "uv sync
  performs "exact" syncing by default, which means it will remove any packages that are not present
  in the lockfile." and "To retain extraneous packages, use the `--inexact` flag".
- Diátaxis, "Reference", https://diataxis.fr/reference/ (fetched 2026-09-25): "There should be no
  doubt or ambiguity in reference; it should be wholly authoritative."

## Decision

- Rewrite the two step lists and the limits from the code; every extra hint is
  `uv sync --locked --inexact [--extra X]`; CONTRIBUTING and ARCHITECTURE say what the code does.
- A test ties the guide to `SCHEDULER_LIMIT_HOURS` and to the weekly's step labels, and fails on
  any exact `uv sync ... --extra` hint in READMEs, docs or scripts.

## Files

- `docs/USER-GUIDE.md`
- `docs/ARCHITECTURE.md`
- `CONTRIBUTING.md`
- `scripts/mcp_server.py`
- `scripts/mcp_http.py`
- `scripts/code_graph.py`
- `scripts/install_models.py`
- `tests/test_the_guide_matches_the_passes.py`
- `CHANGELOG.md`

## Follow-up the same day

- `tests/test_quality_guards.py` requires ARCHITECTURE to keep the phrase "manual dependency selection"; the rewrite had dropped it. Restored with the correct command. File: `docs/ARCHITECTURE.md`.
