# The names of the first prompt, and three dead branches

Dated 2026-09-18. Findings M-C1, M-C3 and the two doctor islands of the third audit, plus
the leftovers of M-C4. What each dead name is, why it is dead, and what the deletion costs.

Files: `scripts/compile_memory.py`, `scripts/llm_client.py`, `scripts/claims.py`,
`scripts/doctor.py`, `scripts/rebuild_memory_index.py`, `scripts/maybe_compile.py`,
`scripts/build_tiers.py`, `docs/ARCHITECTURE.md`, `tests/test_compile_audit.py`,
`tests/test_security_invariants.py`, `tests/test_compile_integration.py`,
`tests/test_claims.py`, `tests/test_doctor.py`,
`tests/test_repair_asks_about_the_provider_the_operator_chose.py`,
`tests/test_index_publishes_only_public_pages.py`, `tests/test_maybe_compile.py`,
`tests/test_generation_integration.py`.

## What was found

- **The first prompt's protocol.** `existing_knowledge_snapshot` and its eight private
  helpers built a text list of pages for a prompt that no longer exists, and
  `parse_compile_audit`, `_audit_line`, `_audit_counts`, `_AUDIT_COUNTS` and
  `_compile_succeeded` parse the `COMPILE_AUDIT:` / `COMPILE_DONE:` lines of that same
  protocol. The compile has answered with JSON against a schema since `compile-draft/v4`;
  the audit counts now live in the draft object. Nothing outside tests calls any of them,
  and the fake provider still appends a `COMPILE_AUDIT:` tail so those tests can pass.
- **A test-only API each.** `llm_client.call_llm_json` (prompt-level JSON constraint,
  superseded by the schema every caller passes), `claims.page_may_auto_supersede`
  (automatic semantic supersession is disabled by contract — `CLAUDE.md`, Stage 2),
  `rebuild_memory_index._published_notes` (superseded by `published_paths`, which every
  writer uses), `doctor._MaintenanceHeartbeat._beat_once` (superseded by
  `renew_until_stopped`), `doctor._codex_config_state` with its seven helpers, and the two
  re-exports in `maybe_compile` that only its own test calls.
- **A branch that cannot fire.** `doctor._repair_queue_capabilities` unblocks queue tasks
  whose `blocked_capability` is `llm.compile`, `llm.flush` or `llm.query`. Nothing in the
  product ever sets one: the only producer of a blocked capability is the queue's own
  `process_cleanup` (`memory_queue.py`), and the way back from that is the `unblock` CLI
  command added in this round's queue work. The audit filed the same fact as Q-M7. The
  first round made this branch ask about the operator's forced provider (M-A9); that fix
  was correct and the branch is still unreachable, so the test that pins it goes with it.
- **A second publication path for tiers.** `build_snapshot_tiers`, `generate_l1_for_source`,
  `tier_artifact_key` and their staging/publish/withdraw helpers write L0/L1/L2 artifacts
  into a generation directory. The production generation is built by
  `build_incremental_generation` (doctor), which never calls them; the only caller is one
  integration test. The only reader of any tier is `build_advisory._decision_line`, and it
  reads the mutable `cache/tiers/` cache that the weekly `build_all_tiers` writes — a
  different path entirely.

## Practice on this date

- The owner's standing rule for this repository is to delete what nothing runs, without
  breaking anything, and this round's brief states the test: a feature nothing runs is
  either wired in — when a contract promises it and the wiring is small — or deleted with
  its tests and its contract sentence. Wiring snapshot tiers in is not small: it would add
  an artifact family to every incremental generation build and a reader for it, for a tier
  that already has a working cache.
- "Dead code … should be deleted. It is not needed, and version control will keep it if it
  is ever wanted again" (Fowler and Beck's smell of *Lazy Element* / *Dead Code* in
  *Refactoring*, 2nd ed., ch. 3; the same reasoning the project applied on 2026-09-17 when
  it removed `loop_detector.py` and `agent_timeline.py`).

## The decision

- All of the above are deleted, together with the tests that exist only to exercise them
  and the one contract sentence that promised the generation-published tiers
  (`docs/ARCHITECTURE.md`: the generation member list loses "L0/L1/L2 tiers"; the tier
  section that describes L0/L1/L2 as a reading strategy stays, because that is still true).
- The fake provider's reply loses its `COMPILE_AUDIT:` tail: it existed only for the
  deleted parsers, and a canned reply that carries a dead protocol teaches tests to expect
  it.
- `test_index_publishes_only_public_pages.py` keeps its assertion about this repository's
  own allowlist and asks `published_paths`, the live reader, instead of the deleted one.
  `test_maybe_compile.py` keeps every assertion and calls `operational_ownership` directly.
- Nothing here changes a path, an environment variable, or a runtime contract, and nothing
  deleted is reachable from a hook, an installer, a scheduler target, the MCP server, the
  OpenCode plugin, or a command named in the README or the user guide — each was checked by
  name across `scripts/`, `tests/`, `benchmark/`, `integrations/`, `skills/`, `docs/` and
  the install scripts before deletion.
