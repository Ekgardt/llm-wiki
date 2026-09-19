# The six capture corrections the first round left

Dated 2026-09-17. Finding C-F15 of the third audit has ten items. Four were closed on
2026-09-17 (`docs/research/2026-09-17-four-small-capture-corrections.md`: the prompt counter,
the reducer map, the strict decode, the README line). This note establishes the state of the
other six and decides each.

## What was found, item by item

1. **`daily_log_written` is true whenever the delegate exits 0** (`integration_adapter._tag_session_end`).
   `session_end_project_tag.main` always exits 0, including when it wrote nothing: no
   `LLM_WIKI_ROOT`, a session inside the vault, a session started in `$HOME`. `codex_memory
   daily-log` prints "Daily log tagged" for all of them. Confirmed by reading; still present.
2. **`_legacy_ok` cannot match the per-line form** (`flush_memory.py:113`). The first comparison
   upper-cases both sides; the second compares `line.strip().upper()` against the mixed-case
   `LEGACY_SENTINELS`, so a line reading `(no durable content)` never matches. This is live code:
   `_classify_response` is what `memory_queue._append_flush_block` calls for a deferred flush.
3. **`source_authority: session`** is written by `session_evidence` and known to
   `provenance.AUTHORITY_WEIGHTS` (0.9, between `inferred` and `ai-derived`). It is not in the
   claim authorities — `claims.py`, `bitemporal_claims.py`, `contradiction_pipeline.py`,
   `evidence_graph.py` (including a SQL `CHECK`) know only user/web/ai-derived/inferred — and
   `CLAUDE.md` rule 13 names those four. Nothing is broken today: session records are not a
   corpus source kind, and `knowledge_extractor` maps an unknown authority to `inferred` rather
   than raising.
4. **Backfill and live capture name and date the same session differently.** Backfill names the
   record by the transcript's file stem (`rollout-2026-…-<uuid>` for Codex) while live capture
   names it by `session_id`, and dates it by the file's UTC mtime while live capture uses the
   local capture day. So "existing records are left alone" does not hold between the two paths:
   the same session is written twice, under two names and possibly two days. The 10 000-file cap
   also truncates the scan silently.
5. **The session's advisory comes from someone else's project.** `session_start_context.advisory_block`
   and `guardrails_block` each pick the slug of the most recent `codex_heartbeats` entry. Two
   sessions that start together swap advisories, and the lookup is written out twice.
6. **No re-entry guard in the adapter.** `CLAUDE_INVOKED_BY` is set by `memory_state.spawn_detached`
   and read only by the two retired delegates. A memory call made from a foreground process
   (a nightly compile, a queue drain) does not carry it at all, so a host whose machine-managed
   settings register our hooks would capture the memory call itself as a session.

## Practice on this date

- A hook that must exit 0 still has to report what it did. The shape already used here is the
  delegate's stdout: the adapter captures it and only forwards it for the three delegates that
  speak to the host, so a machine-readable line costs nothing.
- Windows Task Scheduler, launchd and systemd all treat "the process exited 0" as success, which
  is why an exit code is the wrong place to carry "I skipped".
- A memory call is service traffic, not an agent's turn. The product already keeps it out of the
  host's session store (`--no-session-persistence`) and out of the vault's directory
  (`llm_client.provider_cwd`); marking its environment is the same rule applied to hooks.

## The decisions

1. `session_end_project_tag` prints one JSON line — `{"daily_log_written": true|false}` — and the
   adapter reads it instead of the exit code. Nothing on stdout (an older install, a delegate that
   died) keeps the old reading: exit 0 means written, because that is what it used to mean.
2. `_legacy_ok` compares the per-line form against the same upper-cased set as the whole-text form.
3. `session` stays as it is, and `CLAUDE.md` (and `AGENTS.md`, byte-identical) says what it is: the
   authority of a raw session record, used by retrieval weighting only, never a claim authority.
   A test pins both halves, so the day session evidence joins the corpus the choice is visible.
4. Backfill takes the session id from the transcript's own records when it carries one, dates the
   record by the local day of the file's last change, and says how many transcripts the cap left
   unscanned. Head-versus-tail is not a real difference: the writer bounds both paths to 512 KiB
   from the start of the rendered document.
5. `build_context_items` takes the slug of the session being started — the adapter already computed
   it from the session's own working directory — and passes it to both blocks. The heartbeat lookup
   stays as the fallback for the context file and `main()`, written once.
6. The adapter refuses host events while `CLAUDE_INVOKED_BY` is set, and `llm_client` sets that
   variable for every provider subprocess, so the refusal holds for a memory call made from any
   process. `--capture-worker` and `--maintenance` are unaffected: they are not host events.

Files: `scripts/session_end_project_tag.py`, `scripts/integration_adapter.py`,
`scripts/flush_memory.py`, `scripts/backfill_sessions.py`, `scripts/session_start_context.py`,
`scripts/llm_client.py`, `CLAUDE.md`, `AGENTS.md`,
`tests/test_the_six_capture_corrections_the_first_round_left.py`
