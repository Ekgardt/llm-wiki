# The guide says what the code does

Date: 2026-09-26. Audit 2026-09-26 C-14.

## Facts (each checked against the code on 2026-09-26)

- `docs/USER-GUIDE.md` said the envelope's `index_timestamp` is always null;
  `mcp_server._index_timestamp` fills it with the answer's generation build time,
  or `collected_at` for `get_context`.
- It said optional extras are never upgraded unattended; `self_update` syncs the
  baseline with `chosen_extras(root)` in one `--inexact` sync, and the nightly now
  retries a stale sync (B-24).
- It said a hung pass on macOS/cron "ends when its maintenance lease is reclaimed";
  the registry reclaims only an owner proven dead, so a live, hung pass keeps the
  fence until it exits.
- `docs/CODE-NAVIGATION.md` and `docs/STRUCTURE.md` said the detached refresh starts
  "once per repository and commit"; `mcp_server` keys it per (checkout, commit).
- The plain `knowledge/` snapshot in `~/llm-wiki-snapshots`
  (`snapshot_knowledge.DEFAULT_SNAPSHOT_ROOT`, subtree `knowledge`) carries private
  pages and session records and was named only in the agents' contract.
- Diátaxis on reference documentation (https://diataxis.fr/reference/, fetched
  2026-09-26): "There should be no doubt or ambiguity in reference; it should be
  wholly authoritative."

## Decision

- The four statements are corrected to what the code does, and the guide names the
  plain snapshot, its path and that it is unencrypted.

## Files

- `docs/USER-GUIDE.md`
- `docs/CODE-NAVIGATION.md`
- `docs/STRUCTURE.md`
- `CHANGELOG.md`
